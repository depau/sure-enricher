import json
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest
from conftest import FakeSure, conn
from sqlalchemy import select

from spe import purchases, vault, writer
from spe.db import Evidence, SureOp
from spe.providers import Context, amazon
from spe.providers.amazon import build_purchases, parse_order, parse_transactions, read_gift_card, store_date
from spe.settings import Settings
from spe.vouchers import ROME


def tx(day, amount, order, kind="Charge", status="Approved"):
    return {"formattedAmount": ("-" if kind == "Charge" else "+") + f"€{amount}", "formattedDate": day,
            "orderData": [{"orderDisplayString": f"Ordine n. {o}", "orderDetailsUrl": f"https://www.amazon.it/gp/css/summary/edit.html?orderID={o}"}
                          for o in ([order] if isinstance(order, str) else order)],
            "statementDescriptor": "AMZN Mktp IT", "statusInfo": {"status": status, "amountDetailsStatusType": kind}}


def page(*txs):
    state = {"transactionResponseState": {"visibleTransactionResponse": {"transactionList": list(txs)}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"state": state}}})}</script>'


def it_date(d):
    return f"{d.day} {amazon.MONTH_NAMES[0].split()[d.month - 1]} {d.year}"


def eur_it(x):
    return ("-" if x < 0 else "") + f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def gc_page(balance, rows, next_token=None):
    """/gc/balance as observed on amazon.it, reduced to what is read: balance, ledger table, pagination."""
    trs = ""
    for day, label, amount, closing, order, serial in rows:
        ref = (f'<a class="a-link-normal" href="/gp/your-account/order-details/ref=gcf_b_bp_lpo_c_d_b_x?ie=UTF8&amp;orderID={order}"><span>{order}</span></a>'
               if order else f"<span>Codice promozionale: xxxx-xxxxxx-AB1C; numero di serie: {serial}</span>" if serial else "")
        trs += f"<tr><td>{it_date(day)}</td><td><span>{label}</span><br>{ref}</td><td>{eur_it(amount)}</td><td>{eur_it(closing)}</td></tr>"
    nxt = (f'<li class="a-last"><a href="/gc/balance?ref_=gcf_b_bp_b_c_d_b_x&amp;next={next_token}">Successivo</a></li>' if next_token
           else '<li class="a-disabled a-last">Successivo</li>')
    return (f'<h2>Il tuo saldo Amazon: <span id="gc-ui-balance-gc-balance-value" class="a-color-success">{eur_it(balance)}</span></h2>'
            '<table id="gc-balance-ledger-table" class="a-bordered"><tr><th>Data</th><th>Descrizione</th><th>Importo</th><th>Saldo di chiusura</th></tr>'
            f'{trs}</table><nav id="gc-balance-ledger-pagination"><ul class="a-pagination"><li class="a-disabled">Precedente</li>{nxt}</ul></nav>')


A, B, C = "405-0000000-0000001", "405-0000000-0000002", "405-0000000-0000003"


def test_one_order_two_shipments_and_a_refund_is_one_purchase():
    txs = parse_transactions(page(
        tx("02 ottobre 2026", "40.00", A), tx("05 ottobre 2026", "60.00", A), tx("12 ottobre 2026", "10.00", A, kind="Refund"),
        tx("13 ottobre 2026", "9.99", B, status="Pending"),  # not settled: ignored until it is
        tx("14 ottobre 2026", "30.00", [B, C]),  # one charge for two orders: held, not guessed
        {**tx("02 ottobre 2026", "300.00", A), "statementDescriptor": ""},  # installment plan total: never on the card
    ))
    orders = {A: {"date": date(2026, 10, 1), "items": [("USB hub", D("40.00")), ("Libro", D("60.00"))]}}
    ps, held = build_purchases(txs, orders)
    [p] = ps
    assert (p.id, p.date, p.total) == (A, date(2026, 10, 1), D("90.00"))
    assert [(x.amount, x.on) for x in p.payments] == [(D("40.00"), date(2026, 10, 2)), (D("60.00"), date(2026, 10, 5)),
                                                       (D("-10.00"), date(2026, 10, 12))]
    assert p.problems() == [] and [ln.description for ln in p.lines] == ["USB hub", "Libro"]
    assert held == [(B, "one charge of €30.00 covers 2 orders: not split automatically")]
    assert {t["store"] for t in txs} == {"www.amazon.it"}


