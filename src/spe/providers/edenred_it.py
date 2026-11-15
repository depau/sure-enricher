"""Edenred Ticket Restaurant Italia (beneficiari.edenred.it). Observed on the owner's account, 2026-11-12.

Unattended path, verified over plain HTTP:
  1. POST sso.eu.edenred.io/connect/token  grant_type=refresh_token, client_id=<public web client>
     -> access_token (1 h) + a NEW refresh_token (rotation: the old one stops working)
  2. GET  beneficiari.edenred.it/nup/sso?redirectPage=...&accessToken=...  -> Liferay session cookies
  3. GET  the portal page -> Liferay.authToken (p_auth)
  4. POST /api/jsonws/invoke  cmd={"/RIS_Beneficiari-portlet.restinsoap/<method>": {...}}, p_auth
     get-session-object (cards, ids), pb.getSaldoCarte (balance per support), pb.getLogOperazioni (movements)
The first refresh token comes from an interactive login in a browser (password + SMS OTP), imported via CDP.
Card, Cloud and smartphone are supports of ONE voucher pool (Conto BPE): one Sure account, moves ignored.
"""

import json
import re
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from spe import loginbrowser, vault, vouchers
from spe.db import Connection, Evidence
from spe.providers import Blocked, Context, NeedsAuth, register
from spe.vouchers import ROME, Kind, VoucherMovement

SSO_TOKEN = "https://sso.eu.edenred.io/connect/token"
CLIENT_ID = "0f3ee1562a5244d1b484572c76cd6c40"  # public SPA client, from beneficiari.edenred.it/assets/ground-config.json
PORTAL = "https://beneficiari.edenred.it"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
WALLET = "ticket_restaurant"
FIRST_SYNC = timedelta(days=365)
LOOKBACK = timedelta(days=21)  # late postings: re-read three weeks before the last successful sync
# Operation codes, from pb.getElencoTipoOperazioneCloud on the owner's account (order of tipoOper below).
KINDS = {
    "01": Kind.ISSUANCE,  # Ordine di BUONI/VOUCHER sulla TESSERA
    "03": Kind.ISSUANCE,  # Ordine di BUONI/VOUCHER su CLOUD
    # "Ricarica ... sulla TESSERA" happens in daytime, at a POS, for amounts the employer never issued: the card
    # pulling vouchers from Cloud. Read as income, it took a real account's balance below zero a year back; as a move it fits.
    "02": Kind.TRANSFER,
    "04": Kind.REDEMPTION,  # Utilizzo BUONI/VOUCHER presso <esercente>
    "20": Kind.TRANSFER, "21": Kind.TRANSFER, "22": Kind.TRANSFER, "23": Kind.TRANSFER, "24": Kind.TRANSFER,
}
TIPO_OPER = "01|03|02|04|20|21|22|23|24"


