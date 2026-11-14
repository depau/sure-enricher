import json
from datetime import date
from decimal import Decimal as D

from spe.providers.amazon import build_purchases, parse_order, parse_transactions, store_date


def tx(day, amount, order, kind="Charge", status="Approved"):
    return {"formattedAmount": ("-" if kind == "Charge" else "+") + f"€{amount}", "formattedDate": day,
            "orderData": [{"orderDisplayString": f"Ordine n. {o}", "orderDetailsUrl": f"https://www.amazon.it/gp/css/summary/edit.html?orderID={o}"}
                          for o in ([order] if isinstance(order, str) else order)],
            "statementDescriptor": "AMZN Mktp IT", "statusInfo": {"status": status, "amountDetailsStatusType": kind}}


def page(*txs):
    state = {"transactionResponseState": {"visibleTransactionResponse": {"transactionList": list(txs)}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"state": state}}})}</script>'


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
    from conftest import conn

    from spe import vault
    from spe.db import Evidence
    from spe.providers import Context, amazon
    from spe.settings import Settings

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
            return listing

        def cookies(self):
            return []

    monkeypatch.setattr(amazon, "AmazonClient", FakeClient)
    s = Settings("http://sure.test", "s")
    first, again = conn(db, "amazon_it"), conn(db, "amazon_it")  # the same account added twice
    db.add(Evidence(user_id=1, connection_id=first.id, provider="amazon_it", key=C, kind="purchase", payload={}))
    again.session = vault.seal("s", {"cookies": []})
    db.commit()

    assert amazon.AmazonEU().sync(Context(db, None, s, again)).startswith("2 charges, 2 orders")
    assert sorted(FakeClient.fetched) == [A, B]  # C belongs to the other connection already
    assert {e.key: e.payload["merchant"] for e in db.query(Evidence).filter_by(connection_id=again.id)} == {A: "Amazon.it", B: "Amazon.de"}