def test_order_page_components():
    html = """<div data-component="orderDate"><span>
      6 ottobre 2026 </span></div><div data-component="purchasedItems"><div data-component="itemTitle"><a>Oral-B iO 6
      Spazzolino</a></div><div data-component="unitPrice"><span>119,99€</span><span aria-hidden="true">119,99€</span></div></div>"""
    assert parse_order(html) == {"date": date(2026, 10, 6), "items": [("Oral-B iO 6 Spazzolino", D("119.99"))]}


def test_dates_from_every_euro_store_language():
    oct6 = date(2026, 10, 6)
    for s in ("6 ottobre 2026", "6. Oktober 2026", "6 octobre 2026", "6 de octubre de 2026", "6 oktober 2026",
              "October 6, 2026", "6 Oct. 2026", "06 okt. 2026"):
        assert store_date(s) == oct6, s
    assert store_date("1. März 2026") == date(2026, 3, 1) and store_date("15 août 2026") == date(2026, 8, 15)


def test_euro_store_order_page_and_purchase_naming():
    html = """<div data-component="orderDate">6. Oktober 2026</div><div data-component="itemTitle">Buch</div>
      <div data-component="unitPrice">1.234,56&nbsp;€</div><div data-component="itemTitle">Livre</div><div data-component="unitPrice">€7.50</div>"""
    assert parse_order(html) == {"date": date(2026, 10, 6), "items": [("Buch", D("1234.56")), ("Livre", D("7.50"))]}
    german = {**tx("7. Oktober 2026", "40.00", A), "orderData": [
        {"orderDisplayString": f"Bestellung {A}", "orderDetailsUrl": f"https://www.amazon.de/gp/css/summary/edit.html?orderID={A}"}]}
    [p], _ = build_purchases(parse_transactions(page(german)), {})
    assert (p.provider, p.merchant, p.date) == ("amazon_it", "Amazon.de", date(2026, 10, 7))  # one EU account, labelled by store


def test_one_connection_reads_every_eu_store_and_never_records_an_order_twice(db, monkeypatch):
    def on(store, order):
        return {**tx("05 Nov 2026", "24.00", order), "orderData": [
            {"orderDisplayString": f"Order #{order}", "orderDetailsUrl": f"https://www.{store}/gp/css/summary/edit.html?orderID={order}"}]}

    listing = page(on("amazon.it", A), on("amazon.de", B), on("amazon.fr", C))  # the whole EU account, on amazon.it

    class FakeClient:
        fetched: list[str] = []

        def __init__(self, cookies):
            pass

        def get(self, path, **params):
            if "orderID" in params:
                self.fetched.append(params["orderID"])
                return "<div data-component='orderDate'>1. November 2026</div>"
            return gc_page(D(0), []) if path == "/gc/balance" else listing

        def cookies(self):
            return []

    monkeypatch.setattr(amazon, "AmazonClient", FakeClient)
    monkeypatch.setattr(amazon, "PAGE_DELAY", 0)
    s = Settings("http://sure.test", "s")
    first, again = conn(db, "amazon_it"), conn(db, "amazon_it")  # the same account added twice
    db.add(Evidence(user_id=1, connection_id=first.id, provider="amazon_it", key=C, kind="purchase", payload={}))
    again.session = vault.seal("s", {"cookies": []})
    db.commit()

    assert amazon.AmazonEU().sync(Context(db, None, s, again)).startswith("2 charges, 2 orders")
    assert sorted(FakeClient.fetched) == [A, B]  # C belongs to the other connection already
    assert {e.key: e.payload["merchant"] for e in db.query(Evidence).filter_by(connection_id=again.id)} == {A: "Amazon.it", B: "Amazon.de"}


GC, CARD = "9c000000-0000-0000-0000-000000000000", "c0000000-0000-0000-0000-000000000000"
APPLIED, RELEASE, REFUND = "Buono Regalo applicato all’ordine Amazon.it", "Rilascio del saldo Buoni Regalo trattenuto dall'ordine Amazon.it", "Rimborso dell’ordine Amazon.it"
ADDED, RELOAD = "Buono Regalo aggiunto", "Saldo Buoni Regalo aggiunto da Ricarica"
R, DIGITAL = "405-0000000-0000009", "D01-0000000-0000001"


