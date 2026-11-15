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
  GET /gc/balance[?next=<token>]            gift card balance (#gc-ui-balance-gc-balance-value) and its ledger
                                            (#gc-balance-ledger-table), 15 rows a page, newest first: date, description
                                            (+ order link, or a gift card's masked code and serial number), signed
                                            amount, balance after it. No row id. Pages chain through "Successivo".
One order = one purchase whose payment legs are its actual charges (shipments, installments, refunds), so the
order total is never booked as an expense of its own. Items go into the notes; categories are left to the owner.
Gift card payments are not on the payments page: the ledger is the only place they show up. Its rows are written
to a wallet account in Sure (like Edenred's vouchers) and become gift card legs of the orders they paid.
Ledger wordings seen (Italian, the account's language): "Buono Regalo applicato all'ordine" (spent on an order),
"Rimborso dell'ordine" (refund, sometimes without the order), "Rilascio del saldo ... trattenuto dall'ordine"
(a hold released), "Buono Regalo aggiunto" (a code redeemed), "Saldo Buoni Regalo aggiunto da Ricarica" (a reload,
itself an order paid by card). Anything else is held, not guessed.
ponytail: only the first page of 20 charges is read; with two syncs a day that is weeks of slack.
"""

import hashlib
import json
import random
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from itertools import pairwise
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select

from spe import loginbrowser, purchases, vault, vouchers
from spe.db import Evidence, record_evidence
from spe.providers import Blocked, Context, NeedsAuth, register
from spe.purchases import GIFT_CARD, Line, Payment, Purchase
from spe.vouchers import ROME, Kind, VoucherMovement

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
GC_WALLET = "amazon_gift_card"
GC_SOURCE = "sure-purchase-enricher:amazon-gift-card"  # Sure dedupes on it: never change it
GC_LABEL = "Amazon gift card"
GC_LOOKBACK = timedelta(days=21)  # late or reordered ledger rows: re-read three weeks before the last read
GC_ORDERS = timedelta(days=60)  # gift card legs are added to orders this recent; older ones only get their movement
PAGE_DELAY = 5.0  # seconds (±jitter) between ledger pages: a first read walks the whole history, and Amazon throttles


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
        if r.status_code == 503:  # a lone one now and then: wait and try once more
            time.sleep(PAGE_DELAY * random.uniform(2, 4))
            r = self.http.get(WWW + path, params=params)
        if r.status_code == 503:
            # Amazon's "Ci dispiace" page: it throttles this session (seen after ~60 page reads in a few minutes).
            raise Blocked("Amazon is throttling requests (503): trying again at the next scheduled sync")
        if "/ap/signin" in str(r.url):
            raise NeedsAuth("Amazon asks to sign in again: reconnect from Connessioni")
        r.raise_for_status()
        return r.text


def store_name(host: str | None) -> str:
    """'www.amazon.fr' -> 'Amazon.fr' (the store the order was placed on)."""
    return "Amazon." + (host or "www.amazon.it").removeprefix("www.").removeprefix("amazon.")


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def signed(s: str) -> Decimal:
    """'-12,34 €' -> -12.34, '5,00 €' -> 5.00."""
    return -amount(s) if s.lstrip().startswith(("-", "\u2212")) else amount(s)


def parse_gift_card(page: str) -> tuple[Decimal, list[dict], str | None]:
    """The balance, the ledger rows of this page (newest first) and the token of the next, older page."""
    bal = re.search(r'id="gc-ui-balance-gc-balance-value"[^>]*>(.*?)</span>', page, re.S)
    table = re.search(r'id="gc-balance-ledger-table".*?</table>', page, re.S)
    if not bal or not table:
        raise RuntimeError("Amazon gift card page without balance or ledger: layout changed, nothing written")
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table[0], re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not cells:
            continue  # the header row
        if len(cells) != 4:
            raise RuntimeError(f"Amazon gift card ledger row with {len(cells)} cells: layout changed, nothing written")
        order = re.search(r"orderID=([\w-]+)", cells[1])
        serial = None if order else re.search(r"(\d{8,})\D*$", _text(cells[1]))
        rows.append({"date": store_date(_text(cells[0])), "label": _text(cells[1].split("<br", 1)[0]),
                     "order": order[1] if order else None, "serial": serial[1] if serial else None,
                     "amount": signed(_text(cells[2])), "closing": signed(_text(cells[3]))})
    nav = re.search(r'id="gc-balance-ledger-pagination".*?</nav>', page, re.S)
    nxt = re.search(r'<li class="a-last"><a href="([^"]+)"', nav[0]) if nav else None
    token = parse_qs(urlsplit(unescape(nxt[1])).query).get("next", [None])[0] if nxt else None
    return signed(_text(bal[1])), rows, token


def read_gift_card(client: AmazonClient, since: date | None) -> tuple[Decimal, list[dict], bool]:
    """Balance and ledger rows down to `since` (every row of that day included; None: the whole history), and
    whether the whole history was read. Refuses a ledger whose running balance doesn't add up: a page was missed
    or misread, and writing it would put a wrong balance into Sure."""
    balance, rows, token = parse_gift_card(client.get("/gc/balance"))
    while token and (since is None or (rows and rows[-1]["date"] >= since)):
        time.sleep(PAGE_DELAY * random.uniform(0.8, 1.2))
        _, more, token = parse_gift_card(client.get("/gc/balance", next=token))
        rows += more
    if rows and rows[0]["closing"] != balance:
        raise RuntimeError(f"Amazon gift card balance €{balance} but its last ledger row ends at €{rows[0]['closing']}: nothing written")
    for newer, older in pairwise(rows):
        if newer["closing"] != older["closing"] + newer["amount"]:
            raise RuntimeError(f"Amazon gift card ledger does not add up on {newer['date']}: nothing written")
    return balance, rows, token is None


def gift_card_movements(rows: list[dict]) -> list[VoucherMovement]:
    """One movement per ledger row, in the ledger's order (the dedupe keys of identical rows depend on it)."""
    out = []
    for r in rows:
        if r["amount"] < 0:
            kind = Kind.REDEMPTION if r["order"] else Kind.OTHER
        elif r["serial"] or "Ricarica" in r["label"]:
            kind = Kind.TOPUP
        elif r["label"].startswith(("Rimborso", "Rilascio")):
            kind = Kind.REFUND
        else:
            kind = Kind.OTHER
        out.append(VoucherMovement(
            wallet=GC_WALLET, booked_on=r["date"], amount=abs(r["amount"]), kind=kind,
            description=f"{r['label']} {r['order']}" if r["order"] else r["label"],
            # the serial number identifies a redeemed card for good; hashed, it is no use to anyone else
            source_id=hashlib.sha256(r["serial"].encode()).hexdigest()[:24] if r["serial"] else None,
            merchant="Amazon" if kind in (Kind.REDEMPTION, Kind.REFUND) else None,
        ))
    return out


def build_purchases(txs: list[dict], orders: dict[str, dict], gift: list[dict] = ()) -> tuple[list[Purchase], list[tuple[str, str]]]:
    """gift: gift card ledger rows of orders, in the charges' shape plus "method": the order's gift card legs."""
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
    for g in gift:
        by_order[g["orders"][0]].append(g)
    out = []
    for oid, ts in by_order.items():
        o = orders.get(oid) or {"date": None, "items": []}
        pays = tuple(Payment(t.get("method", "card"), t["amount"] if t["kind"] == "Charge" else -t["amount"], on=t["date"])
                     for t in sorted(ts, key=lambda t: t["date"]))
        store = next((t["store"] for t in ts if t.get("store")), None)
        out.append(Purchase(
            provider="amazon_it", id=oid, merchant=store_name(store), date=o["date"] or min(t["date"] for t in ts),
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
        cfg = ctx.state.config or {}
        try:
            listed = parse_transactions(client.get("/cpe/yourpayments/transactions"))
            gc_summary, gift, reloads = self._gift_card(ctx, client, cfg.get("gift_card_account"))
            # another connection of the user (the same account added twice) already has it: never twice
            elsewhere = set(ctx.db.scalars(select(Evidence.key).where(Evidence.user_id == ctx.state.user_id,
                                                                      Evidence.connection_id != ctx.state.id)))
            # a reload of the gift card balance is a transfer from the card, not a purchase
            txs = [t for t in listed if not elsewhere.intersection(t["orders"]) and not reloads.intersection(t["orders"])]
            gift = [g for g in gift if g["orders"][0] not in elsewhere]
            current = {o for t in [*txs, *gift] for o in t["orders"]}
            known = {k: v for k, v in ctx.state.cursor.get("orders", {}).items() if k in current}  # details, fetched once
            for oid in current - known.keys():
                time.sleep(PAGE_DELAY * random.uniform(0.8, 1.2))  # a first read fetches dozens: Amazon throttles bursts
                o = parse_order(client.get("/your-orders/order-details", orderID=oid))
                known[oid] = {"date": o["date"].isoformat() if o["date"] else None, "items": [[t, str(p)] for t, p in o["items"]]}
                ctx.state.cursor = {**ctx.state.cursor, "orders": known}  # kept if Amazon cuts the read short (throttling)
            ctx.state.cursor = {**ctx.state.cursor, "orders": known}
            orders = {k: {"date": date.fromisoformat(v["date"]) if v["date"] else None,
                          "items": [(t, Decimal(p)) for t, p in v["items"]]} for k, v in known.items()}
            ps, held = build_purchases(txs, orders, gift)
            for p in ps:
                purchases.record(ctx.db, ctx.state, p)
            for oid, why in held:
                record_evidence(ctx.db, ctx.state, oid, "purchase", {"id": oid}, why)
            return f"{len(txs)} charges, {len(ps)} orders, {len(held)} held; {gc_summary}"
        finally:
            ctx.state.session = vault.seal(secret, {"cookies": client.cookies()})
            ctx.db.commit()

    def _gift_card(self, ctx: Context, client: AmazonClient, account: str | None) -> tuple[str, list[dict], set[str]]:
        """Read the gift card ledger; with a wallet account chosen, write its movements there and hand back the
        orders' gift card legs and the orders that were balance reloads."""
        gc = dict(ctx.state.cursor.get("gift_card") or {})
        today = datetime.now(ROME).date()
        if account:
            since = date.fromisoformat(gc["read_on"]) - GC_LOOKBACK if gc.get("read_on") else None
        else:
            since = today if "opening" in gc else None  # read whole once, for the opening balance shown in Connections
        balance, rows, complete = read_gift_card(client, since)
        gc["balance"] = str(balance)
        if complete:
            first = rows[-1] if rows else None
            gc["opening"] = [str(first["closing"] - first["amount"]) if first else str(balance),
                             (first["date"] - timedelta(days=1) if first else today).isoformat()]
        if not account:
            ctx.state.cursor = {**ctx.state.cursor, "gift_card": gc}
            return f"gift card €{balance}, no account chosen", [], set()
        rows = [r for r in rows if since is None or r["date"] >= since]  # whole days only: twins keep their order
        moves = gift_card_movements(rows)
        summary = vouchers.stage(ctx.db, ctx.state, moves, {GC_WALLET: account}, GC_SOURCE, GC_LABEL)
        gc["read_on"] = today.isoformat()
        ctx.state.cursor = {**ctx.state.cursor, "gift_card": gc}
        gift = [{"date": r["date"], "amount": m.amount, "kind": "Charge" if r["amount"] < 0 else "Refund", "orders": [r["order"]],
                 "store": None, "method": GIFT_CARD}
                for r, m in zip(rows, moves, strict=True)
                if m.kind in (Kind.REDEMPTION, Kind.REFUND) and r["order"] and ORDER_ID.fullmatch(r["order"]) and r["date"] >= today - GC_ORDERS]
        reloads = {r["order"] for r, m in zip(rows, moves, strict=True) if m.kind == Kind.TOPUP and r["order"]}
        mismatch = vouchers.balance_mismatch(ctx.db, ctx.sure, account, balance, "Amazon gift card")
        if mismatch:
            ctx.state.status = "degraded"
        return f"gift card: {summary}; " + (mismatch or f"balance €{balance}"), gift, reloads


register(AmazonEU())
