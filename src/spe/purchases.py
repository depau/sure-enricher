"""Logical purchases (receipt / order) with several payment legs, matched to existing Sure transactions.

The merchant source (receipt, order) owns *what* was bought; the payment ledger (bank, voucher wallet)
owns *how much* left which account and when. A matched purchase only edits metadata of the legs it
found (notes, category, optionally a split); it never creates a transaction for the purchase total.
"""

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from spe import writer
from spe.db import Connection, Evidence, SureOp, record_evidence
from spe.sure import SureClient

CENT = Decimal("0.01")
VOUCHER = "edenred"  # payment method paid from the meal-voucher wallet
NO_LEDGER = {"gift_card", "promo", "loyalty", "cash"}  # paid from balances Sure does not track: no leg to find


# The merchant split children get in Sure (a family merchant, created if missing), so rules can target them.
BRAND = {"esselunga": "Esselunga", "amazon_it": "Amazon", "aliexpress": "AliExpress"}

@dataclass(frozen=True)
class Line:
    description: str
    amount: Decimal  # after line discounts; discount lines are negative
    voucher_eligible: bool = True  # meal vouchers pay food: these items go on the voucher leg first


@dataclass(frozen=True)
class Payment:
    method: str  # "card" (any bank-synced account), "edenred", "gift_card", ...
    amount: Decimal  # positive = charge, negative = refund back to the same rail
    on: date | None = None  # settlement date when the source knows it
    reference: str | None = None


@dataclass(frozen=True)
class Purchase:
    provider: str
    id: str  # receipt / order id: the strong evidence
    merchant: str
    date: date  # purchase date, Europe/Rome
    total: Decimal  # net amount charged after discounts and refunds
    payments: tuple[Payment, ...]
    lines: tuple[Line, ...] = ()
    keywords: tuple[str, ...] = ()  # bank-statement spellings of the merchant, e.g. ("ESSELUNGA",)
    window_days: int = 5  # card settlement lag tolerated

    def problems(self) -> list[str]:
        out = []
        if sum(p.amount for p in self.payments) != self.total:
            out.append(f"payments sum {sum(p.amount for p in self.payments)} != total {self.total}")
        return out

    @property
    def marker(self) -> str:
        return f"[spe:{self.provider}:{self.id}]"


@dataclass
class LegMatch:
    payment: Payment
    tx: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    strong: bool = False


def _amount(tx: dict) -> Decimal:
    return Decimal(tx["amount_decimal"])


def match(p: Purchase, txs: list[dict], voucher_accounts: set[str], claimed: set[str],
          picks: dict[str, str] | None = None) -> list[LegMatch]:
    """Deterministic: exact amount, date window, account rail; merchant keyword or a dedicated wallet makes it strong.

    picks: leg index (as a string) -> transaction id the owner chose among that leg's candidates (In sospeso).
    A pick only counts while that transaction is still a valid candidate.
    """
    out, taken = [], set(claimed)
    for i, pay in enumerate(p.payments):
        if pay.method in NO_LEDGER:
            out.append(LegMatch(pay, strong=True))
            continue
        start = (pay.on or p.date) - timedelta(days=3)  # banks may date a card charge before the shop reports it
        end = (pay.on or p.date) + timedelta(days=p.window_days)
        cands = [
            t
            for t in txs
            if t["id"] not in taken
            and not t.get("transfer")
            and not t.get("split_parent")
            and not t.get("excluded")
            and _amount(t) == pay.amount
            and start <= date.fromisoformat(t["date"]) <= end
            and ((t["account"]["id"] in voucher_accounts) == (pay.method == VOUCHER))
        ]
        named = [t for t in cands if any(k.casefold() in _label(t) for k in p.keywords)]
        picked = next((t for t in cands if t["id"] == (picks or {}).get(str(i))), None)
        if picked is not None:
            m = LegMatch(pay, picked, cands, strong=True)  # a human chose it
        elif pay.method == VOUCHER and len(cands) == 1:
            m = LegMatch(pay, cands[0], cands, strong=True)  # dedicated wallet: amount + date is enough
        elif len(named) == 1:
            m = LegMatch(pay, named[0], cands, strong=True)
        else:
            m = LegMatch(pay, None, named or cands)  # none or ambiguous: a human decides
        if m.tx:
            taken.add(m.tx["id"])
        out.append(m)
    return out


def _brief(tx: dict) -> dict:
    return {"id": tx["id"], "date": tx["date"], "name": tx.get("name"), "amount": tx["amount_decimal"], "account": tx["account"].get("name")}


def _label(tx: dict) -> str:
    return f"{tx.get('name', '')} {(tx.get('merchant') or {}).get('name', '')}".casefold()