def ledger(today):
    """Newest first, as Amazon lists it, on two pages; the balance runs up from zero."""
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    rows = [
        (d(4), APPLIED, D("-20.00"), D("30.00"), A, None),
        (d(6), RELEASE, D("5.00"), D("50.00"), B, None),  # a hold on order B, released
        (d(7), APPLIED, D("-5.00"), D("45.00"), B, None),
        (d(9), RELOAD, D("25.00"), D("50.00"), R, None),  # a reload: itself an order, paid by card
        (d(13), REFUND, D("2.50"), D("25.00"), None, None),
        (d(25), APPLIED, D("-27.50"), D("22.50"), DIGITAL, None),
        (d(30), ADDED, D("50.00"), D("50.00"), None, "1234567890123456"),  # a code: gift, or bought by card?
    ]
    return {None: gc_page(D("30.00"), rows[:4], "tok2"), "tok2": gc_page(D("30.00"), rows[4:])}


def fake_amazon(pages, listing, orders):
    class FakeClient:
        def __init__(self, cookies):
            pass

        def get(self, path, **params):
            if path == "/gc/balance":
                return pages[params.get("next")]
            return orders[params["orderID"]] if "orderID" in params else listing

        def cookies(self):
            return []

    return FakeClient


def test_gift_card_ledger_that_does_not_add_up_is_refused(monkeypatch):
    monkeypatch.setattr(amazon, "PAGE_DELAY", 0)
    today = datetime.now(ROME).date()
    pages = ledger(today)
    pages["tok2"] = pages["tok2"].replace(eur_it(D("22.50")), eur_it(D("21.50")))  # a misread row
    with pytest.raises(RuntimeError, match="does not add up"):
        read_gift_card(fake_amazon(pages, "", {})([]), None)


def test_gift_card_movements_land_in_the_wallet_once_and_pay_their_orders(db, monkeypatch):
    today = datetime.now(ROME).date()
    day = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    order = '<div data-component="orderDate">{}</div><div data-component="itemTitle">{}</div><div data-component="unitPrice">{}</div>'
    orders = {A: order.format(it_date(today - timedelta(days=5)), "Cuffie", "35,00 €"), B: order.format(it_date(today - timedelta(days=8)), "Cavo", "5,00 €")}
    listing = page(tx(it_date(today - timedelta(days=3)), "15.00", A), tx(it_date(today - timedelta(days=9)), "25.00", R))
    monkeypatch.setattr(amazon, "AmazonClient", fake_amazon(ledger(today), listing, orders))
    monkeypatch.setattr(amazon, "PAGE_DELAY", 0)
    sure = FakeSure()
    card_a = sure.add(CARD, day(3), "15.00", "AMZN Mktp IT")
    c = conn(db, "amazon_it", gift_card_account=GC)
    c.session = vault.seal("s", {"cookies": []})
    db.commit()

    def sync():
        c.status = "syncing"  # as run_connection does
        return amazon.AmazonEU().sync(Context(db, sure.client(), Settings("http://sure.test", "s"), c))

    assert "Sure account" in sync() and c.status == "degraded"  # wallet still empty in Sure: reported, not fixed
    creates = db.scalars(select(SureOp)).all()
    assert sorted((o.kind, o.payload["nature"], o.payload["amount"], o.status) for o in creates) == [
        ("create", "expense", "20.00", "approved"), ("create", "expense", "27.50", "approved"), ("create", "expense", "5.00", "approved"),
        ("create", "income", "2.50", "approved"), ("create", "income", "25.00", "approved"), ("create", "income", "5.00", "approved"),
        ("create", "income", "50.00", "approved")]  # the wallet is the enricher's: no approval; Sure pairs top-ups with card payments
    bought = {e.key: [(p["method"], p["amount"]) for p in e.payload["payments"]] for e in db.scalars(select(Evidence).where(Evidence.kind == "purchase"))}
    assert bought == {A: [("gift_card", "20.00"), ("card", "15.00")], B: [("gift_card", "5.00"), ("gift_card", "-5.00")]}  # no reload, no digital

    assert writer.apply(db, sure.client(), 1)["created"] == 7
    posts = sure.posts
    summary = sync()  # a later read: everything already written
    assert writer.apply(db, sure.client(), 1) == {} and sure.posts == posts
    assert summary.endswith("balance €30.00") and c.status != "degraded"  # Sure's wallet matches Amazon's balance
    assert purchases.reconcile(db, sure.client(), {purchases.VOUCHER: set(), purchases.GIFT_CARD: {GC}}, 1) == "2/2 purchases linked"
    splits = {o.transaction_id: [s["name"] for s in o.payload["splits"]] for o in db.scalars(select(SureOp).where(SureOp.kind == "split"))}
    gc_a = next(r for r in sure.rows if r["account"]["id"] == GC and r["amount_decimal"] == "20.00")
    assert splits[card_a["id"]] == splits[gc_a["id"]] == ["Cuffie (parte)"]  # one €35 item, paid €15 by card + €20 by gift card
