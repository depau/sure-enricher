"""Esselunga in-store receipts (www.esselunga.it, "Scontrini"). Observed on the owner's account, 2026-11-13.

Over plain HTTP with the cookies of a signed-in browser (imported once, refreshed by the site as we use it):
  GET  /area-utenti/ist35/myesselunga/shoppingMovements      page; Fidaty card code in setCurrentCard("...")
  POST /area-utenti/csrfjs  (FETCH-CSRF-TOKEN: 1)            -> "X-CSRF-TOKEN:<value>"
  POST .../shoppingMovementsAjax?serviceName=getMovements...  receipts: id, importo, dataOperazione, store
  GET  .../shoppingMovementsAjax?pdfId=<id>...                308, body = relative URL of the receipt PDF
Every ajax call MUST carry the CSRF header: one call without it logged the session out of the receipts area.
The PDF text has the items, discounts, total and how it was paid (card, Ticket Restaurant, cash...).
With SPE_RECEIPTS_DIR set, every receipt PDF the site still lists is also kept there (the first run backfills).
"""

import io
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pypdf

from spe import loginbrowser, purchases, vault
from sqlalchemy import select

from spe.db import Evidence, record_evidence
from spe.providers import Context, NeedsAuth, register
from spe.purchases import Line, Payment, Purchase
from spe.vouchers import ROME

WWW = "https://www.esselunga.it"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
FIRST_SYNC = timedelta(days=60)
PAGE_SIZE = 16  # what the site asks for
MONEY = r"(\d{1,6},\d{2})"
ITEM = re.compile(rf"^(?P<desc>\S.*?)\s+\*(?P<vat>[a-z])\s+{MONEY}$")  # "INSALATINA NATURAMA   *a   1,98"
VAT = re.compile(r"^\*(?P<code>[a-z]) = IVA (?P<rate>\d+)%")  # legend: "*a = IVA 04%"
FOOD_VAT = 22  # Italian food is taxed at 4/5/10%; 22% is non-food (and some drinks): those go on the card first
DISCOUNT = re.compile(rf"^\s+(?P<desc>\S.*?)\s+{MONEY}-S$")  # "  SCONTO FIDATY 30%    0,81-S"
PAYMENT = re.compile(rf"^PAGAMENTO\s+(?P<how>.+?)\s+{MONEY}$")
# Receipt payment wording -> purchases.Payment.method. Unknown wording is held, never guessed.
METHODS = {"CARTA DI CREDITO": "card", "CARTA DI DEBITO": "card", "BANCOMAT": "card", "BUONO PASTO ELETTRON": "edenred",
           "CONTANTI": "cash"}


