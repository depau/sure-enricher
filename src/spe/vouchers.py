"""Wallet movements (Edenred meal vouchers, the Amazon gift card balance) -> planned Sure transactions.

Providers hand over normalized VoucherMovements; nothing here talks to a provider. The wallet's Sure account is the
enricher's: its movements are created without asking.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from spe import writer
from spe.db import Connection, Evidence, SureOp, record_evidence
from spe.sure import SureClient

ROME = ZoneInfo("Europe/Rome")
SURE_SOURCE = "sure-purchase-enricher:edenred-it"  # the project's first name: Sure dedupes on it, never change it
CENT = Decimal("0.01")


class Kind(StrEnum):
    ISSUANCE = "issuance"  # employer top-up: benefit inflow, not a transfer from a bank account
    REDEMPTION = "redemption"  # spent at a merchant
    REFUND = "refund"  # merchant reversal back into the wallet
    EXPIRY = "expiry"  # forfeited vouchers: not a purchase
    TRANSFER = "transfer"  # between supports of one pool (card <-> Cloud <-> smartphone): never written
    TOPUP = "topup"  # balance added from outside (a gift card code, a reload paid by card); Sure pairs it with a card payment itself
    OTHER = "other"


# Sure `nature` per kind. Kinds absent here are held: Sure's create API cannot set
# `kind`/`excluded`, so an expiry or internal move would show up as shopping or income.
NATURE = {Kind.ISSUANCE: "income", Kind.REDEMPTION: "expense", Kind.REFUND: "income", Kind.TOPUP: "income"}


@dataclass(frozen=True)
class VoucherMovement:
    wallet: str  # one economically distinct pool, mapped to one Sure account
    booked_on: date  # Europe/Rome calendar date
    amount: Decimal  # EUR value, positive; direction comes from kind
    kind: Kind
    description: str
    pending: bool = False
    source_id: str | None = None  # the source's own immutable id, when it has one
    occurred_at: datetime | None = None  # timezone-aware, when the source has a time
    merchant: str | None = None  # the shop it was spent at: a family merchant in Sure, for rules


@dataclass(frozen=True)
class PlannedCreate:
    movement: VoucherMovement
    payload: dict  # body for POST /api/v1/transactions under "transaction"
    collision: bool  # fingerprint shared with another movement: review, keys rely on occurrence order

    @property
    def external_id(self) -> str:
        return self.payload["external_id"]

    @property
    def account_id(self) -> str:
        return self.payload["account_id"]


@dataclass(frozen=True)
class Held:
    movement: VoucherMovement
    key: str
    reason: str


def external_ids(movements: list[VoucherMovement]) -> list[tuple[str, bool]]:
    """Stable dedupe key per movement, plus whether it is a fingerprint collision.

    With a source id the key is that id (repeats are the same movement, e.g. page overlap).
    Without one, the key is a fingerprint of every field plus an occurrence index, so two
    identical lunches on the same day stay two movements. The index is stable as long as
    each fetch covers whole days, so all identical twins are always seen together.
    """
    bases = []
    for m in movements:
        if m.source_id:
            bases.append(f"id:{m.wallet}:{m.source_id}")
            continue
        when = m.occurred_at.astimezone(UTC).isoformat() if m.occurred_at else m.booked_on.isoformat()
        fields = [m.wallet, when, str(m.amount.quantize(CENT)), m.kind, " ".join(m.description.casefold().split())]
        bases.append("fp:" + hashlib.sha256("\x1f".join(fields).encode()).hexdigest()[:32])

    totals = Counter(bases)
    seen: Counter[str] = Counter()
    out = []
    for b in bases:
        if b.startswith("id:"):
            out.append((b, False))
        else:
            out.append((f"{b}#{seen[b]}", totals[b] > 1))
            seen[b] += 1
    return out


def plan(movements: list[VoucherMovement], accounts: dict[str, str], source: str = SURE_SOURCE,
         label: str = "Edenred Ticket Restaurant") -> tuple[list[PlannedCreate], list[Held]]:
    """accounts maps wallet -> Sure account UUID."""
    creates: dict[str, PlannedCreate] = {}
    held = []
    for m, (ext_id, collision) in zip(movements, external_ids(movements), strict=True):
        if m.kind == Kind.TRANSFER:
            continue  # a wallet is the whole voucher pool: moving vouchers between its supports is not money
        if m.pending:
            held.append(Held(m, ext_id, "pending: written once posted"))
        elif m.kind not in NATURE:
            held.append(Held(m, ext_id, f"{m.kind}: Sure semantics not settled yet"))
        elif m.wallet not in accounts:
            held.append(Held(m, ext_id, f"wallet {m.wallet!r} not mapped to a Sure account"))
        elif m.amount <= 0 or m.amount != m.amount.quantize(CENT):
            held.append(Held(m, ext_id, f"amount {m.amount} is not a positive whole-cent value"))
        else:
            creates[ext_id] = PlannedCreate(
                movement=m,
                collision=collision,
                payload={
                    "account_id": accounts[m.wallet],
                    "date": m.booked_on.isoformat(),
                    "amount": str(m.amount.quantize(CENT)),
                    "currency": "EUR",
                    "nature": NATURE[m.kind],
                    "name": m.description,
                    "notes": f"{label} ({m.kind})",
                    "source": source,
                    "external_id": ext_id,
                } | ({"merchant": m.merchant} if m.merchant else {}),
            )
    return list(creates.values()), held


def stage(db: Session, conn: Connection, movements: list[VoucherMovement], accounts: dict[str, str], source: str = SURE_SOURCE,
          label: str = "Edenred Ticket Restaurant") -> str:
    """Record movements as evidence of the connection and queue their Sure creates. The wallet account belongs to
    the enricher, so new movements are written without asking; identical twins wait for approval."""
    creates, held = plan(movements, accounts, source, label)
    db.execute(delete(Evidence).where(Evidence.connection_id == conn.id, Evidence.issue.startswith("pending")))
    for c in creates:
        record_evidence(db, conn, c.external_id, "voucher_movement", _jsonable(c.movement),
                        "identical twin: approve if both are real" if c.collision else None)
        m = c.movement
        op = writer.stage_create(db, conn.provider, c.payload, f"{m.booked_on} {c.payload['nature']} {c.payload['amount']} EUR {m.description}",
                                 approve=not c.collision, user_id=conn.user_id)
        if op.status == "planned" and not c.collision:
            op.status = "approved"  # queued before wallet creates stopped asking
    for h in held:
        record_evidence(db, conn, h.key, "voucher_movement", _jsonable(h.movement), h.reason)
    db.commit()
    return f"{len(creates)} movements staged, {len(held)} held"


def balance_mismatch(sure: SureClient, account_id: str | None, balance: Decimal, who: str) -> str | None:
    """Compare the source's balance with the wallet account's in Sure; reported, never corrected."""
    if not account_id:
        return None
    acct = next((a for a in sure.accounts() if a["id"] == account_id), None)
    if acct is None:
        return f"Sure account {account_id} not found"
    in_sure = Decimal(acct["balance_cents"]) / 100
    if in_sure != balance:
        return f"{who} says €{balance}, Sure shows €{in_sure}: approve pending ops or fix the opening balance"
    return None


def merchant_name(description: str) -> str | None:
    """'LA PIADINERIA - MILANO' -> 'La Piadineria': the shop, without the city Edenred appends. Becomes a family
    merchant in Sure (spe.writer), so rules can target voucher spending by shop."""
    shop = description.split(" - ")[0].strip()
    return " ".join(w.capitalize() for w in shop.split()) or None


def backfill_merchants(db: Session, user_id: int | None) -> None:
    """Voucher transactions created before they carried a merchant: queue setting it, for approval. Caller commits."""
    for o in db.scalars(select(SureOp).where(SureOp.user_id == user_id, SureOp.kind == "create", SureOp.provider == "edenred_it",
                                             SureOp.status == "done")).all():
        kind = re.search(r"\((\w+)\)$", o.payload.get("notes") or "")
        tx_id = (o.response or {}).get("id")
        if "merchant" in o.payload or not tx_id or not kind or kind[1] == Kind.ISSUANCE or not (shop := merchant_name(o.payload["name"])):
            continue
        writer.stage(db, key=f"merchant:{tx_id}", kind="update", provider=o.provider, payload={"merchant": shop},
                     summary=f"Edenred {o.payload['date']} {o.payload['name']}: esercente {shop}", approve=False, user_id=user_id,
                     group=o.group_key, account_id=o.account_id, transaction_id=tx_id, before={"merchant": None, "name": o.payload["name"],
                                                                                              "amount_decimal": o.payload["amount"], "date": o.payload["date"]})


def _jsonable(m: VoucherMovement) -> dict:
    return {k: v if v is None or isinstance(v, (bool, str)) else str(v) for k, v in asdict(m).items()}


def load_fixture(path: Path) -> list[VoucherMovement]:
    """Normalized movements from JSON (tests / dry runs). Amounts must be strings: no floats."""
    out = []
    for raw in json.loads(path.read_text())["movements"]:
        if not isinstance(raw["amount"], str):
            raise ValueError(f"amount must be a decimal string, got {raw['amount']!r}")
        occurred = datetime.fromisoformat(raw["occurred_at"]) if raw.get("occurred_at") else None
        if occurred and occurred.tzinfo is None:
            raise ValueError(f"occurred_at must carry a UTC offset: {raw['occurred_at']}")
        booked = date.fromisoformat(raw["booked_on"]) if raw.get("booked_on") else occurred.astimezone(ROME).date()
        out.append(
            VoucherMovement(
                wallet=raw["wallet"],
                booked_on=booked,
                amount=Decimal(raw["amount"]),
                kind=Kind(raw["kind"]),
                description=raw["description"],
                pending=raw.get("pending", False),
                source_id=raw.get("source_id"),
                occurred_at=occurred,
            )
        )
    return out
