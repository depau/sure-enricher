"""PayPal "Paga in 3 rate" plans, detected from the PayPal account Sure already syncs (Enable Banking).

Feed shape (owner's data, 2024-2025), amounts in Sure's sign (outflow positive):
  open      -X  "Pagamento con credito acquirenti PayPal"   PayPal credit funds a purchase of X
            +X  <merchant>                                  the purchase itself, same day
  repay     +a  "PayPal (Europe) S.à r.l. et Cie, SCA"      installment k, at day 0 / +1 / +2 months,
                                                            funded by a card debit "PAYPAL *PAGA IN 3 RATE"
  reduce    +r  "Pagamento credito acquirenti generico"     credit cut by a merchant refund (-r, same day);
                                                            r == X cancels the plan
  refund    -a  "PayPal (Europe) S.à r.l. et Cie, SCA"      an installment given back after a cancellation
After a reduction PayPal spreads what is left evenly over the remaining installments.
Only interest-free 3-installment plans are tracked; anything that does not replay cleanly needs review.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

OPEN = "pagamento con credito acquirenti paypal"
REDUCE = "pagamento credito acquirenti generico"
REPAY = "paypal (europe)"
INSTALLMENTS = 3
WINDOW = timedelta(days=6)  # tolerance around each monthly due date
SAME_DAY = timedelta(days=1)
CENT = Decimal("0.01")


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(d.year + y, m + 1, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _name(tx: dict) -> str:
    return (tx.get("name") or "").casefold()


def _d(tx: dict) -> date:
    return date.fromisoformat(tx["date"])


def _a(tx: dict) -> Decimal:
    return Decimal(tx["amount_decimal"])


@dataclass
class Plan:
    credit: dict  # the credit-opening inflow: its id is the plan id
    merchant: dict | None = None
    repayments: list[dict] = field(default_factory=list)
    reductions: list[dict] = field(default_factory=list)  # credit cuts after merchant refunds
    refunds: list[dict] = field(default_factory=list)  # installments given back
    issue: str | None = None

    @property
    def id(self) -> str:
        return self.credit["id"]

    @property
    def day(self) -> date:
        return _d(self.credit)

    @property
    def total(self) -> Decimal:
        return -_a(self.credit)

    @property
    def reduced(self) -> Decimal:
        return sum((_a(t) for t in self.reductions), Decimal(0))

    @property
    def paid(self) -> Decimal:
        """Net installments paid (repayments minus installments given back)."""
        return sum((_a(t) for t in self.repayments), Decimal(0)) + sum((_a(t) for t in self.refunds), Decimal(0))

    @property
    def remaining(self) -> Decimal:
        return self.total - self.reduced - self.paid

    @property
    def cancelled(self) -> bool:
        return self.reduced == self.total

    def due(self, k: int) -> date:
        return add_months(self.day, k)

    def expected(self) -> Decimal:
        """Next installment: what is left spread over the installments left."""
        return self.remaining / (INSTALLMENTS - len(self.repayments))


def detect(txs: list[dict], today: date) -> list[Plan]:
    """Replay the PayPal feed chronologically. txs must cover each plan's day 0 .. +2 months (+window)."""
    def order(t: dict) -> tuple:  # same day: open, then the day-0 installment, then reductions, then refunds
        n, a = _name(t), _a(t)
        rank = 0 if OPEN in n else 1 if REPAY in n and a > 0 else 2 if REDUCE in n else 3
        return t["date"], rank, t["id"]

    txs = sorted(txs, key=order)
    plans: list[Plan] = []
    used: set[str] = set()

    def flag(p: Plan, why: str) -> None:
        p.issue = p.issue or why

    for t in txs:
        n, amount = _name(t), _a(t)
        if OPEN in n and amount < 0:
            p = Plan(t)
            plans.append(p)
            merchants = [
                m for m in txs
                if m["id"] not in used and not m.get("transfer") and REPAY not in _name(m) and "credito acquirenti" not in _name(m)
                and _a(m) == p.total and abs(_d(m) - p.day) <= SAME_DAY
            ]
            if len(merchants) == 1:
                p.merchant = merchants[0]
                used.add(p.merchant["id"])
            else:
                flag(p, f"{len(merchants)} merchant purchases of €{p.total} around {p.day}: needs review")
        elif REDUCE in n and amount > 0:
            refunds = [m for m in txs if _a(m) == -amount and not m.get("transfer") and abs(_d(m) - _d(t)) <= SAME_DAY and m["id"] not in used]
            owners = [
                p for p in plans
                if not p.issue and p.total - p.reduced >= amount
                and (amount == p.total or any(_name(m) == _name(p.merchant) for m in refunds))
            ]
            if len(owners) == 1:
                owners[0].reductions.append(t)
            else:
                for p in owners:
                    flag(p, f"credit reduction €{amount} on {t['date']} matches {len(owners)} plans: needs review")
        elif REPAY in n and amount > 0:
            owners = [
                p for p in plans
                if not p.issue and len(p.repayments) < INSTALLMENTS and p.remaining > 0
                and abs(_d(t) - p.due(len(p.repayments))) <= WINDOW and abs(amount - p.expected()) <= CENT
            ]
            if len(owners) == 1:
                owners[0].repayments.append(t)
            elif owners:
                for p in owners:
                    flag(p, f"installment €{amount} on {t['date']} matches {len(owners)} plans: needs review")
        elif REPAY in n and amount < 0:
            owners = [p for p in plans if not p.issue and p.cancelled and p.paid >= -amount]
            if len(owners) == 1:
                owners[0].refunds.append(t)
            else:
                for p in owners:
                    flag(p, f"installment refund €{-amount} on {t['date']} matches {len(owners)} plans: needs review")

    for p in plans:
        if p.issue or p.remaining == 0:
            continue
        k = len(p.repayments)
        if p.remaining < 0:
            flag(p, f"paid €{p.paid} exceeds the credit: not an interest-free Pay in 3 plan, unsupported")
        elif k == INSTALLMENTS:
            flag(p, f"€{p.remaining} left after {INSTALLMENTS} installments: unsupported plan")
        elif p.due(k) + WINDOW < today:
            flag(p, f"installment {k + 1} due {p.due(k)} not found in the PayPal feed")
    return plans