def _eur(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def parse_receipt(text: str) -> dict:
    """Items, total, payments and timestamp from the receipt PDF text (layout mode)."""
    lines: list[tuple[str, Decimal, str | None]] = []
    rates: dict[str, int] = {}
    payments: list[tuple[str, Decimal]] = []
    total = when = None
    for raw in text.splitlines():
        s = raw.rstrip()
        if total is None and (m := ITEM.match(s.strip()) if not s.startswith("  ") else None):
            lines.append((m["desc"], _eur(m[3]), m["vat"]))
        elif total is None and (m := DISCOUNT.match(s)):
            lines.append((m["desc"].strip(), -_eur(m[2]), None))
        elif m := VAT.match(s.strip()):
            rates[m["code"]] = int(m["rate"])
        elif m := re.match(rf"^TOTALE EURO\s+{MONEY}", s.strip()):
            total = _eur(m[1])
        elif m := PAYMENT.match(s.strip()):
            payments.append((m["how"], _eur(m[2])))
        elif when is None and (m := re.match(r"^(\d{2}-\d{2}-\d{4} \d{2}:\d{2})$", s.strip())):
            when = datetime.strptime(m[1], "%d-%m-%Y %H:%M").replace(tzinfo=ROME)
    return {"lines": [(d, a, rates.get(v, FOOD_VAT) < FOOD_VAT if v else True) for d, a, v in lines],
            "total": total, "payments": payments, "at": when}


def to_purchase(receipt_id: str, store: str, parsed: dict) -> tuple[Purchase | None, str | None]:
    if parsed["total"] is None or parsed["at"] is None:
        return None, "receipt PDF not understood (no total or date)"
    pays = []
    for how, amount in parsed["payments"]:
        method = next((v for k, v in METHODS.items() if how.startswith(k)), None)
        if method is None:
            return None, f"unknown payment '{how}' on the receipt"
        pays.append(Payment("cash" if method == "cash" else method, amount))
    lines = tuple(Line(d, a, eligible) for d, a, eligible in parsed["lines"])
    p = Purchase(provider="esselunga", id=receipt_id, merchant=f"Esselunga {store}".strip(), date=parsed["at"].date(),
                 total=parsed["total"], payments=tuple(pays), lines=lines, keywords=("ESSELUNGA",))
    return p, None


class EsselungaClient:
    def __init__(self, cookies: list[dict], transport: httpx.BaseTransport | None = None):
        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        self.http = httpx.Client(cookies=jar, headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"},
                                 follow_redirects=True, timeout=30, transport=transport)
        self.headers: dict[str, str] = {}

    def cookies(self) -> list[dict]:
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.http.cookies.jar]

    def open(self) -> str:
        """Enter the receipts area; returns the Fidaty card code. NeedsAuth when the session is gone."""
        r = self.http.get(f"{WWW}/area-utenti/ist35/myesselunga/shoppingMovements")
        card = re.search(r'setCurrentCard\("(\d+)"\)', r.text)
        if not card:
            raise NeedsAuth("Esselunga session expired: sign in again in the login browser and import it")
        token = self.http.post(f"{WWW}/area-utenti/csrfjs", headers={"FETCH-CSRF-TOKEN": "1", "Referer": str(r.url)}).text.strip()
        name, _, value = token.partition(":")
        if not value:
            raise RuntimeError("Esselunga CSRF token not issued: refusing to call the receipts API without it")
        self.headers = {name: value, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Referer": str(r.url)}
        return card[1]

    def receipts(self, card: str, since: date) -> list[dict]:
        out: list[dict] = []
        while True:
            q = {"codCarta": card, "dailyMovements": "true", "numeroMovimenti": str(PAGE_SIZE), "serviceName": "getMovements",
                 "startIndex": str(len(out)), "vediNonAttive": "false"}
            r = self.http.post(f"{WWW}/area-utenti/services/myesselunga/shoppingMovementsAjax", params=q, headers=self.headers)
            r.raise_for_status()
            page = json.loads(r.text, parse_float=Decimal)
            out += page
            oldest = min((datetime.strptime(m["dataOperazione"], "%d/%m/%Y").date() for m in page), default=since)
            if len(page) < PAGE_SIZE or oldest < since:
                return [m for m in out if datetime.strptime(m["dataOperazione"], "%d/%m/%Y").date() >= since]

    def receipt_pdf(self, card: str, receipt_id: int) -> bytes | None:
        """None when the site serves an empty file (seen for some listed receipts, e.g. 2026-02-27)."""
        r = self.http.get(f"{WWW}/area-utenti/services/myesselunga/shoppingMovementsAjax",
                          params={"pdfId": receipt_id, "codCarta": card, "vediNonAttive": "true"}, headers=self.headers)
        if r.status_code != 308:
            raise RuntimeError(f"receipt {receipt_id}: expected a 308 with the PDF link, got {r.status_code}")
        pdf = self.http.get(WWW + r.text.strip())
        pdf.raise_for_status()
        if not pdf.content:
            return None
        if not pdf.content.startswith(b"%PDF"):
            raise RuntimeError(f"receipt {receipt_id}: the link did not return a PDF")
        return pdf.content


def pdf_text(pdf: bytes) -> str:
    return pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text(extraction_mode="layout")


def archive_path(root: str, user_id: int | None, connection_id: int, day: date, receipt_id: str) -> Path:
    return Path(root) / f"user-{user_id}" / f"esselunga-{connection_id}" / f"{day:%Y}" / f"{day:%Y-%m-%d}_{receipt_id}.pdf"


def keep(path: Path, pdf: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(pdf)
    tmp.replace(path)  # never a half-written receipt under the final name


class Esselunga:
    name = "esselunga"
    label = "Esselunga (scontrini)"
    interval = timedelta(hours=12)

    login_url = f"{WWW}/area-utenti/ist35/myesselunga/shoppingMovements"  # sends you to sign in first
    domains = ("esselunga.it",)  # the only sites the sign-in tab may show

    def import_session(self, ctx: Context, tab: loginbrowser.LoginTab) -> None:
        jar = tab.cookies("esselunga.it")
        if not any(c["name"] == "rmbme-sso-x" or c["name"] == "access_token" for c in jar):
            raise NeedsAuth("not signed in to esselunga.it yet")
        ctx.state.session = vault.seal(ctx.settings.secret_key, {"cookies": jar})
        ctx.state.sync_requested = True

    def sync(self, ctx: Context) -> str:
        secret = ctx.settings.secret_key
        session = vault.unseal(secret, ctx.state.session)
        if not session:
            raise NeedsAuth("not connected: sign in to esselunga.it in the login browser and import the session")
        client = EsselungaClient(session["cookies"])
        try:
            card = client.open()
            today = datetime.now(ROME).date()
            last = ctx.state.cursor.get("last_success")
            since = date.fromisoformat(last) - timedelta(days=7) if last else today - FIRST_SYNC
            root = ctx.settings.receipts_dir
            backfill = bool(root) and not ctx.state.cursor.get("archive_backfilled")
            new = parsed = kept = 0
            for m in client.receipts(card, date.min if backfill else since):
                rid = str(m["id"])
                day = datetime.strptime(m["dataOperazione"], "%d/%m/%Y").date()
                has_pdf = bool(m.get("isPDF") and m.get("isPdfAvailable"))
                path = archive_path(root, ctx.state.user_id, ctx.state.id, day, rid) if root and has_pdf else None
                # receipts never change once issued; older ones than the sync window are only archived
                done = day < since or bool(ctx.db.scalar(select(Evidence.id).filter_by(connection_id=ctx.state.id, key=rid)))
                if done and (path is None or path.exists()):
                    continue
                pdf = None
                if has_pdf:
                    pdf = client.receipt_pdf(card, m["id"])
                    if pdf is not None and path is not None and not path.exists():
                        keep(path, pdf)
                        kept += 1
                        time.sleep(1)  # a backfill can be hundreds of receipts: don't hammer the site
                if done:
                    continue
                new += 1
                if pdf is None:
                    record_evidence(ctx.db, ctx.state, rid, "purchase", {"total": str(m["importo"]), "date": m["dataOperazione"]},
                                    "no receipt PDF: bank payment left as is" if not has_pdf else "Esselunga served an empty receipt PDF")
                    continue
                p, issue = to_purchase(rid, m.get("descrizioneCommerciale", ""), parse_receipt(pdf_text(pdf)))
                if p is None:
                    record_evidence(ctx.db, ctx.state, rid, "purchase", {"total": str(m["importo"]), "date": m["dataOperazione"]}, issue)
                    continue
                if p.total != m["importo"]:
                    issue = f"receipt total €{p.total} differs from the listed €{m['importo']}"
                    record_evidence(ctx.db, ctx.state, rid, "purchase", purchases.to_json(p), issue)
                    continue
                purchases.record(ctx.db, ctx.state, p)
                parsed += 1
            ctx.state.cursor = {**ctx.state.cursor, "last_success": today.isoformat(), "archive_backfilled": bool(root)}
            return f"{new} new receipts, {parsed} parsed" + (f", {kept} PDFs archived" if root else "")
        finally:
            # The site rotates its tokens in cookies as we browse: keep the newest ones, success or not.
            ctx.state.session = vault.seal(secret, {"cookies": client.cookies()})
            ctx.db.commit()


register(Esselunga())
