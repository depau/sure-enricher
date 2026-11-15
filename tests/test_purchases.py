from datetime import date
from decimal import Decimal as D

from conftest import FakeSure, conn
from sqlalchemy import select

from spe import purchases, writer
from spe.db import Evidence, SureOp
from spe.purchases import Line, LegMatch, Payment, Purchase

EDENRED = "e0000000-0000-0000-0000-000000000000"
CARD = "c0000000-0000-0000-0000-000000000000"
WALLETS = {purchases.VOUCHER: {EDENRED}}


def approve_and_apply(db, sure):
    for op in db.scalars(select(SureOp).where(SureOp.status == "planned")):
        op.status = "approved"
    db.commit()
    return writer.apply(db, sure.client(), 1)


def esselunga_48():
    return Purchase(
        provider="esselunga",
        id="R-2026-11-03-0042",
        merchant="Esselunga",
        date=date(2026, 11, 3),
        total=D("48.00"),
        payments=(Payment("edenred", D("24.00")), Payment("card", D("24.00"))),
        lines=(
            Line("PASTA", D("12.00")),
            Line("DETERSIVO", D("6.00"), voucher_eligible=False),
            Line("FRUTTA", D("31.50")),
            Line("SCONTO FIDATY", D("-1.50")),  # folded into FRUTTA: 30.00
        ),
        keywords=("ESSELUNGA",),
    )


def children(sure, parent):
    return [(r["name"], r["amount_decimal"], r["category"]) for r in sure.rows if r.get("parent_id") == parent["id"]]


def test_esselunga_mixed_payment_becomes_product_entries_on_both_legs(db):
    sure = FakeSure()
    voucher_leg = sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO", source="sure-purchase-enricher:edenred-it")
    card_leg = sure.add(CARD, "2026-11-04", "24.0", "ESSELUNGA SPA MILANO")  # settled next day
    sure.add(CARD, "2026-11-03", "24.0", "BAR CENTRALE")  # same amount, wrong merchant: must not match
    spending_before = sure.spending({EDENRED, CARD})

    purchases.record(db, conn(db, "esselunga"), esselunga_48())
    db.commit()
    assert purchases.reconcile(db, sure.client(), WALLETS, 1) == "1/1 purchases linked"
    ops = db.scalars(select(SureOp).order_by(SureOp.id)).all()
    assert [(o.kind, o.transaction_id) for o in ops] == [
        ("update", voucher_leg["id"]), ("split", voucher_leg["id"]), ("update", card_leg["id"]), ("split", card_leg["id"]),
    ]  # no create: the €48 total is never booked as a third expense

    assert approve_and_apply(db, sure) == {"updated": 2, "split": 2}
    # vouchers take food first; the detergent goes on the card, with the rest of the fruit
    assert children(sure, voucher_leg) == [("PASTA", "12.00", None), ("FRUTTA (parte)", "12.00", None)]
    assert children(sure, card_leg) == [("DETERSIVO", "6.00", None), ("FRUTTA (parte)", "18.00", None)]
    assert sure.family_syncs == 1  # children are uncategorized: Sure's rules (its classifier) run next
    assert sure.spending({EDENRED, CARD}) == spending_before  # split parents are excluded, children carry the €48
    assert "R-2026-11-03-0042" in card_leg["notes"]

    purchases.reconcile(db, sure.client(), WALLETS, 1)  # re-running plans nothing new
    assert db.scalar(select(SureOp).where(SureOp.status == "planned")) is None


def test_ambiguous_card_leg_waits_for_the_owner_to_pick_it(db):
    sure = FakeSure()
    sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO")
    sure.add(CARD, "2026-11-04", "24.0", "ESSELUNGA SPA")
    sure.add(CARD, "2026-11-05", "24.0", "ESSELUNGA SPA")  # two identical candidates
    shop = conn(db, "esselunga")
    purchases.record(db, shop, esselunga_48())
    db.commit()
    assert purchases.reconcile(db, sure.client(), WALLETS, 1) == "0/1 purchases linked"
    ev = db.scalar(select(Evidence).filter_by(key="R-2026-11-03-0042"))
    assert ev.issue == "card €24.00: 2 candidates, needs review"
    assert db.scalar(select(SureOp)) is None

    # In sospeso: the owner picks the second charge; the next read of the receipt keeps that choice
    first, second = ev.payload["candidates"]["1"]
    ev.payload = {**ev.payload, "picks": {"1": second["id"]}}
    purchases.record(db, shop, esselunga_48())
    db.commit()
    assert ev.payload["picks"] == {"1": second["id"]}
    assert purchases.reconcile(db, sure.client(), WALLETS, 1) == "1/1 purchases linked"
    touched = {o.transaction_id for o in db.scalars(select(SureOp))}
    assert second["id"] in touched and first["id"] not in touched


def test_ignored_purchase_is_left_alone(db):
    sure = FakeSure()
    shop = conn(db, "esselunga")
    purchases.record(db, shop, esselunga_48())
    ev = db.scalar(select(Evidence))
    ev.payload = {**ev.payload, "ignored": True}
    sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO")
    sure.add(CARD, "2026-11-04", "24.0", "ESSELUNGA SPA")
    db.commit()
    assert purchases.reconcile(db, sure.client(), WALLETS, 1) == "nothing to reconcile"


def test_unpaid_leg_keeps_purchase_held_and_nothing_written(db):
    sure = FakeSure()
    sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO")
    purchases.record(db, conn(db, "esselunga"), esselunga_48())
    db.commit()
    purchases.reconcile(db, sure.client(), WALLETS, 1)
    assert db.scalar(select(Evidence).filter_by(key="R-2026-11-03-0042")).issue == "card €24.00: no Sure transaction yet"
    assert db.scalar(select(SureOp)) is None


