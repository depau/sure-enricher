from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import FakeSure, conn
from sqlalchemy import select

from spe import vouchers, writer
from spe.db import Connection, Evidence, SureOp
from spe.sure import SureError
from spe.vouchers import Kind, VoucherMovement

ACCOUNT = "11111111-1111-1111-1111-111111111111"
FIXTURE = Path(__file__).parent / "fixtures" / "edenred_movements.json"
ACCOUNTS = {"card": ACCOUNT}


def movements():
    return vouchers.load_fixture(FIXTURE)


def edenred(db):
    return db.scalar(select(Connection).filter_by(provider="edenred_it")) or conn(db, "edenred_it")


def stage_all(db):
    """Stage with auto-apply, then approve the identical twins as a human would."""
    vouchers.stage(db, edenred(db), movements(), ACCOUNTS, auto_apply=True)
    for op in db.scalars(select(SureOp).where(SureOp.status == "planned")):
        op.status = "approved"
    db.commit()


def test_plan_keeps_identical_twins_and_holds_unsettled_kinds():
    creates, held = vouchers.plan(movements(), ACCOUNTS)
    assert len({c.external_id for c in creates}) == len(creates) == 5  # R-1 seen twice = one refund
    assert sum(c.collision for c in creates) == 2  # two identical €8 lunches: two expenses, flagged
    assert {h.movement.kind for h in held} == {Kind.EXPIRY, Kind.REDEMPTION}  # expiry + a pending redemption
    issuance = next(c for c in creates if c.movement.kind == Kind.ISSUANCE)
    assert issuance.payload["nature"] == "income" and issuance.payload["amount"] == "160.00"


def test_keys_stable_across_refetch_and_representation():
    a = VoucherMovement("card", date(2026, 11, 3), Decimal("24"), Kind.REDEMPTION, "ESSELUNGA  Milano")
    b = VoucherMovement("card", date(2026, 11, 3), Decimal("24.00"), Kind.REDEMPTION, "esselunga milano")
    assert vouchers.external_ids([a]) == vouchers.external_ids([b])
    assert sorted(vouchers.external_ids(movements())) == sorted(vouchers.external_ids(list(reversed(movements()))))


def test_twins_wait_for_approval_and_held_items_are_visible(db):
    vouchers.stage(db, edenred(db), movements(), ACCOUNTS, auto_apply=True)
    statuses = sorted(op.status for op in db.scalars(select(SureOp)))
    assert statuses == ["approved"] * 3 + ["planned"] * 2
    issues = {e.issue for e in db.scalars(select(Evidence).where(Evidence.issue.is_not(None)))}
    assert "pending: written once posted" in issues and "expiry: Sure semantics not settled yet" in issues


def test_replay_writes_nothing_twice(db):
    sure = FakeSure()
    stage_all(db)
    assert writer.apply(db, sure.client(), 1)["created"] == 5
    posts = sure.posts
    stage_all(db)  # next sync sees the same movements
    assert writer.apply(db, sure.client(), 1) == {}
    assert sure.posts == posts and len(sure.rows) == 5
    # Sure stores inflows negative: wallet gained 160 issued - 24 - 8 - 8 + 3.50 refund
    assert sum(Decimal(r["amount_decimal"]) for r in sure.rows) == Decimal("-123.50")


@pytest.mark.parametrize("honours_idempotency", [True, False])
def test_crash_between_sure_write_and_local_commit(db, honours_idempotency):
    sure = FakeSure(honours_idempotency)
    stage_all(db)
    op, *others = db.scalars(select(SureOp).order_by(SureOp.id))
    for o in others:
        o.status = "planned"
    # Simulate: POST landed in Sure (keyed: by Sure, or by an older build that kept the fields),
    # then the process died before marking the op done.
    sure.rows.append({**op.payload, "id": "pre-existing", "amount_decimal": op.payload["amount"], "account": {"id": ACCOUNT}})
    op.status = "sending"
    db.commit()

    assert writer.apply(db, sure.client(), 1) == {"recovered": 1}
    assert sure.posts == 0 and len(sure.rows) == 1


def test_sure_dropping_idempotency_key_stops_writes(db):
    sure = FakeSure(honours_idempotency=False)
    stage_all(db)
    with pytest.raises(writer.IdempotencyUnsupported):
        writer.apply(db, sure.client(), 1)
    assert len(sure.rows) == 1
    assert db.scalars(select(SureOp).order_by(SureOp.id)).first().status == "done"  # never re-sent


def test_server_error_keeps_op_queued_and_retry_does_not_duplicate(db):
    sure = FakeSure()
    stage_all(db)
    sure.fail_next = 503
    with pytest.raises(SureError):
        writer.apply(db, sure.client(), 1)
    assert writer.apply(db, sure.client(), 1)["created"] == 5
    assert len(sure.rows) == 5


def test_validation_error_needs_a_human(db):
    sure = FakeSure()
    stage_all(db)
    sure.fail_next = 422
    assert writer.apply(db, sure.client(), 1)["failed"] == 1
    assert writer.apply(db, sure.client(), 1) == {}  # failed ops are not retried until the owner says so


def test_voucher_spending_gets_the_shop_as_merchant_and_older_rows_get_it_on_approval(db):
    sure = FakeSure()
    lunch = VoucherMovement("card", date(2026, 11, 12), Decimal("8.00"), Kind.REDEMPTION, "LA PIADINERIA - MILANO", source_id="L1")
    topup = VoucherMovement("card", date(2026, 11, 1), Decimal("160.00"), Kind.ISSUANCE, "Ordine di BUONI/VOUCHER su CLOUD", source_id="T1")
    vouchers.stage(db, edenred(db), [lunch, topup], ACCOUNTS, auto_apply=True)
    assert writer.apply(db, sure.client(), 1)["created"] == 2
    assert {r["name"]: (r["merchant"] or {}).get("name") for r in sure.rows} == {"LA PIADINERIA - MILANO": "La Piadineria",
                                                                                 "Ordine di BUONI/VOUCHER su CLOUD": None}

    # rows created before merchants existed: one is still bare, another got a merchant by hand in Sure meanwhile
    old = VoucherMovement("card", date(2026, 10, 2), Decimal("24.00"), Kind.REDEMPTION, "ESSELUNGA - MILANO - CENTRO", source_id="E1")
    hand = VoucherMovement("card", date(2026, 10, 3), Decimal("8.00"), Kind.REDEMPTION, "TO MARKET - MILANO", source_id="M1")
    vouchers.stage(db, edenred(db), [old, hand], ACCOUNTS, auto_apply=True)
    for op in db.scalars(select(SureOp).where(SureOp.status == "approved")):
        op.payload = {k: v for k, v in op.payload.items() if k != "merchant"}
    writer.apply(db, sure.client(), 1)
    by_name = {r["name"]: r for r in sure.rows}
    by_name["TO MARKET - MILANO"]["merchant"] = sure.merchants[0]
    vouchers.backfill_merchants(db, 1)
    vouchers.backfill_merchants(db, 1)  # queued once
    planned = db.scalars(select(SureOp).where(SureOp.status == "planned")).all()
    assert sorted(o.payload["merchant"] for o in planned) == ["Esselunga", "To Market"]
    for op in planned:
        op.status = "approved"
    db.commit()
    assert writer.apply(db, sure.client(), 1) == {"updated": 1, "failed": 1}
    assert by_name["ESSELUNGA - MILANO - CENTRO"]["merchant"]["name"] == "Esselunga"
    assert by_name["TO MARKET - MILANO"]["merchant"] == sure.merchants[0]  # chosen in Sure: left as is
