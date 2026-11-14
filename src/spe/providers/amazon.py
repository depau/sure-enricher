"""Amazon orders of a European Amazon account (.it, .de, .fr, .es, .nl, .com.be...), from the charges Amazon itself
lists. Observed on the owner's account, 2026-11-14: the payments page lists the whole EU account's charges whichever
store you read it on, and amazon.it shows the details of orders placed on the other stores too. So one connection,
signed in on amazon.it, covers every EU store; each order is labelled with its own store (Amazon.fr...).
The provider id stays "amazon_it": it is part of the markers already written into Sure notes. amazon.com is a
separate account (charges in USD) and is not covered.

With the cookies of a signed-in browser:
  GET /cpe/yourpayments/transactions        __NEXT_DATA__ JSON: the 20 latest charges/refunds, each with
                                            date, amount, card, order number(s), Charge/Refund, status
  GET /your-orders/order-details?orderID=   order date and items (title, unit price, quantity)
One order = one purchase whose payment legs are its actual charges (shipments, installments, refunds), so the
order total is never booked as an expense of its own. Items go into the notes; categories are left to the owner.
ponytail: only the first page of 20 charges is read; with two syncs a day that is weeks of slack.
"""

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from spe import loginbrowser, purchases, vault
from spe.db import Evidence, record_evidence
from spe.providers import Context, NeedsAuth, register
from spe.purchases import Line, Payment, Purchase

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
WWW = "https://www.amazon.it"
# Dates and prices are parsed in any EU store's language (and English): the account's language preference wins.
MONTH_NAMES = [
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "januar februar märz april mai juni juli august september oktober november dezember",
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "januari februari maart april mei juni juli augustus september oktober november december",
    "january february march april may june july august september october november december",
]
MONTHS = {name: i for line in MONTH_NAMES for i, name in enumerate(line.split(), 1)}
ORDER_ID = re.compile(r"\d{3}-\d{7}-\d{7}")
PRICE = re.compile(r"\d[\d.,\s\u00a0\u202f]*[.,]\d{2}")


def store_date(s: str) -> date:
    """'6 ottobre 2026', '6. Oktober 2026', '6 de octubre de 2026', 'October 6, 2026', '6 oct. 2026' -> date."""
    words = [w.lower().rstrip(".") for w in re.findall(r"[^\W\d_]+\.?", s)]
    numbers = [int(n) for n in re.findall(r"\d+", s)]
    months = {MONTHS[w] for w in words if w in MONTHS} or {
        n for w in words if len(w) >= 3 for name, n in MONTHS.items() if name.startswith(w)}
    years = [n for n in numbers if n > 1900]
    days = [n for n in numbers if 1 <= n <= 31]
    if len(months) != 1 or len(years) != 1 or len(days) != 1:
        raise ValueError(f"unrecognised Amazon date {s!r}")
    return date(years[0], months.pop(), days[0])


def amount(s: str) -> Decimal:
    """'-€24.00' / '+€5,00' -> 24.00 / 5.00 (sign ignored: Charge/Refund says the direction)."""
    digits = re.sub(r"[^\d.,]", "", s)
    if "," in digits and "." in digits:
        digits = digits.replace(".", "") if digits.rfind(",") > digits.rfind(".") else digits.replace(",", "")
    return Decimal(digits.replace(",", "."))