def _items(lines: tuple[Line, ...]) -> list[Line]:
    """Net items: a discount (negative line) is folded into the item above it."""
    out: list[Line] = []
    for ln in lines:
        if ln.amount < 0 and out:
            out[-1] = replace(out[-1], amount=out[-1].amount + ln.amount)
        elif ln.amount > 0:
            out.append(ln)
    return [ln for ln in out if ln.amount > 0]


def proportional(items: list[Line], amount: Decimal) -> list[tuple[str, Decimal]]:
    """Spread a charge over items by their price, to the cent; rounding leftovers go to the dearest item."""
    total = sum(ln.amount for ln in items)
    parts = [(ln.description, (amount * ln.amount / total).quantize(CENT, ROUND_DOWN)) for ln in items]
    biggest = max(range(len(parts)), key=lambda i: items[i].amount)
    parts[biggest] = (parts[biggest][0], parts[biggest][1] + amount - sum(a for _, a in parts))
    return [(d, a) for d, a in parts if a]


def allocate(p: Purchase, legs: list[LegMatch]) -> dict[int, list[tuple[str, Decimal]]]:
    """Product entries per charge leg (refund legs get none: notes only).

    A receipt whose items add up to the total is cut exactly: voucher legs take voucher-eligible items first,
    other legs the rest, an item is shared between two legs when it straddles them ("(parte)"). When items do
    not add up to the charges (orders with shipping, discounts, installments), each charge is spread over the
    items in proportion to their prices.
    """
    items = _items(p.lines)
    result: dict[int, list[tuple[str, Decimal]]] = {i: [] for i in range(len(legs))}
    charges = [i for i, leg in enumerate(legs) if leg.payment.amount > 0]
    if not items or not charges:
        return result
    if sum(ln.amount for ln in items) != sum(legs[i].payment.amount for i in charges):
        for i in charges:
            result[i] = proportional(items, legs[i].payment.amount)
        return result
    left = [ln.amount for ln in items]
    for i in sorted(charges, key=lambda i: legs[i].payment.method != VOUCHER):  # vouchers first
        voucher = legs[i].payment.method == VOUCHER
        order = sorted(range(len(items)), key=lambda k: items[k].voucher_eligible != voucher)  # stable: receipt order
        need = legs[i].payment.amount
        for k in order:
            take = min(left[k], need)
            if take > 0:
                result[i].append((items[k].description + ("" if take == items[k].amount else " (parte)"), take))
                left[k] -= take
                need -= take
    return result


def _notes(p: Purchase, existing: str | None) -> str:
    paid = " + ".join(f"{pay.method} €{pay.amount}" for pay in p.payments)
    items = "; ".join(f"{line.description} €{line.amount}" for line in p.lines[:30])
    ours = f"{p.merchant} {p.id} · total €{p.total} · paid {paid}" + (f"\n{items}" if items else "") + f"\n{p.marker}"
    return f"{existing}\n\n{ours}" if existing else ours


def plan_ops(p: Purchase, legs: list[LegMatch]) -> list[dict]:
    """Edits for a fully matched purchase: notes, then a split of each charge into its product entries.

    The split children carry the product names and no category: Sure's own classifier (a rule with the
    "auto-categorize" action) categorizes them. Never touches amounts or dates; a transaction the owner
    edited by hand (`user_modified`) or already split is left alone.
    """
    alloc = allocate(p, legs)
    ops = []
    for i, leg in enumerate(legs):
        tx = leg.tx
        if tx is None:
            continue
        before = {k: tx.get(k) for k in ("notes", "category", "split_parent", "amount_decimal", "date", "name")}  # name: display only
        if p.marker not in (tx.get("notes") or ""):
            notes = _notes(p, tx.get("notes"))
            ops.append({"kind": "update", "tx": tx, "fields": {"notes": notes}, "before": before})
            before = before | {"notes": notes}  # what the split expects once the notes have landed
        if alloc[i] and not tx.get("split_parent") and not tx.get("user_modified"):
            splits = [{"name": name, "amount": str(amount)} for name, amount in alloc[i]]
            fields = {"splits": splits} | ({"merchant": BRAND.get(p.provider)} if BRAND.get(p.provider) else {})
            ops.append({"kind": "split", "tx": tx, "fields": fields, "before": before})
    return ops


