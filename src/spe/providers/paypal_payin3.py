"""PayPal Pay in 3: plans detected in the PayPal account that Sure already syncs. No PayPal login.

Writeback (needs a manual Loan account + the Sure MCP connection), validated on Sure 79e8a26 for this exact feed shape:
  1. credit inflow  --match_transfer-->  Loan   (loan +X, funds_movement: neither income nor spending)
  2. each repayment --match_transfer-->  Loan   (loan -a, PayPal side becomes loan_payment: spending)
     then category = the purchase's category, so spending lands in that category month by month
  3. merchant charge: split into one excluded child, so the full price is not spent a second time
Net worth drops by X on purchase day (card + debt) while spending is spread: debt is real, goods are not an asset.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from spe import writer
from spe.db import record_evidence
from spe.payin3 import INSTALLMENTS, Plan, detect
from spe.providers import Blocked, Context, register
from spe.vouchers import ROME

LOOKBACK = timedelta(days=150)  # a plan spans ~2 months; older ones are complete
BEFORE_FIELDS = ("notes", "category", "amount_decimal", "date", "name")  # name: display only


def plan_payload(p: Plan) -> dict:
    return {
        "date": p.day.isoformat(),
        "merchant": (p.merchant or {}).get("name"),
        "merchant_tx": (p.merchant or {}).get("id"),
        "total": str(p.total),
        "reduced": str(p.reduced),
        "paid": str(p.paid),
        "remaining": str(p.remaining),
        "repayments": [{"tx": r["id"], "date": r["date"], "amount": r["amount_decimal"]} for r in p.repayments],
    }


def _before(tx: dict) -> dict:
    return {k: tx.get(k) for k in BEFORE_FIELDS}


class PayIn3:
    name = "paypal_payin3"
    label = "PayPal Pay in 3 (from Sure's PayPal feed)"
    interval = timedelta(hours=12)

    def sync(self, ctx: Context) -> str:
        cfg = ctx.state.config or {}
        if not cfg.get("paypal_account"):
            raise Blocked("choose the PayPal account in Sure for this connection")
        today = datetime.now(ROME).date()
        txs = list(ctx.sure.transactions(account_id=cfg["paypal_account"], start_date=(today - LOOKBACK).isoformat(), end_date=today.isoformat()))
        writeback = bool(cfg.get("loan_account"))
        since = date.fromisoformat(cfg["since"]) if cfg.get("since") else date.min
        plans = detect(txs, today)
        for p in plans:
            issue = p.issue
            if not issue:
                if writeback and p.day >= since:
                    issue = self._stage_writeback(ctx, p, cfg["loan_account"])
                else:
                    self._stage_notes(ctx, p, with_category=None)
            record_evidence(ctx.db, ctx.state, p.id, "installment_plan", plan_payload(p), issue)
        ctx.db.commit()
        active = [p for p in plans if not p.issue and p.remaining]
        owed = sum((p.remaining for p in active), Decimal(0))
        flagged = sum(1 for p in plans if p.issue)
        mode = "debt writeback" if writeback else "notes only (choose a loan account for the debt)"
        ctx.state.cursor = {**(ctx.state.cursor or {}), "outstanding": str(owed), "active": len(active)}
        return f"{len(active)} active plans, €{owed} outstanding, {flagged} need review; {mode}"

    def _stage_notes(self, ctx: Context, p: Plan, with_category: str | None) -> None:
        running = p.total - p.reduced
        for k, r in enumerate(p.repayments, 1):
            running -= Decimal(r["amount_decimal"])
            mark = f"[spe:payin3:{p.id}:{k}]"
            existing = r.get("notes") or ""
            fields: dict = {}
            if mark not in existing:
                text = f"PayPal Pay in 3 · rata {k}/{INSTALLMENTS} · {(p.merchant or {}).get('name')} €{p.total} del {p.day} · residuo €{running}. {mark}"
                fields["notes"] = f"{existing}\n\n{text}" if existing else text
            if with_category and not r.get("category"):
                fields["category_id"] = with_category
            if fields:
                writer.stage(
                    ctx.db, user_id=ctx.state.user_id, group=f"[spe:payin3:{p.id}]", key=f"update:{r['id']}:{mark}", kind="update", provider=self.name, payload=fields,
                    summary=f"Pay in 3 {p.day} €{p.total}: installment {k} {r['date']} €{r['amount_decimal']}"
                    + (" → category" if "category_id" in fields else " notes"),
                    approve=False, account_id=r["account"]["id"], transaction_id=r["id"], before=_before(r),
                )

    def _stage_writeback(self, ctx: Context, p: Plan, loan: str) -> str | None:
        """Stage the loan recipe for one clean plan. Returns an issue when it cannot be applied as is."""
        merchant = p.merchant or {}
        if p.reductions or p.refunds:
            self._stage_notes(ctx, p, with_category=None)
            return "refunded/cancelled plan: debt writeback not validated for this case, notes only"
        excluded = [t["name"] for t in (p.credit, *p.repayments) if t.get("excluded")]
        if excluded:
            return f"un-exclude in Sure (excluded rows cannot be matched): {', '.join(excluded)}"
        category = (merchant.get("category") or {}).get("id")
        if not category:
            return f"give '{merchant.get('name')}' ({p.day}, €{p.total}) a category in Sure: installments inherit it"

        def match(tx: dict, what: str) -> None:
            if tx.get("transfer"):
                return  # already linked (by us earlier, or by the owner)
            writer.stage(
                ctx.db, user_id=ctx.state.user_id, group=f"[spe:payin3:{p.id}]", key=f"mcp:match_transfer:{tx['id']}", kind="mcp", provider=self.name,
                payload={"tool": "match_transfer", "arguments": {"transaction_id": tx["id"], "target_account_id": loan}},
                summary=f"Pay in 3 {p.day} €{p.total}: {what} {tx['date']} €{tx['amount_decimal']} → Loan",
                approve=False, account_id=tx["account"]["id"], transaction_id=tx["id"], before=_before(tx),  # display only
            )

        match(p.credit, "open debt")
        for k, r in enumerate(p.repayments, 1):
            match(r, f"installment {k}")
        self._stage_notes(ctx, p, with_category=category)
        if not merchant.get("split_parent"):
            writer.stage(
                ctx.db, user_id=ctx.state.user_id, group=f"[spe:payin3:{p.id}]", key=f"split:{merchant['id']}:payin3", kind="split", provider=self.name,
                payload={"splits": [{"name": f"{merchant['name']} (Pay in 3: spesa ripartita sulle rate)",
                                     "amount": merchant["amount_decimal"], "category_id": category, "excluded": True}]},
                summary=f"Pay in 3 {p.day}: exclude the full €{p.total} charge, the installments carry the spending",
                approve=False, account_id=merchant["account"]["id"], transaction_id=merchant["id"],
                before=_before(merchant) | {"split_parent": False},
            )
        return None


register(PayIn3())