def parse_transactions(html: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise NeedsAuth("Amazon payments page without data: session expired? sign in again and import it")
    state = json.loads(m[1])["props"]["pageProps"]["state"]
    out = []
    for t in state["transactionResponseState"]["visibleTransactionResponse"]["transactionList"]:
        info = t["statusInfo"]
        out.append({
            "date": store_date(t["formattedDate"]),
            "amount": amount(t["formattedAmount"]),
            "kind": info.get("amountDetailsStatusType"),  # Charge | Refund
            "status": info.get("status"),
            "orders": [o for d in t.get("orderData") or [] for o in ORDER_ID.findall(d.get("orderDisplayString", ""))],
            # which store the order belongs to: "https://www.amazon.it/gp/css/summary/edit.html?orderID=..."
            "store": next((urlsplit(d["orderDetailsUrl"]).hostname for d in t.get("orderData") or [] if d.get("orderDetailsUrl")), None),
            "descriptor": t.get("statementDescriptor") or "",
        })
    return out


class _Components(HTMLParser):
    """Text of every element carrying data-component="<name>" (Amazon's order page markup)."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.found: dict[str, list[str]] = defaultdict(list)
        self.open: list[tuple[str, int]] = []  # (component, depth where it started)
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        self.depth += 1
        name = dict(attrs).get("data-component")
        if name:
            self.open.append((name, self.depth))
            self.found[name].append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        while self.open and self.open[-1][1] >= self.depth:
            self.open.pop()
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        for name, _ in self.open:
            self.found[name][-1] += data


def parse_order(html: str) -> dict:
    p = _Components()
    p.feed(html)
    text = {k: [" ".join(v.split()) for v in vs] for k, vs in p.found.items()}
    when = next((x for x in text.get("orderDate", []) if x), None)
    prices = [amount(m[0]) for x in text.get("unitPrice", []) if (m := PRICE.findall(x))]
    items = list(zip(text.get("itemTitle", []), prices, strict=False))
    return {"date": store_date(when) if when else None, "items": items}


class AmazonClient:
    def __init__(self, cookies: list[dict], transport: httpx.BaseTransport | None = None):
        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        self.http = httpx.Client(cookies=jar, headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"},
                                 follow_redirects=True, timeout=30, transport=transport)

    def cookies(self) -> list[dict]:
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.http.cookies.jar]

    def get(self, path: str, **params: str) -> str:
        r = self.http.get(WWW + path, params=params)
        if "/ap/signin" in str(r.url):
            raise NeedsAuth("Amazon asks to sign in again: reconnect from Connessioni")
        r.raise_for_status()
        return r.text


def store_name(host: str | None) -> str:
    """'www.amazon.fr' -> 'Amazon.fr' (the store the order was placed on)."""
    return "Amazon." + (host or "www.amazon.it").removeprefix("www.").removeprefix("amazon.")


def build_purchases(txs: list[dict], orders: dict[str, dict]) -> tuple[list[Purchase], list[tuple[str, str]]]:
    by_order: dict[str, list[dict]] = defaultdict(list)
    held = []
    for t in txs:
        if t["status"] != "Approved" or not t["orders"]:
            continue  # pending, or Amazon Pay at another shop: nothing to attach it to
        if not t["descriptor"]:
            # Orders paid in installments list the plan total without a statement descriptor next to the
            # monthly card charges: the total never reaches the bank, the installments do.
            continue
        if len(t["orders"]) > 1:
            held.append((t["orders"][0], f"one charge of €{t['amount']} covers {len(t['orders'])} orders: not split automatically"))
            continue
        by_order[t["orders"][0]].append(t)
    out = []
    for oid, ts in by_order.items():
        o = orders.get(oid) or {"date": None, "items": []}
        pays = tuple(Payment("card", t["amount"] if t["kind"] == "Charge" else -t["amount"], on=t["date"])
                     for t in sorted(ts, key=lambda t: t["date"]))
        out.append(Purchase(
            provider="amazon_it", id=oid, merchant=store_name(ts[0]["store"]), date=o["date"] or min(t["date"] for t in ts),
            total=sum((p.amount for p in pays), Decimal(0)), payments=pays,
            lines=tuple(Line(title, price) for title, price in o["items"]), keywords=("AMAZON", "AMZN"),
        ))
    return out, held


class AmazonEU:
    name = "amazon_it"
    label = "Amazon (Europa)"
    interval = timedelta(hours=12)
    login_url = f"{WWW}/cpe/yourpayments/transactions"  # sends you to sign in first
    domains = ("amazon.it", "amazon.com", "amazon.eu")  # the only sites the sign-in tab may show

    def import_session(self, ctx: Context, tab: loginbrowser.LoginTab) -> None:
        jar = tab.cookies("amazon.it")
        if not any(c["name"] in ("at-acbit", "sess-at-acbit", "x-acbit") for c in jar):
            raise NeedsAuth("not signed in to amazon.it yet")
        ctx.state.session = vault.seal(ctx.settings.secret_key, {"cookies": jar})
        ctx.state.sync_requested = True

    def sync(self, ctx: Context) -> str:
        secret = ctx.settings.secret_key
        session = vault.unseal(secret, ctx.state.session)
        if not session:
            raise NeedsAuth("not connected: sign in to amazon.it from Connessioni")
        client = AmazonClient(session["cookies"])
        try:
            listed = parse_transactions(client.get("/cpe/yourpayments/transactions"))
            # another connection of the user (the same account added twice) already has it: never twice
            elsewhere = set(ctx.db.scalars(select(Evidence.key).where(Evidence.user_id == ctx.state.user_id,
                                                                      Evidence.connection_id != ctx.state.id)))
            txs = [t for t in listed if not elsewhere.intersection(t["orders"])]
            current = {o for t in txs for o in t["orders"]}
            known = {k: v for k, v in ctx.state.cursor.get("orders", {}).items() if k in current}  # details, fetched once
            for oid in current - known.keys():
                o = parse_order(client.get("/your-orders/order-details", orderID=oid))
                known[oid] = {"date": o["date"].isoformat() if o["date"] else None, "items": [[t, str(p)] for t, p in o["items"]]}
            ctx.state.cursor = {**ctx.state.cursor, "orders": known}
            orders = {k: {"date": date.fromisoformat(v["date"]) if v["date"] else None,
                          "items": [(t, Decimal(p)) for t, p in v["items"]]} for k, v in known.items()}
            ps, held = build_purchases(txs, orders)
            for p in ps:
                purchases.record(ctx.db, ctx.state, p)
            for oid, why in held:
                record_evidence(ctx.db, ctx.state, oid, "purchase", {"id": oid}, why)
            return f"{len(txs)} charges, {len(ps)} orders, {len(held)} held"
        finally:
            ctx.state.session = vault.seal(secret, {"cookies": client.cookies()})
            ctx.db.commit()


register(AmazonEU())
