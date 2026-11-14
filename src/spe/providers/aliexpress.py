"""AliExpress orders, read over AliExpress's own signed "mtop" API. Observed on the owner's account, 2026-11-14.

With the cookies of a signed-in browser (the request is signed with md5(token&t&appKey&data), token = the first
part of the _m_h5_tk cookie, which the API rotates by answering FAIL_SYS_TOKEN_EXOIRED once):
  mtop.aliexpress.trade.buyer.order.list    10 orders a page: id, date, EUR total, status, items, paymentOutId;
                                            next pages are a POST carrying the page's own "ultron" state back
  mtop.aliexpress.trade.buyer.order.detail  for a cancelled order: the day it ended (its refund follows)
One checkout pays several orders with one card charge: orders whose paymentOutId differ only in their last six
characters ("...893594" + "03"/"04"... + "0524") were exactly the card charges in Sure (32.99 = 4 orders, 43.79
= 5...). So one checkout = one purchase: a card charge of all its orders, a refund per cancelled order, and each
order's total spread over its items (shipping, customs, coins included) as product lines.
ponytail: refunds of returned items in completed orders are not listed here; those bank refunds stay as they are.
"""

import hashlib
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from spe import loginbrowser, purchases, vault
from spe.db import record_evidence
from spe.providers import Context, NeedsAuth, register
from spe.providers.amazon import UA, store_date
from spe.purchases import Line, Payment, Purchase, proportional
from spe.vouchers import ROME

API = "https://acs.aliexpress.com/h5"
APP_KEY = "12574478"  # the web site's own, from its mtop client
ORDERS = "mtop.aliexpress.trade.buyer.order.list"
DETAIL = "mtop.aliexpress.trade.buyer.order.detail"
BASE = {"clientPlatform": "pc", "shipToCountry": "IT", "_lang": "en_US", "timeZone": "GMT+0100"}  # English dates
FIRST_SYNC, LOOKBACK = timedelta(days=365), timedelta(days=30)  # orders are rare: a year at first
PAGE_SIZES = (50, 150, 400)


def money(info: str) -> Decimal:
    """'10,46€|10|46' -> 10.46 (integer and cents come separately, whatever the locale)."""
    _, units, cents = info.split("|")
    return Decimal(f"{units}.{cents}")


def checkout_key(payment_out_id: str) -> str:
    return payment_out_id[:-6]


class MtopClient:
    def __init__(self, cookies: list[dict], transport: httpx.BaseTransport | None = None):
        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        self.http = httpx.Client(cookies=jar, timeout=30, transport=transport, headers={
            "User-Agent": UA, "Referer": "https://www.aliexpress.com/p/order/index.html", "Origin": "https://www.aliexpress.com"})

    def cookies(self) -> list[dict]:
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.http.cookies.jar]

    def call(self, api: str, data: dict, post: bool = False) -> dict:
        for _ in range(2):  # the first call after a while only hands out a fresh token
            token = next((c.value for c in self.http.cookies.jar if c.name == "_m_h5_tk"), "").split("_")[0]
            t, body = str(int(time.time() * 1000)), json.dumps(data, separators=(",", ":"))
            params = {"jsv": "2.5.1", "appKey": APP_KEY, "t": t, "sign": hashlib.md5(f"{token}&{t}&{APP_KEY}&{body}".encode()).hexdigest(),
                      "api": api, "v": "1.0", "type": "originaljson", "dataType": "originaljsonp", "method": "POST" if post else "GET"}
            url = f"{API}/{api}/1.0/"
            r = self.http.post(url, params=params, data={"data": body}) if post else self.http.get(url, params=params | {"data": body})
            r.raise_for_status()
            j = r.json()
            ret = " ".join(j.get("ret", []))
            if "TOKEN" in ret:
                continue
            if "SESSION" in ret or "LOGIN" in ret:
                raise NeedsAuth("AliExpress asks to sign in again: reconnect from Connessioni")
            if "USER_VALIDATE" in ret or "RGV587" in ret:
                raise NeedsAuth("AliExpress wants a human check: reconnect from Connessioni and solve it")
            if not ret.startswith("SUCCESS"):
                raise RuntimeError(f"AliExpress {api}: {ret[:200]}")
            return j["data"]
        raise RuntimeError(f"AliExpress {api}: token not renewed")

    def orders(self, since: date) -> list[dict]:
        """Orders placed on or after `since`. Asks for one ever larger first page instead of paging: consecutive pages
        repeat and skip orders (observed), a single page of N is consistent."""
        page = self.call(ORDERS, BASE | {"statusTab": None, "renderType": "init"})
        body = next(k for k in page["data"] if k.startswith("pc_om_list_body"))
        link = page["linkage"]
        orders: list[dict] = []
        for size in PAGE_SIZES:
            state = json.loads(json.dumps(page["data"][body]))
            state["fields"].update(pageIndex=1, pageSize=size)
            params = {"operator": body, "data": {body: state}, "hierarchy": {"structure": page["hierarchy"]["structure"]},
                      "endpoint": page["endpoint"], "linkage": {"common": {k: link["common"].get(k) for k in ("compress", "queryParams", "validateParams")},
                                                                "signature": link.get("signature")}}
            got = self.call(ORDERS, BASE | {"statusTab": None, "params": json.dumps(params, separators=(",", ":"))}, post=True)["data"]
            orders = [v["fields"] for k, v in got.items() if k.startswith("pc_om_list_order_")]
            if not got.get(body, {}).get("fields", {}).get("hasMore") or not orders or min(store_date(o["orderDateText"]) for o in orders) < since:
                break
        unique = {o["orderId"]: o for o in orders}
        return [o for o in unique.values() if store_date(o["orderDateText"]) >= since]

    def ended_on(self, order_id: str) -> date | None:
        data = self.call(DETAIL, BASE | {"tradeOrderId": order_id})["data"]
        info = next((v["fields"] for k, v in data.items() if k.startswith("detail_simple_order_info_component")), {})
        return store_date(info["orderEndTime"]) if info.get("orderEndTime") else None