class EdenredClient:
    def __init__(self, refresh_token: str, transport: httpx.BaseTransport | None = None):
        self.refresh_token = refresh_token
        self.http = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"},
                                 follow_redirects=True, timeout=30, transport=transport)
        self.p_auth: str | None = None

    def refresh(self) -> str:
        """Returns the rotated refresh token: persist it before doing anything else."""
        r = self.http.post(SSO_TOKEN, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": CLIENT_ID})
        if r.status_code == 400 and "invalid_grant" in r.text:
            raise NeedsAuth("Edenred session expired: sign in again from the dashboard")
        r.raise_for_status()
        t = r.json()
        self.refresh_token, self.access_token = t["refresh_token"], t["access_token"]
        return self.refresh_token

    def open_portal(self) -> None:
        r = self.http.get(f"{PORTAL}/nup/sso", params={"redirectPage": "/group/portale-beneficiari?language=it-IT", "accessToken": self.access_token})
        r.raise_for_status()
        m = re.search(r"Liferay\.authToken\s*=\s*['\"]([^'\"]+)", r.text)
        if not m or "/group/portale-beneficiari" not in str(r.url):
            raise Blocked(f"Edenred portal did not open a session (landed on {r.url.path}): layout or access changed")
        self.p_auth = m.group(1)

    def invoke(self, method: str, args: dict) -> dict:
        cmd = {f"/RIS_Beneficiari-portlet.restinsoap/{method}": args}
        r = self.http.post(f"{PORTAL}/api/jsonws/invoke", data={"cmd": json.dumps(cmd), "p_auth": self.p_auth},
                           headers={"X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "exception" in data:
            raise RuntimeError(f"Edenred {method}: {str(data['exception'])[:150]}")
        return data

    def web_service(self, operation: str, parameter: dict) -> dict:
        return self.invoke("call-web-service", {"operationKey": operation, "parameter": json.dumps(parameter)})

    def account(self) -> dict:
        # The portal's own JS sends this placeholder username; the server resolves the session user.
        # Right after the SSO handoff the session object is sometimes still empty (seen twice): retry briefly.
        for attempt in range(4):
            so = self.invoke("get-session-object", {"parameter": json.dumps({"username": "usr28082020"})})
            smart = next((p for p in so.get("prodotti") or [] if p.get("type") == "SMART"), None)
            if smart:
                sd = smart["sessionDataBenaProd"]
                return {"circuit": smart["circuito"]["codice"], "cards": sd["smartCards"], "user": sd["beneficiario"]}
            time.sleep(2 * (attempt + 1))
        raise RuntimeError("Edenred portal session has no Ticket Restaurant product (empty session object); retrying later")

    def balance(self, acct: dict) -> Decimal:
        """EUR value of every voucher on every support (effective + pending), from the per-value breakdown."""
        total = Decimal(0)
        for c in acct["cards"]:
            s = self.web_service("pb.getSaldoCarte", {
                "username": acct["user"]["authUsername"], "tipo": "1", "circuito": acct["circuit"],
                "carteValues": f"{c['dataFineValidita']};{c['cardCdSid']}|", "codTipoGruppoBadge": c["tipoCarta"],
                "datiSaldo": True, "datiSaldiScadenzaValBuono": True, "datiDettagliOperazioni": False,
            })
            if "saldoTotale" not in s:
                raise RuntimeError(f"balance response without saldoTotale for a {c['descTipoCarta']} support")
            for lot in s.get("dettagliSaldoTotale") or []:
                total += Decimal(lot["valore"].replace(".", "").replace(",", ".")) * int(lot["quantita"])
        return total

    def movements(self, acct: dict, since: date, until: date) -> list[dict]:
        u = acct["user"]
        params = ";".join([
            "flTitolari:1", f"circCd:{acct['circuit']}", f"dataDa:{since:%d/%m/%Y}", f"dataA:{until:%d/%m/%Y}",
            "listaCarte:" + ",".join(c["cardCdSid"] for c in acct["cards"]), f"tipoOper:{TIPO_OPER}", f"matricola:{u['dipeCd']}",
        ])
        return self.web_service("pb.getLogOperazioni", {"username": u["authUsername"], "params": params}).get("return") or []


def normalize(rows: list[dict]) -> list[VoucherMovement]:
    """One movement per checkout: the portal lists every voucher of a payment as its own row. A checkout is its
    second, operation and shop, not its till: Edenred changes the till once a payment settles (seen 2026-11-15,
    the same €64 under till 000014242 at night and 000290153 by morning). ponytail: two tills of one shop paying
    in the same second become one movement, with the right total."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        d = (r.get("elencoDettagli") or [{}])[0]
        groups[(r["dataOperazione"], r["codOperazione"], d.get("eserCd"))].append(r)
    out = []
    for (when, code, shop), rs in groups.items():
        at = datetime.strptime(when, "%d/%m/%Y %H:%M:%S").replace(tzinfo=ROME)
        amount = sum((Decimal(r["valBuono"]) * int(r.get("numBuoni") or 1) for r in rs), Decimal(0))
        desc = rs[0]["descOperazione"].removeprefix("Utilizzo BUONI/VOUCHER presso ").strip()
        kind = KINDS.get(code, Kind.OTHER)
        out.append(VoucherMovement(
            wallet=WALLET, booked_on=at.date(), amount=amount, kind=kind, description=desc,
            source_id=f"{code}:{at:%Y%m%d%H%M%S}:{shop or '-'}", occurred_at=at,
            merchant=vouchers.merchant_name(desc) if kind != Kind.ISSUANCE else None,
        ))
    return out


def keep_first_ids(moves: list[VoucherMovement], known_keys: list[str]) -> list[VoucherMovement]:
    """Movements already written under the older id, which ended with the till, keep that id (their key in Sure).
    One written under two ids (the till changed between two reads) is already in Sure twice: not written again."""
    older: dict[str, list[str]] = defaultdict(list)
    for key in known_keys:
        sid = key.removeprefix(f"id:{WALLET}:")
        if sid.count(":") == 3:
            older[sid.rsplit(":", 1)[0]].append(sid)
    out = []
    for m in moves:
        ids = older.get(m.source_id or "", [])
        if len(ids) <= 1:
            out.append(replace(m, source_id=ids[0]) if ids else m)
    return out


def session_from_browser(tab: loginbrowser.LoginTab) -> dict:
    """The refresh token the portal keeps once the owner signed in (only readable on the portal's own origin)."""
    raw = tab.evaluate(f"location.origin === '{PORTAL}' ? localStorage.getItem('refreshToken') : null")
    if raw is None:
        raise NeedsAuth("not back on beneficiari.edenred.it yet: finish signing in")
    stored = json.loads(raw) if raw else None
    if not stored or not stored.get("data"):
        raise NeedsAuth("the Edenred tab is not signed in yet")
    return {"refresh_token": stored["data"], "imported_at": datetime.now(ROME).isoformat()}


class EdenredIT:
    name = "edenred_it"
    label = "Edenred Ticket Restaurant (Italia)"
    interval = timedelta(hours=12)

    login_url = f"{PORTAL}/"
    domains = ("edenred.it", "edenred.io")  # portal + SSO (sso.eu.edenred.io): the only sites the sign-in tab may show

    def import_session(self, ctx: Context, tab: loginbrowser.LoginTab) -> None:
        ctx.state.session = vault.seal(ctx.settings.secret_key, session_from_browser(tab))
        ctx.state.sync_requested = True

    def sync(self, ctx: Context) -> str:
        secret = ctx.settings.secret_key
        session = vault.unseal(secret, ctx.state.session)
        if not session:
            raise NeedsAuth("not connected: sign in to Edenred and import the session from the dashboard")
        client = EdenredClient(session["refresh_token"])
        # Rotation: the old token is dead once this returns, so persist the new one first.
        ctx.state.session = vault.seal(secret, {**session, "refresh_token": client.refresh()})
        ctx.db.commit()
        client.open_portal()
        acct = client.account()
        today = datetime.now(ROME).date()
        last = ctx.state.cursor.get("last_success")
        since = date.fromisoformat(last) - LOOKBACK if last else today - FIRST_SYNC
        rows = client.movements(acct, since, today)
        balance = client.balance(acct)
        cfg = ctx.state.config or {}
        accounts = {WALLET: cfg["account_id"]} if cfg.get("account_id") else {}
        known = ctx.db.scalars(select(Evidence.key).filter_by(connection_id=ctx.state.id, kind="voucher_movement")).all()
        summary = vouchers.stage(ctx.db, ctx.state, keep_first_ids(normalize(rows), list(known)), accounts)
        ctx.state.cursor = {**ctx.state.cursor, "last_success": today.isoformat(), "balance": str(balance)}
        mismatch = vouchers.balance_mismatch(ctx.db, ctx.sure, cfg.get("account_id"), balance, "Edenred")
        if mismatch:
            ctx.state.status = "degraded"
            return f"{summary}; {mismatch}"
        return f"{summary}; Edenred balance €{balance}"


def opening_balance(db: Session, conn: Connection) -> tuple[Decimal, date, Decimal] | None:
    """What to type in Sure's new-account form so the wallet account lands on Edenred's balance once the movements
    read so far are written: (opening balance, opening date, Edenred balance). None before the first read."""
    balance = (conn.cursor or {}).get("balance")
    payloads = db.scalars(select(Evidence.payload).filter_by(connection_id=conn.id, kind="voucher_movement")).all()
    moves = [p for p in payloads if not p.get("pending") and p["kind"] in vouchers.NATURE]
    if balance is None or not moves:
        return None
    net = sum(Decimal(p["amount"]) * (1 if vouchers.NATURE[p["kind"]] == "income" else -1) for p in moves)
    first = min(date.fromisoformat(p["booked_on"]) for p in moves)
    return Decimal(balance) - net, first - timedelta(days=1), Decimal(balance)


register(EdenredIT())
