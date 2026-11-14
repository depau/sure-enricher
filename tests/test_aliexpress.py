import hashlib
import json
from datetime import date
from decimal import Decimal as D
from urllib.parse import parse_qs

import httpx
import pytest

from spe.providers import NeedsAuth
from spe.providers.aliexpress import MtopClient, build_purchases


def order(oid, pay, day, total, items, status="Completed"):
    """An order as the list API returns it (synthetic ids and amounts)."""
    def info(x):
        units, cents = f"{x:.2f}".split(".")
        return f"{units},{cents}€|{units}|{cents}"
    return {"orderId": oid, "paymentOutId": pay, "orderDateText": day, "currencyCode": "EUR", "statusText": status,
            "formatPriceInfo": info(D(total)), "orderLines": [{"itemTitle": t, "formatPriceInfo": info(D(p)), "quantity": q} for t, p, q in items]}


def test_one_checkout_is_one_purchase_with_its_orders_items_and_cancellations():
    orders = [
        order("1", "2199000000000000000000101" + "0524", "Jul 12, 2026", "10.46", [("Cable", "2.90", 1)]),
        order("2", "2199000000000000000000102" + "0524", "Jul 12, 2026", "10.86", [("Clip", "4.25", 1), ("Hook", "3.32", 1)], "Canceled "),
        order("3", "2199000000000000000000103" + "0524", "Jul 12, 2026", "11.67", [("Screws", "1.89", 2), ("Nut", "1.83", 1)]),
        order("4", "2199000000000000000000999" + "0524", "Jun 24, 2026", "26.39", [("Lamp", "26.39", 1)]),  # another checkout
    ]
    ps, held = build_purchases(orders, {"2": date(2026, 7, 15)})
    assert held == []
    jul, jun = sorted(ps, key=lambda p: p.date, reverse=True)
    assert [(x.amount, x.on) for x in jul.payments] == [(D("32.99"), date(2026, 7, 12)), (D("-10.86"), date(2026, 7, 15))]
    assert jul.total == D("22.13") and jul.problems() == []
    # each order's total (shipping, customs, coins) spread over its own items: lines add up to the card charge
    assert sum(ln.amount for ln in jul.lines) == D("32.99")
    assert {ln.description: ln.amount for ln in jul.lines if ln.description in ("Cable", "Screws", "Nut")} == {
        "Cable": D("10.46"), "Screws": D("7.87"), "Nut": D("3.80")}  # 11.67 by 3.78 : 1.83, cent to the dearest
    assert (jun.total, jun.keywords, jun.marker) == (D("26.39"), ("ALIEXPRESS",), "[spe:aliexpress:21990000000000000000009]")


def test_mtop_calls_are_signed_renew_their_token_and_spot_a_lost_session():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = parse_qs(req.url.query.decode())
        calls.append(q)
        token = next((c.split("=")[1].split("_")[0] for c in req.headers.get("cookie", "").split("; ") if c.startswith("_m_h5_tk=")), "")
        expected = hashlib.md5(f"{token}&{q['t'][0]}&12574478&{q['data'][0]}".encode()).hexdigest()
        assert q["sign"] == [expected]
        if token != "fresh":
            return httpx.Response(200, json={"ret": ["FAIL_SYS_TOKEN_EXOIRED::expired"]}, headers={"set-cookie": "_m_h5_tk=fresh_123; Domain=.aliexpress.com; Path=/"})
        if json.loads(q["data"][0]).get("who") == "gone":
            return httpx.Response(200, json={"ret": ["FAIL_SYS_SESSION_EXPIRED::session expired"]})
        return httpx.Response(200, json={"ret": ["SUCCESS::ok"], "data": {"ok": True}})

    client = MtopClient([{"name": "_m_h5_tk", "value": "stale_1", "domain": ".aliexpress.com", "path": "/"}], transport=httpx.MockTransport(handler))
    assert client.call("mtop.test", {"who": "me"}) == {"ok": True}
    assert len(calls) == 2  # one round to get the fresh token
    with pytest.raises(NeedsAuth):
        client.call("mtop.test", {"who": "gone"})


def test_orders_come_from_one_growing_page_until_old_enough(monkeypatch):
    from spe.providers import aliexpress

    def page(size, n_orders, more):
        orders = {f"pc_om_list_order_{i}": {"fields": order(str(i), f"21990000000000000000{i:03d}010524", "Jul 12, 2026" if i < 50 else "Jan 3, 2025",
                                                             "1.00", [("x", "1.00", 1)])} for i in range(n_orders)}
        return {"data": {"pc_om_list_body_1": {"fields": {"hasMore": more, "pageIndex": 1, "pageSize": size}}, **orders},
                "linkage": {"common": {}}, "hierarchy": {"structure": {}}, "endpoint": {}}

    asked = []

    def call(self, api, data, post=False):
        if not post:
            return page(10, 10, True)
        size = json.loads(data["params"])["data"]["pc_om_list_body_1"]["fields"]["pageSize"]
        asked.append(size)
        return page(size, min(size, 60), size < 60)

    monkeypatch.setattr(aliexpress.MtopClient, "call", call)
    got = aliexpress.MtopClient([]).orders(date(2026, 1, 1))
    assert asked == [50, 150] and len(got) == 50  # 50 not old enough: asked again for more; older ones dropped