def build_purchases(orders: list[dict], ended: dict[str, date | None]) -> tuple[list[Purchase], list[tuple[str, str]]]:
    by_checkout: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        if o.get("paymentOutId"):  # unpaid orders have none: nothing reached the card
            by_checkout[checkout_key(o["paymentOutId"])].append(o)
    out, held = [], []
    for key, os_ in by_checkout.items():
        if any(o.get("currencyCode") != "EUR" for o in os_):
            held.append((key, "charged in another currency: matching needs euro amounts"))
            continue
        day = min(store_date(o["orderDateText"]) for o in os_)
        cancelled = [o for o in os_ if o.get("statusText", "").strip().lower().startswith("cancel")]
        pays = (Payment("card", sum((money(o["formatPriceInfo"]) for o in os_), Decimal(0)), on=day),
                *(Payment("card", -money(o["formatPriceInfo"]), on=ended.get(o["orderId"])) for o in cancelled))
        lines = []
        for o in os_:
            items = [Line(ln.get("itemTitle") or "articolo", money(ln["formatPriceInfo"]) * int(ln.get("quantity") or 1)) for ln in o["orderLines"]]
            items = [i for i in items if i.amount > 0]
            lines += [Line(t, a) for t, a in proportional(items, money(o["formatPriceInfo"]))] if items else []
        out.append(Purchase(provider="aliexpress", id=key, merchant="AliExpress", date=day, total=sum((p.amount for p in pays), Decimal(0)),
                            payments=pays, lines=tuple(lines), keywords=("ALIEXPRESS",)))
    return out, held


class AliExpress:
    name = "aliexpress"
    label = "AliExpress (ordini)"
    interval = timedelta(hours=12)
    login_url = "https://www.aliexpress.com/p/order/index.html"  # sends you to sign in first
    domains = ("aliexpress.com", "aliexpress.us", "aliexpress.it", "alibaba.com")  # sign-in goes through login.aliexpress.us

    def import_session(self, ctx: Context, tab: loginbrowser.LoginTab) -> None:
        jar = tab.cookies("aliexpress.com")
        if not any(c["name"] == "xman_us_t" for c in jar):
            raise NeedsAuth("not signed in to aliexpress.com yet")
        ctx.state.session = vault.seal(ctx.settings.secret_key, {"cookies": jar})
        ctx.state.sync_requested = True

    def sync(self, ctx: Context) -> str:
        secret = ctx.settings.secret_key
        session = vault.unseal(secret, ctx.state.session)
        if not session:
            raise NeedsAuth("not connected: sign in to aliexpress.com from Connessioni")
        client = MtopClient(session["cookies"])
        try:
            today = datetime.now(ROME).date()
            last = ctx.state.cursor.get("last_success")
            orders = client.orders(date.fromisoformat(last) - LOOKBACK if last else today - FIRST_SYNC)
            known = ctx.state.cursor.get("ended", {})  # cancelled orders' end dates, fetched once
            for o in orders:
                if o.get("statusText", "").strip().lower().startswith("cancel") and o["orderId"] not in known:
                    when = client.ended_on(o["orderId"])
                    known[o["orderId"]] = when.isoformat() if when else None
            ctx.state.cursor = {**ctx.state.cursor, "ended": known, "last_success": today.isoformat()}
            ps, held = build_purchases(orders, {k: date.fromisoformat(v) if v else None for k, v in known.items()})
            for p in ps:
                purchases.record(ctx.db, ctx.state, p)
            for key, why in held:
                record_evidence(ctx.db, ctx.state, key, "purchase", {"id": key}, why)
            return f"{len(orders)} orders, {len(ps)} checkouts, {len(held)} held"
        finally:
            ctx.state.session = vault.seal(secret, {"cookies": client.cookies()})
            ctx.db.commit()


register(AliExpress())
