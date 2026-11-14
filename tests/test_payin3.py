from datetime import date
from decimal import Decimal
from decimal import Decimal as D

from conftest import FakeSure, conn

from spe import payin3

PP = "pp000000-0000-0000-0000-000000000000"


def plan_rows(sure, day, total, shares, merchant="DHL Express", upto=3):
    sure.add(PP, day, total, merchant)
    sure.add(PP, day, f"-{total}", "Pagamento con credito acquirenti PayPal")
    for k, amount in enumerate(shares[:upto]):
        due = payin3.add_months(date.fromisoformat(day), k).isoformat()
        sure.add(PP, due, amount, "PayPal (Europe) S.à r.l. et Cie, SCA")
        sure.add(PP, due, f"-{amount}", "Transfer to PayPal", transfer={"id": "t"})


def test_300_euro_plan_tracks_remaining_debt():
    sure = FakeSure()
    plan_rows(sure, "2026-08-10", "300.0", ["100.0"] * 3, upto=2)
    sure.add(PP, "2026-08-10", "100.0", "Some other shop")  # same day, different amount
    [p] = payin3.detect(sure.rows, today=date(2026, 9, 15))
    assert (p.issue, p.merchant["name"], p.total, p.paid, p.remaining) == (None, "DHL Express", D("300"), D("200"), D("100"))


def test_uneven_cents_and_two_concurrent_plans():
    sure = FakeSure()
    plan_rows(sure, "2026-01-07", "199.57", ["66.52", "66.53", "66.52"])
    plan_rows(sure, "2026-01-15", "79.90", ["26.64", "26.63", "26.63"], merchant="Zalando")
    plans = payin3.detect(sure.rows, today=date(2026, 6, 1))
    assert [(p.issue, p.remaining, len(p.repayments)) for p in plans] == [(None, D(0), 3), (None, D(0), 3)]


def test_missing_installment_and_other_credit_plans_are_flagged_not_tracked():
    sure = FakeSure()
    plan_rows(sure, "2026-03-01", "90.0", ["30.0"] * 3, upto=2)
    [p] = payin3.detect(sure.rows, today=date(2026, 5, 20))
    assert p.issue == "installment 3 due 2026-05-01 not found in the PayPal feed"

    sure = FakeSure()  # a 12-month interest-bearing credit: out of scope, must not look like Pay in 3
    plan_rows(sure, "2026-03-01", "90.0", ["8.10"] * 12, upto=3)
    [p] = payin3.detect(sure.rows, today=date(2026, 6, 1))
    assert p.issue == "installment 1 due 2026-03-01 not found in the PayPal feed" and not p.repayments


def test_partial_refund_respreads_remaining_installments():
    sure = FakeSure()  # €42.27 plan, €7.79 refunded after the first installment: 14.09 then 10.19 + 10.20
    plan_rows(sure, "2026-01-11", "42.27", ["14.09", "10.19", "10.20"], merchant="AliExpress")
    sure.add(PP, "2026-01-25", "-7.79", "AliExpress")
    sure.add(PP, "2026-01-25", "7.79", "Pagamento credito acquirenti generico")
    [p] = payin3.detect(sure.rows, today=date(2026, 4, 1))
    assert (p.issue, p.reduced, p.paid, p.remaining) == (None, D("7.79"), D("34.48"), D(0))


def test_cancelled_plan_gives_back_the_first_installment():
    sure = FakeSure()
    plan_rows(sure, "2026-07-05", "288.49", ["96.16"] * 3, merchant="Subito.it", upto=1)
    sure.add(PP, "2026-07-05", "-288.49", "Subito.it")
    sure.add(PP, "2026-07-05", "288.49", "Pagamento credito acquirenti generico")
    sure.add(PP, "2026-07-06", "-96.16", "PayPal (Europe) S.à r.l. et Cie, SCA")
    [p] = payin3.detect(sure.rows, today=date(2026, 10, 1))
    assert (p.issue, p.cancelled, p.paid, p.remaining) == (None, True, D(0), D(0))


def test_writeback_stages_the_validated_recipe_and_is_idempotent(db):
    from sqlalchemy import select

    from spe import writer
    from spe.db import SureOp
    from spe.providers import Context
    from spe.providers.paypal_payin3 import PayIn3
    from spe.settings import Settings

    loan, cat = "loan0000-0000-0000-0000-000000000000", {"id": "c-electronics", "name": "Electronics"}
    sure = FakeSure()
    sure.categories.append(cat)
    today = date.today()
    day = payin3.add_months(today, -1).isoformat()
    plan_rows(sure, day, "300.0", ["100.0"] * 3, upto=2)
    merchant = sure.rows[0]
    merchant["category"] = cat
    ctx = Context(db, sure.client(), Settings("http://sure.test", "s"), conn(db, "paypal_payin3", paypal_account=PP, loan_account=loan))

    PayIn3().sync(ctx)
    ops = db.scalars(select(SureOp).order_by(SureOp.id)).all()
    assert [o.kind for o in ops] == ["mcp", "mcp", "mcp", "update", "update", "split"]
    assert ops[-1].payload["splits"] == [{"name": "DHL Express (Pay in 3: spesa ripartita sulle rate)", "amount": "300.0",
                                          "category_id": "c-electronics", "excluded": True}]
    for o in ops:
        o.status = "approved"
    db.commit()
    assert writer.apply(db, sure.client(), 1) == {"match_transfer": 3, "updated": 2, "split": 1}
    loan_balance = sum(Decimal(r["amount_decimal"]) for r in sure.rows if r["account"]["id"] == loan)
    assert loan_balance == D("100.0")  # +300 opened, -100 -100 repaid
    assert all(r["category"] == cat for r in sure.rows if r["name"].startswith("PayPal (Europe)"))

    PayIn3().sync(ctx)  # next run: everything already linked/annotated/split
    assert db.scalar(select(SureOp).where(SureOp.status == "planned")) is None


def test_excluded_rows_block_writeback_with_an_actionable_issue(db):
    from spe.db import Evidence
    from spe.providers import Context
    from spe.providers.paypal_payin3 import PayIn3
    from spe.settings import Settings

    sure = FakeSure()
    plan_rows(sure, payin3.add_months(date.today(), -1).isoformat(), "300.0", ["100.0"] * 3, upto=2)
    sure.rows[0]["category"] = {"id": "c", "name": "Shopping"}
    sure.rows[1]["excluded"] = True  # how the owner handles the credit inflow today
    PayIn3().sync(Context(db, sure.client(), Settings("http://sure.test", "s"), conn(db, "paypal_payin3", paypal_account=PP, loan_account="loan")))
    [ev] = db.query(Evidence).all()
    assert ev.issue.startswith("un-exclude in Sure")