def test_amazon_charges_spread_over_items_by_price(db):
    sure = FakeSure()
    first = sure.add(CARD, "2026-10-02", "40.0", "AMAZON EU SARL")
    second = sure.add(CARD, "2026-10-05", "60.0", "AMZN Mktp IT")
    refund = sure.add(CARD, "2026-10-12", "-10.0", "AMAZON EU rimborso")
    order = Purchase(
        provider="amazon_it", id="405-1234567-1234567", merchant="Amazon.it", date=date(2026, 10, 1), total=D("90.00"),
        payments=(Payment("card", D("40.00"), on=date(2026, 10, 2)), Payment("card", D("60.00"), on=date(2026, 10, 5)),
                  Payment("card", D("-10.00"), on=date(2026, 10, 12))),
        lines=(Line("USB hub", D("30.00")), Line("Libro", D("80.00"))),  # €110 of items, €100 charged (coupon)
        keywords=("AMAZON", "AMZN"),
    )
    purchases.record(db, conn(db, "amazon_it"), order)
    db.commit()
    assert purchases.reconcile(db, sure.client(), {purchases.VOUCHER: set()}, 1) == "1/1 purchases linked"
    assert approve_and_apply(db, sure) == {"updated": 3, "split": 2}
    assert children(sure, first) == [("USB hub", "10.90", None), ("Libro", "29.10", None)]
    assert children(sure, second) == [("USB hub", "16.36", None), ("Libro", "43.64", None)]
    assert not refund["split_parent"] and order.id in refund["notes"]  # refunds get notes only
    assert sure.spending({CARD}) == D("90.00")


def test_edit_in_sure_after_planning_is_not_overwritten(db):
    sure = FakeSure()
    sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO")
    card_leg = sure.add(CARD, "2026-11-04", "24.0", "ESSELUNGA SPA")
    purchases.record(db, conn(db, "esselunga"), esselunga_48())
    db.commit()
    purchases.reconcile(db, sure.client(), WALLETS, 1)
    card_leg["notes"] = "my own note"  # owner edits in Sure before approving
    stats = approve_and_apply(db, sure)
    assert stats["failed"] == 2  # the card notes and its split both refuse
    assert card_leg["notes"] == "my own note" and not card_leg["split_parent"]


def test_hand_edited_transactions_are_not_split():
    p = esselunga_48()
    mine = {"id": "t", "notes": p.marker, "category": {"id": "c"}, "user_modified": True, "split_parent": False}
    assert purchases.plan_ops(p, [LegMatch(Payment("card", D("48.00")), mine, [], True)]) == []


def test_split_children_carry_the_merchant_and_parent_tags_and_old_splits_get_them_on_approval(db):
    sure = FakeSure()
    tagged = sure.add(EDENRED, "2026-11-03", "24.0", "ESSELUNGA MILANO", tags=[sure.tags[0]])
    card_leg = sure.add(CARD, "2026-11-04", "24.0", "ESSELUNGA SPA MILANO", merchant=sure.merchants[0])
    purchases.record(db, conn(db, "esselunga"), esselunga_48())
    purchases.reconcile(db, sure.client(), WALLETS, 1)
    approve_and_apply(db, sure)
    kids = [r for r in sure.rows if r.get("parent_id")]
    assert {r["merchant"]["name"] for r in kids} == {"Esselunga"}  # a family merchant, created once, not the bank's raw one
    assert [m["name"] for m in sure.merchants].count("Esselunga") == 1
    assert {r["parent_id"]: [t["id"] for t in r["tags"]] for r in kids} == {tagged["id"]: ["t-family"], card_leg["id"]: []}
    purchases.backfill_split_metadata(db, 1)
    assert db.scalar(select(SureOp).where(SureOp.status == "planned")) is None  # new splits need no backfill

    # A split made before this existed: children inherited their parent's merchant (often none), no tags, and the
    # classifier categorized one since.
    for op in db.scalars(select(SureOp).where(SureOp.kind == "split")):
        op.payload = {"splits": op.payload["splits"]}
    for r in kids:
        r["merchant"], r["tags"] = sure.by_id(r["parent_id"])["merchant"], []
    kids[0]["category"] = sure.categories[0]
    purchases.backfill_split_metadata(db, 1)
    db.commit()
    assert [o.kind for o in db.scalars(select(SureOp).where(SureOp.status == "planned"))] == ["resplit", "resplit"]
    sure.rows = [r for r in sure.rows if r is not kids[-1]] + [{**kids[-1], "name": "FRUTTA MODIFICATA"}]  # card split edited by hand

    assert approve_and_apply(db, sure) == {"resplit": 1, "failed": 1}
    kids = [r for r in sure.rows if r.get("parent_id") == tagged["id"]]
    assert [(r["name"], r["merchant"]["name"], [t["id"] for t in r["tags"]], r["category"]) for r in kids] == [
        ("PASTA", "Esselunga", ["t-family"], sure.categories[0]), ("FRUTTA (parte)", "Esselunga", ["t-family"], None)]
    failed = db.scalar(select(SureOp).where(SureOp.status == "failed"))
    assert failed.transaction_id == card_leg["id"] and "changed in Sure" in failed.error
    purchases.backfill_split_metadata(db, 1)  # nothing re-queued: one re-split per old split
    assert db.scalar(select(SureOp).where(SureOp.status == "planned")) is None
