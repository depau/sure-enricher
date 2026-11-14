"""What the overview, approval and history pages show: Sure ops grouped per purchase / plan / movement."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from spe.db import Evidence, SureOp, now
from spe.i18n import Tr
from spe.vouchers import ROME

EN = Tr("en")

METHOD = {"card": "card", "edenred": "vouchers", "gift_card": "gift card", "cash": "cash"}
MARKER = re.compile(r"\[spe:([a-z0-9_]+):([^:\]]+)(?::\d+)?\]")


def eur(d: object, tr: Tr = EN) -> str:
    """€1.234,56 in Italian, €1,234.56 in English."""
    out = "€" + f"{Decimal(str(d)):,.2f}"
    return out.replace(",", " ").replace(".", ",").replace(" ", ".") if tr.lang == "it" else out


def when(d: datetime | None, tr: Tr = EN) -> str:
    if d is None:
        return "—"
    local, today = d.astimezone(ROME), now().astimezone(ROME).date()
    day = {today: tr("today"), today - timedelta(days=1): tr("yesterday")}.get(local.date(), f"{local:%d/%m}")
    return f"{day} {local:%H:%M}"


@dataclass
class Product:
    name: str
    amount: Decimal
    part: bool  # a share of one item split across two payment legs
    excluded: bool = False


@dataclass
class Leg:
    tx_id: str | None
    account: str
    tx: str
    amount: Decimal | None
    date: str | None
    products: list[Product] = field(default_factory=list)
    notes: str | None = None
    category: bool = False
    merchant: str | None = None
    resplit: bool = False  # products already split: only their metadata is added

    @property
    def products_sum(self) -> Decimal:
        return sum((p.amount for p in self.products), Decimal(0))


@dataclass
class Group:
    key: str
    provider: str
    ops: list[SureOp]
    title: str
    meta: str
    total: Decimal | None
    total_note: str
    legs: list[Leg]
    minis: list[str]

    @property
    def ids(self) -> list[int]:
        return [o.id for o in self.ops]

    @property
    def status(self) -> str:
        statuses = {o.status for o in self.ops}
        return statuses.pop() if len(statuses) == 1 else "mixed"

    @property
    def at(self) -> datetime:
        return max(o.updated_at for o in self.ops)

    @property
    def tx_id(self) -> str | None:
        return next((o.transaction_id for o in self.ops if o.transaction_id), None) or next(
            (o.response.get("id") for o in self.ops if o.kind == "create" and isinstance(o.response, dict)), None)


def build(db: Session, user_id: int, ops: list[SureOp], accounts: dict[str, dict], tr: Tr = EN) -> list[Group]:
    """Ops grouped by group_key, in the order their first op appears."""
    by: dict[str, list[SureOp]] = {}
    for o in ops:
        by.setdefault(o.group_key, []).append(o)
    return [_describe(db, user_id, key, sorted(group, key=lambda o: o.id), accounts, tr) for key, group in by.items()]


def _tx(o: SureOp) -> dict:
    """The Sure transaction as planned (before) or as written (response)."""
    resp = o.response if isinstance(o.response, dict) and "amount_decimal" in o.response else {}
    return {**resp, **{k: v for k, v in (o.before or {}).items() if v is not None}}


def _describe(db: Session, user_id: int, key: str, ops: list[SureOp], accounts: dict[str, dict], tr: Tr) -> Group:
    def account(account_id: str | None) -> str:
        return accounts.get(account_id or "", {}).get("name") or tr("account not visible")

    def money(x: object) -> str:
        return eur(x, tr)

    legs: dict[str, Leg] = {}
    minis: list[str] = []
    # Older ops did not snapshot the transaction name: take it from any op that wrote to the same transaction.
    tx_ids = {o.transaction_id for o in ops if o.transaction_id}
    names = {o.transaction_id: o.response["name"] for o in db.scalars(select(SureOp).where(SureOp.transaction_id.in_(tx_ids), SureOp.status == "done"))
             if isinstance(o.response, dict) and o.response.get("name")}
    for o in ops:
        tx = {"name": names.get(o.transaction_id)} | _tx(o)
        if o.kind == "create":
            p = o.payload
            minis.append(tr("Create transaction “%(name)s” in %(account)s", name=p.get("name", ""), account=account(p.get("account_id"))))
        elif o.kind == "mcp":
            minis.append(tr("Link “%(name)s” of %(day)s (%(amount)s) to the loan", name=tx.get("name") or tr("transaction"),
                            day=_date(tx.get("date")) or "?", amount=money(tx["amount_decimal"]) if "amount_decimal" in tx else "?"))
        else:
            amount = tx.get("amount_decimal")
            leg = legs.setdefault(o.transaction_id or str(o.id), Leg(o.transaction_id, account(o.account_id), tx.get("name") or tr("transaction"),
                                                                      Decimal(amount) if amount is not None else None, tx.get("date")))
            if o.kind in ("split", "resplit"):
                leg.products = [Product(s["name"].removesuffix(" (parte)"), Decimal(s["amount"]), s["name"].endswith("(parte)"), bool(s.get("excluded")))
                                for s in o.payload.get("splits") or o.payload.get("expect", [])]
                leg.merchant, leg.resplit = o.payload.get("merchant"), o.kind == "resplit"
            else:
                leg.notes = o.payload.get("notes") or leg.notes
                leg.category = leg.category or "category_id" in o.payload
                leg.merchant = o.payload.get("merchant") or leg.merchant

    first = ops[0]
    title, meta, total, note = first.summary, "", None, ""
    m = MARKER.fullmatch(key)
    ev = db.scalar(select(Evidence).where(Evidence.user_id == user_id, Evidence.key == m[2],
                                          Evidence.kind.in_(("purchase", "installment_plan")))) if m else None
    if ev is not None and ev.kind == "purchase":
        p = ev.payload
        title = purchase_title(ev, tr)
        pays, lines = p.get("payments", []), p.get("lines", [])
        methods = sorted({tr(METHOD.get(x["method"], x["method"])) for x in pays})
        paid = (tr("%(n)s charges ", n=len(pays)) if len(pays) > 1 else "") + (" + ".join(methods) if methods else "?")
        meta = " · ".join(x for x in (_date(p.get("date")), tr.n("1 line", "%(n)s lines", len(lines)), tr("paid with %(how)s", how=paid)) if x)
        total = Decimal(p["total"])
        priced = sum((Decimal(x["amount"]) for x in lines), Decimal(0))
        # Amazon instalments: each monthly charge is its own Sure transaction; the order total is what was charged so far
        note = (tr("charged so far, of %(priced)s in products", priced=money(priced)) if priced > total and ev.provider == "amazon_it"
                else " + ".join(f"{money(x['amount'])} {tr(METHOD.get(x['method'], x['method']))}" for x in pays))
    elif ev is not None:
        p = ev.payload
        title, meta, total = tr("Pay in 3 · %(merchant)s", merchant=p.get("merchant") or tr("purchase")), _date(p.get("date")), Decimal(p["total"])
        note = tr("%(amount)s to pay", amount=money(p["remaining"])) if Decimal(p.get("remaining", "0")) else tr("paid")
    elif first.kind == "create":
        p = first.payload
        title = tr("Edenred · new movement") if first.provider == "edenred_it" else tr("New transaction")
        meta = " · ".join(x for x in (_date(p.get("date")), p.get("name")) if x)
        total = Decimal(p["amount"]) if "amount" in p else None
        note = tr("expense") if p.get("nature") == "expense" else tr("income")
    return Group(key, first.provider, ops, title, meta, total, note, list(legs.values()), minis)


def purchase_title(ev: Evidence, tr: Tr) -> str:
    p = ev.payload
    if ev.provider == "aliexpress":
        return tr("AliExpress · purchase of %(day)s", day=_date(p.get("date")))
    merchant = re.sub(r"^(\w+) \1\b", r"\1", p.get("merchant") or "Amazon")  # "Esselunga Esselunga di Centro"
    if ev.provider == "amazon_it":
        return tr("%(merchant)s · order %(id)s", merchant=merchant, id=p.get("id", ev.key))
    return tr("%(merchant)s · receipt %(id)s", merchant=merchant, id=p.get("id", ev.key))


def _date(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else ""


def planned_summary(groups: list[Group], names: dict[str, str]) -> str:
    counts: dict[str, int] = {}
    for g in groups:
        counts[g.provider] = counts.get(g.provider, 0) + 1
    return " · ".join(f"{n} {names.get(p, p)}" for p, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def done_since(db: Session, user_id: int, days: int) -> tuple[int, int]:
    """Purchases/movements written in the last `days` days, and how many product lines their splits created."""
    ops = db.scalars(select(SureOp).where(SureOp.user_id == user_id, SureOp.status == "done", SureOp.updated_at >= now() - timedelta(days=days))).all()
    return len({o.group_key for o in ops}), sum(len(o.payload.get("splits", [])) for o in ops if o.kind == "split")


# Why an item is held, from the issue the matcher wrote. "wait": nothing to decide, it resolves on its own
# (or by syncing again). (pattern, text, kind); the text's %(…)s come from the pattern's named groups.
REASONS = [
    (re.compile(r"(?P<method>\w+) €(?P<amount>[\d.-]+): (?P<n>\d+) candidates, needs review"),
     "%(method)s %(amount)s: %(n)s possible transactions in Sure, choose the right one", "choose"),
    (re.compile(r"(?P<method>\w+) €(?P<amount>[\d.-]+): no Sure transaction yet"),
     "%(method)s %(amount)s: no matching transaction in Sure, for now", "wait"),
    (re.compile(r"not linked yet"), "Waiting to be linked with Sure", "wait"),
    (re.compile(r"pending: written once posted"), "Movement not posted by Edenred yet", "wait"),
    (re.compile(r"no receipt PDF: bank payment left as is"), "Receipt without PDF: the payment in Sure stays as it is", "wait"),
    (re.compile(r"Esselunga served an empty receipt PDF"), "Esselunga served an empty receipt", "wait"),
    (re.compile(r"one charge of €(?P<amount>[\d.]+) covers (?P<n>\d+) orders.*"),
     "One charge of %(amount)s covers %(n)s orders: not split automatically", "decide"),
    (re.compile(r"identical twin.*"), "Two identical movements: approve them in To approve if both are real", "decide"),
    (re.compile(r"wallet '.*' not mapped.*"), "The voucher account is missing: choose it in Connections", "decide"),
]


def reasons(issue: str, tr: Tr = EN) -> list[tuple[str, str]]:
    """(text, kind) per part of an issue; unknown wordings are shown as they are, as decisions."""
    out = []
    for part in issue.split("; "):
        for rx, text, kind in REASONS:
            if m := rx.fullmatch(part):
                values = m.groupdict()
                if "method" in values:
                    values["method"] = tr(METHOD.get(values["method"], values["method"])).capitalize()
                if "amount" in values:
                    values["amount"] = eur(values["amount"], tr)
                out.append((tr(text, **values), kind))
                break
        else:
            out.append((part, "decide"))
    return out


@dataclass
class HeldItem:
    ev: Evidence
    title: str
    meta: str
    total: Decimal | None
    reasons: list[tuple[str, str]]
    choices: list[dict]  # per ambiguous leg: {"leg": index, "label": ..., "candidates": [...]}

    @property
    def waiting(self) -> bool:
        return all(kind == "wait" for _, kind in self.reasons)


def held(db: Session, user_id: int, tr: Tr = EN) -> list[HeldItem]:
    out = []
    for ev in db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.issue.is_not(None)).order_by(Evidence.last_seen.desc())):
        p = ev.payload
        if ev.kind == "purchase":
            title = purchase_title(ev, tr)
            meta, total = _date(p.get("date")) or (p.get("date") or ""), Decimal(p["total"]) if p.get("total") else None
        elif ev.kind == "voucher_movement":
            title, meta = tr("Edenred · %(description)s", description=p.get("description", ev.key)), _date(p.get("booked_on"))
            total = Decimal(p["amount"]) if p.get("amount") else None
        else:
            title, meta = tr("Pay in 3 · %(merchant)s", merchant=p.get("merchant") or tr("purchase")), _date(p.get("date"))
            total = Decimal(p["total"]) if p.get("total") else None
        pays = p.get("payments", [])
        choices = [{"leg": int(i), "label": f"{tr(METHOD.get(pays[int(i)]['method'], ''))} {eur(pays[int(i)]['amount'], tr)}" if int(i) < len(pays) else "",
                    "candidates": cands, "picked": (p.get("picks") or {}).get(i)}
                   for i, cands in sorted((p.get("candidates") or {}).items())]
        out.append(HeldItem(ev, title, meta, total, reasons(ev.issue, tr), choices))
    return out


def needs_owner(db: Session, user_id: int) -> int:
    """Held items waiting on a decision (not just on time), not ignored: the On hold badge."""
    return sum(1 for h in held(db, user_id) if not h.waiting and not h.ev.payload.get("ignored"))