def reconcile(db: Session, sure: SureClient, voucher_accounts: set[str], user_id: int | None) -> str:
    """Match the user's unlinked purchases against what they see in Sure and stage edits for approval."""
    every = db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.kind == "purchase")).all()
    pending = [ev for ev in every if "payments" in ev.payload and ev.issue and not ev.payload.get("ignored")]
    if not pending:
        return "nothing to reconcile"
    purchases = [(ev, from_json(ev.payload)) for ev in pending]
    links = {ev.id: set(ev.payload.get("links", [])) for ev in every}
    claimed = set().union(*links.values()) if links else set()
    days = [pay.on or p.date for _, p in purchases for pay in p.payments] + [p.date for _, p in purchases]
    start = min(days) - timedelta(days=3)
    end = max(days) + timedelta(days=max(p.window_days for _, p in purchases))
    txs = list(sure.transactions(start_date=start.isoformat(), end_date=end.isoformat()))

    linked = 0
    for ev, p in purchases:
        if p.problems():
            continue  # its issue already says why
        legs = match(p, txs, voucher_accounts, claimed - links[ev.id], ev.payload.get("picks"))
        missing = [leg for leg in legs if not leg.strong]
        if missing:
            ev.issue = "; ".join(
                f"{leg.payment.method} €{leg.payment.amount}: "
                + (f"{len(leg.candidates)} candidates, needs review" if leg.candidates else "no Sure transaction yet")
                for leg in missing
            )
            # what In sospeso offers to choose from, per leg
            ev.payload = {**ev.payload, "candidates": {str(i): [_brief(t) for t in leg.candidates[:10]]
                                                       for i, leg in enumerate(legs) if not leg.strong and leg.candidates}}
            continue
        try:
            ops = plan_ops(p, legs)
        except ValueError as e:
            ev.issue = str(e)
            continue
        for op in ops:
            tx = op["tx"]
            writer.stage(
                db,
                before=op["before"],
                key=f"{op['kind']}:{tx['id']}:{p.marker}",
                kind=op["kind"],
                provider=p.provider,
                payload=op["fields"],
                summary=f"{p.merchant} {p.id}: {'notes' if op['kind'] == 'update' else 'split into products'} on {tx['date']} {tx['name']} €{tx['amount_decimal']}",
                approve=False,
                user_id=user_id,
                group=p.marker,
                account_id=tx["account"]["id"],
                transaction_id=tx["id"],
            )
        ev.payload = {**{k: v for k, v in ev.payload.items() if k != "candidates"}, "links": [leg.tx["id"] for leg in legs if leg.tx]}
        claimed.update(ev.payload["links"])
        ev.issue = None
        linked += 1
    db.commit()
    return f"{linked}/{len(purchases)} purchases linked"


def backfill_split_metadata(db: Session, user_id: int | None) -> None:
    """Splits we made before children carried the merchant and the parent's tags: queue a re-split adding them,
    for approval (it rewrites existing transactions). Caller commits."""
    old = db.scalars(select(SureOp).where(SureOp.user_id == user_id, SureOp.kind == "split", SureOp.status == "done")).all()
    for o in old:
        merchant = BRAND.get(o.provider)
        if merchant is None or "merchant" in o.payload:
            continue
        writer.stage(db, key=f"resplit:{o.transaction_id}:{o.group_key}", kind="resplit", provider=o.provider,
                     payload={"merchant": merchant, "expect": o.payload["splits"]}, summary=f"add merchant {merchant} and tags to the products",
                     approve=False, user_id=user_id, group=o.group_key, account_id=o.account_id, transaction_id=o.transaction_id,
                     before=o.before)


def record(db: Session, conn: Connection, p: Purchase) -> None:
    """Store a purchase as evidence of the connection. Unlinked purchases carry an issue until reconcile() links them."""
    ev = db.scalar(select(Evidence).filter_by(connection_id=conn.id, key=p.id))
    payload = to_json(p) | ({"links": ev.payload["links"]} if ev and "links" in ev.payload else {})
    problems = p.problems()
    # A new payment leg (next shipment, installment, refund) reopens a linked purchase; enriched legs are skipped.
    unchanged = ev is not None and ev.payload.get("payments") == payload["payments"]
    issue = "; ".join(problems) if problems else (None if payload.get("links") and unchanged else "not linked yet")
    record_evidence(db, conn, p.id, "purchase", payload, issue)


def to_json(p: Purchase) -> dict:
    return json.loads(json.dumps(asdict(p), default=str))


def from_json(d: dict) -> Purchase:
    def dt(v: str | None) -> date | None:
        return date.fromisoformat(v) if v else None

    return Purchase(
        provider=d["provider"],
        id=d["id"],
        merchant=d["merchant"],
        date=date.fromisoformat(d["date"]),
        total=Decimal(d["total"]),
        payments=tuple(Payment(x["method"], Decimal(x["amount"]), dt(x.get("on")), x.get("reference")) for x in d["payments"]),
        lines=tuple(Line(x["description"], Decimal(x["amount"]), x.get("voucher_eligible", True)) for x in d["lines"]),
        keywords=tuple(d.get("keywords", ())),
        window_days=d.get("window_days", 5),
    )


def load_fixture(path: Path) -> list[Purchase]:
    return [from_json(d) for d in json.loads(path.read_text())["purchases"]]
