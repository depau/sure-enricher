"""Web UI. Every page belongs to the user signed in through Sure (spe.auth); served under SPE_PUBLIC_URL's path.

Opening any page without a session starts Sure's OAuth sign-in, which also gives the daemon this user's
token for the API and MCP. Session: a sealed cookie (SameSite=Lax), plus an Origin check on every POST.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, pass_context, select_autoescape
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from spe import livelogin, vault, views
from spe.i18n import Tr, language
from spe.auth import SureAuth
from spe.db import Connection, Evidence, SureOp, User, now
from spe.loginbrowser import LoginTab
from spe.providers import Context, NeedsAuth
from spe.providers.edenred_it import opening_balance
from spe.scheduler import Scheduler
from spe.sure import SureError
from spe.vouchers import ROME

log = logging.getLogger(__name__)
SESSION, LOGIN = "spe_session", "spe_login"
SESSION_DAYS = 30
HISTORY_PAGE = 50
IMPORT_FAILED = {
    "unreachable": "The login browser is not responding: try again shortly, or ask whoever runs the server.",
    "signed-out": "The sign-in doesn't look complete yet: finish signing in, then import the session.",
}
LOGIN_TTL = timedelta(minutes=20)  # an abandoned sign-in is erased after this


@dataclass
class Login:
    tab: LoginTab
    user_id: int
    touched: datetime
    provider: str


INTERVALS = [(6, "Every 6 hours"), (12, "Every 12 hours"), (24, "Every day")]
ACCOUNT_KINDS = {"depository": "Bank account", "credit_card": "Credit card", "investment": "Investments", "crypto": "Crypto",
                 "property": "Property", "vehicle": "Vehicle", "other_asset": "Other asset", "loan": "Loan", "other_liability": "Other liability"}


@dataclass(frozen=True)
class Pick:
    key: str
    label: str
    placeholder: str
    types: tuple[str, ...]  # Sure account_type values it accepts
    required: bool


@dataclass(frozen=True)
class ProviderUI:
    name: str
    title: str
    pitch: str
    subtitle: str
    color: str
    site: str = ""
    picks: tuple[Pick, ...] = field(default=())


PROVIDERS = {
    "edenred_it": ProviderUI("Edenred", "Edenred Ticket Restaurant", "Sign in in the login browser · then choose the voucher account",
                             "Ticket Restaurant", "#E4002B", "edenred.it",
                             (Pick("account_id", "Voucher account in Sure", "Choose the voucher account…", ("depository", "other_asset"), True),)),
    "esselunga": ProviderUI("Esselunga", "Esselunga", "Sign in in the login browser · in-store receipts", "in-store receipts",
                            "#0B5BA8", "esselunga.it"),
    "amazon_it": ProviderUI("Amazon", "Amazon (Europe)", "Sign in on amazon.it · orders from every European store (.it, .de, .fr, .es…)",
                            "orders and refunds", "#FF9900", "amazon.it",
                            (Pick("gift_card_account", "Gift card account in Sure", "No gift card account", ("depository", "other_asset"), False),)),
    "aliexpress": ProviderUI("AliExpress", "AliExpress", "Sign in in the login browser · orders and refunds", "orders and refunds",
                             "#E62E04", "aliexpress.com"),
    "paypal_payin3": ProviderUI("PayPal Pay in 3", "PayPal Pay in 3", "No sign-in · choose the PayPal account and a loan",
                                "from the PayPal account in Sure", "#003087", "",
                                (Pick("paypal_account", "PayPal account in Sure", "Choose the PayPal account…",
                                      ("depository", "other_asset", "credit_card"), True),
                                 Pick("loan_account", "Loan that holds the instalment debt", "Choose a Loan account…", ("loan",), False))),
}
STATUS = {"connected": ("ok", "Connected"), "never_run": ("", "Waiting"), "syncing": ("", "Running"), "needs_auth": ("warn", "Reconnect needed"),
          "blocked": ("warn", "Blocked"), "error": ("bad", "Error"), "degraded": ("warn", "To check")}


templates = Environment(loader=PackageLoader("spe"), autoescape=select_autoescape(default=True))
templates.filters["eur"] = pass_context(lambda ctx, d: views.eur(d, ctx["_"]))
templates.filters["when"] = pass_context(lambda ctx, d: views.when(d, ctx["_"]))
JS_STRINGS = ["live", "loading…", "disconnected: reload the page", "Session imported", "Signed in: session imported",
              "The site showed a notice (confirmed): %(text)s", "Blocked %(host)s: only %(name)s sites can be opened here",
              "Pasted%(where)s · %(n)s characters", "Sent%(where)s · %(n)s characters", " into the %(field)s field",
              " to the %(field)s field", "selected in the browser"]  # used by login.html's script


def badge(c: Connection) -> tuple[str, str]:
    ui, cfg = PROVIDERS[c.provider], c.config or {}
    if not c.enabled:
        return "", "Paused"
    if any(p.required and not cfg.get(p.key) for p in ui.picks):
        return "warn", "To set up"
    if c.provider == "paypal_payin3" and not cfg.get("loan_account"):
        return "warn", "Loan missing"
    return STATUS.get(c.status, ("", c.status))


class LoginRequired(Exception):
    pass


async def _form(request: Request) -> dict[str, list[str]]:
    return parse_qs((await request.body()).decode(), keep_blank_values=True)


def create_app(sched: Scheduler, auth: SureAuth) -> FastAPI:
    s, engine = sched.settings, sched.engine
    base = s.base_path
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(s.public_url))
    cookie = {"httponly": True, "secure": s.public_url.startswith("https"), "samesite": "lax", "path": base or "/"}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def db() -> Session:
        return Session(engine)

    def session_user(cookies: dict[str, str]) -> User | None:
        data = vault.unseal(s.secret_key, cookies.get(SESSION, "").encode() or None)
        if data and datetime.fromisoformat(data["until"]) > now():
            with db() as d:
                user = d.get(User, data["uid"])
                if user is not None and user.status != "disconnected":
                    d.expunge(user)
                    return user
        return None

    def me(request: Request) -> User:
        user = session_user(request.cookies)
        if user is None:
            raise LoginRequired(request.url.path)
        if request.method == "POST" and request.headers.get("origin", origin) != origin:
            raise HTTPException(403, "cross-origin form post refused")
        return user

    Me = Annotated[User, Depends(me)]

    @app.exception_handler(LoginRequired)
    def _login(request: Request, exc: LoginRequired) -> RedirectResponse:
        url, pending = auth.start(next_url=str(exc) if request.method == "GET" else f"{base}/")
        resp = RedirectResponse(url, status_code=303)
        resp.set_cookie(LOGIN, pending.decode(), max_age=900, **cookie)
        return resp

    def tr_for(user: User) -> Tr:
        return Tr(language(user.locale))  # the language the user's Sure family is set to

    def render(name: str, user: User, page: str, title: str, error: str | None = None, **ctx: object) -> HTMLResponse:
        tr = tr_for(user)
        with db() as d:
            planned = d.scalar(select(func.count(func.distinct(SureOp.group_key))).where(SureOp.user_id == user.id, SureOp.status == "planned"))
            held = views.needs_owner(d, user.id)
        html = templates.get_template(name).render(
            base=base, user=user, page=page, sure_url=s.sure_public_url, public_host=s.public_url.split("://", 1)[-1], _=tr,
            title=tr(title), error=tr(error) if error else None, js_strings={k: tr(k) for k in JS_STRINGS},
            counts={"planned": planned, "held": held}, login_browser=bool(s.login_browser_cdp), providers=PROVIDERS, **ctx)
        return HTMLResponse(html)

    def back(to: str = "/") -> RedirectResponse:
        sched.wake.set()
        return RedirectResponse(f"{base}{to}", status_code=303)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        with db() as d:
            d.execute(text("select 1"))
        alive = sched.thread.is_alive() and sched.heartbeat is not None
        return JSONResponse({"scheduler": alive, "writes_halted": sched.writes_halted}, 200 if alive else 503)

    @app.get("/auth/callback")
    def callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        if error or not code:
            raise HTTPException(400, f"Sure did not authorize the sign-in: {error or 'no code'}")
        try:
            user, next_url = auth.finish(code, state, request.cookies.get(LOGIN, "").encode() or None)
        except PermissionError as e:
            raise HTTPException(400, str(e)) from e
        resp = RedirectResponse(next_url if next_url.startswith(base or "/") else f"{base}/", status_code=303)
        until = (now() + timedelta(days=SESSION_DAYS)).isoformat()
        resp.set_cookie(SESSION, vault.seal(s.secret_key, {"uid": user.id, "until": until}).decode(), max_age=SESSION_DAYS * 86400, **cookie)
        resp.delete_cookie(LOGIN, path=cookie["path"])
        return resp

    @app.post("/auth/logout")
    def logout(user: Me) -> RedirectResponse:
        resp = RedirectResponse(s.sure_public_url, status_code=303)
        resp.delete_cookie(SESSION, path=cookie["path"])
        return resp

    @app.get("/status")
    def status(user: Me) -> dict:
        with db() as d:
            conns = d.scalars(select(Connection).where(Connection.user_id == user.id)).all()
            counts = dict(d.execute(select(SureOp.status, func.count()).where(SureOp.user_id == user.id).group_by(SureOp.status)).all())
            held = d.scalar(select(func.count()).select_from(Evidence).where(Evidence.user_id == user.id, Evidence.issue.is_not(None)))
            return {
                "connections": {c.id: {"provider": c.provider, "status": c.status, "message": c.message,
                                       "last_success_at": c.last_success_at, "next_run_at": c.next_run_at} for c in conns},
                "ops": counts, "held_evidence": held, "write_error": sched.write_errors.get(user.id) or sched.writes_halted,
            }

    # --- connections ----------------------------------------------------------------------------------------

    def own_connection(d: Session, user: User, cid: int) -> Connection:
        c = d.get(Connection, cid)
        if c is None or c.user_id != user.id:
            raise HTTPException(404)
        return c

    def can_import(c: Connection) -> bool:
        return bool(s.login_browser_cdp) and hasattr(sched.providers.get(c.provider), "import_session")

    def row(d: Session, c: Connection, accounts: dict[str, dict], open_id: int | None, tr: Tr) -> dict:
        ui, cfg = PROVIDERS[c.provider], c.config or {}

        def name(key: str) -> str | None:
            v = cfg.get(key)
            return (accounts[v]["name"] if v in accounts else "account not visible") if v else None

        fields = [{"key": p.key, "label": p.label, "placeholder": p.placeholder, "value": cfg.get(p.key),
                   "options": [{"id": a["id"], "name": a["name"], "kind": ACCOUNT_KINDS.get(a["account_type"], a["account_type"])}
                               for a in accounts.values() if a["account_type"] in p.types],
                   "ids": {a["id"] for a in accounts.values() if a["account_type"] in p.types}} for p in ui.picks]
        if c.provider == "edenred_it":
            where = [("in ", False), (name("account_id") or "no account chosen", bool(cfg.get("account_id")))]
        elif c.provider == "paypal_payin3":
            where = [("from ", False), (name("paypal_account") or "no account", bool(cfg.get("paypal_account"))), (" · loan ", False),
                     (name("loan_account") or "not chosen", bool(cfg.get("loan_account")))]
        else:
            where = [("payments from ", False), ("all your accounts", True)]
        every = tr(dict(INTERVALS)[c.interval_hours]).lower() if c.interval_hours in dict(INTERVALS) else tr("every %(n)s hours", n=c.interval_hours)
        last = c.last_success_at.astimezone(ROME).strftime("%d/%m %H:%M") if c.last_success_at else tr("never read")
        needs_login = c.status in ("needs_auth", "never_run") and hasattr(sched.providers.get(c.provider), "import_session")
        if needs_login:
            help_ = "help_login.html"
        elif c.provider == "edenred_it" and not cfg.get("account_id"):
            help_ = "help_edenred.html"
        elif c.provider == "paypal_payin3" and not cfg.get("loan_account"):
            help_ = "help_paypal.html"
        elif c.provider == "amazon_it" and not cfg.get("gift_card_account") and "opening" in (c.cursor or {}).get("gift_card", {}):
            help_ = "help_giftcard.html"
        else:
            help_ = None
        opening = opening_balance(d, c) if help_ == "help_edenred.html" else None
        if help_ == "help_giftcard.html":
            gc = c.cursor["gift_card"]  # read once in full: the ledger's balance before its first row
            opening = (Decimal(gc["opening"][0]), date.fromisoformat(gc["opening"][1]), Decimal(gc["balance"]))
        return {"c": c, "ui": ui, "badge": badge(c), "where": where, "when": f"{every} · {last}", "fields": fields,
                "open": c.id == open_id, "reconnect": needs_login and can_import(c), "help": help_, "opening": opening}

    def sure_accounts(user: User) -> tuple[dict[str, dict], str | None]:
        try:
            return {a["id"]: a for a in auth.client_for(user.id).accounts()}, None
        except SureError as e:
            return {}, tr_for(user)("Can't read the accounts from Sure (%(status)s): account choices unavailable.", status=e.status)

    @app.get("/connections", response_class=HTMLResponse)
    def connections(user: Me, open: int | None = None, failed: str | None = None) -> HTMLResponse:
        accounts, error = sure_accounts(user)
        error = IMPORT_FAILED.get(failed or "", error)
        with db() as d:
            conns = d.scalars(select(Connection).where(Connection.user_id == user.id, Connection.provider.in_(PROVIDERS)).order_by(Connection.id)).all()
            rows = [row(d, c, accounts, open, tr_for(user)) for c in conns]
            return render("connections.html", user, "connections", title="Connections", rows=rows, intervals=INTERVALS, error=error)

    @app.get("/connections/new", response_class=HTMLResponse)
    def new_connection(user: Me) -> HTMLResponse:
        offered = [(k, ui) for k, ui in PROVIDERS.items() if k in sched.providers]
        return render("new_connection.html", user, "connections", title="Add connection", offered=offered)

    @app.post("/connections")
    async def create_connection(request: Request, user: Me) -> RedirectResponse:
        provider = (await _form(request)).get("provider", [""])[0]
        p = sched.providers.get(provider)
        if p is None or provider not in PROVIDERS:
            raise HTTPException(422, "unknown provider")
        with db() as d:
            c = Connection(user_id=user.id, provider=provider, interval_hours=int(p.interval.total_seconds() // 3600), config={})
            d.add(c)
            d.commit()
            cid = c.id
            if can_import(c):  # signing in is the next step anyway
                return back(f"/connections/{cid}/login")
        return back(f"/connections?open={cid}#c{cid}")

    @app.post("/connections/{cid}")
    async def save_connection(cid: int, request: Request, user: Me) -> RedirectResponse:
        form = {k: v[-1].strip() for k, v in (await _form(request)).items()}
        accounts, error = sure_accounts(user)
        with db() as d:
            c = own_connection(d, user, cid)
            cfg = dict(c.config or {})
            for p in PROVIDERS[c.provider].picks:
                v = form.get(p.key) or None
                if v and v != cfg.get(p.key) and (v not in accounts or accounts[v]["account_type"] not in p.types):
                    raise HTTPException(422, error or f"{p.label}: account not available")
                cfg[p.key] = v
            if c.provider == "paypal_payin3":
                since = form.get("since") or None
                if since:
                    date.fromisoformat(since)  # ValueError -> 500 is fine for a hand-crafted post
                cfg["since"] = since
            hours = int(form.get("interval_hours", c.interval_hours))
            if hours not in dict(INTERVALS):
                raise HTTPException(422, "interval")
            c.label, c.config, c.interval_hours = (form.get("label") or None), cfg, hours
            c.enabled = form.get("enabled") == "1"
            c.sync_requested = c.enabled  # re-read with the new choices
            d.commit()
        return back(f"/connections?open={cid}#c{cid}")

    @app.post("/connections/{cid}/delete")
    def delete_connection(cid: int, user: Me) -> RedirectResponse:
        with db() as d:
            d.delete(own_connection(d, user, cid))  # evidence cascades; Sure is untouched
            d.commit()
        return back("/connections")

    @app.post("/connections/{cid}/sync")
    def sync_now(cid: int, user: Me) -> RedirectResponse:
        with db() as d:
            own_connection(d, user, cid).sync_requested = True
            d.commit()
        return back(f"/connections?open={cid}#c{cid}")

    # --- live sign-in in the login browser ---------------------------------------------------------------------

    logins: dict[int, Login] = {}  # connection id -> its open sign-in (one process: in memory is enough)

    def sweep() -> None:
        for cid, login in list(logins.items()):
            if now() - login.touched > LOGIN_TTL:
                close_login(cid)

    def close_login(cid: int) -> None:
        login = logins.pop(cid, None)
        if login is not None:
            try:
                login.tab.close()
            except Exception:  # noqa: BLE001 - the browser restarted: the context is gone already
                log.warning("could not dispose the sign-in of connection %s", cid)

    def own_login(user: User, cid: int) -> Login:
        login = logins.get(cid)
        if login is None or login.user_id != user.id:
            raise HTTPException(404, "no sign-in in progress")
        login.touched = now()
        return login

    @app.get("/connections/{cid}/login", response_class=HTMLResponse)
    def login_page(cid: int, user: Me, failed: str | None = None) -> Response:
        sweep()
        with db() as d:
            c = own_connection(d, user, cid)
            if not can_import(c):
                raise HTTPException(404)
            if cid not in logins or logins[cid].user_id != user.id:
                close_login(cid)
                try:
                    tab = LoginTab(s.login_browser_cdp, sched.providers[c.provider].login_url)
                    tab.dispose_others({x.tab.context for x in logins.values()})
                except (httpx.HTTPError, OSError, RuntimeError):
                    log.exception("login browser unavailable")
                    return back(f"/connections?open={cid}&failed=unreachable#c{cid}")
                logins[cid] = Login(tab, user.id, now(), c.provider)
            return render("login.html", user, "connections", title=tr_for(user)("Reconnect %(name)s", name=tr_for(user)(PROVIDERS[c.provider].name)), c=c, ui=PROVIDERS[c.provider],
                          error=IMPORT_FAILED.get(failed or ""))

    @app.websocket("/connections/{cid}/login/ws")
    async def login_ws(websocket: WebSocket, cid: int) -> None:
        user = session_user(websocket.cookies)
        login = logins.get(cid)
        if user is None or websocket.headers.get("origin") != origin or login is None or login.user_id != user.id:
            await websocket.close(code=4403)  # also refuses other sites' pages (cross-site websocket hijacking)
            return
        await websocket.accept()
        login.touched = now()
        provider = sched.providers[login.provider]
        if await livelogin.bridge(websocket, login.tab.page_ws, provider.domains, provider.login_url,
                                  lambda: import_session(user, cid, login)):
            close_login(cid)

    def import_session(user: User, cid: int, login: Login) -> bool:
        """The import behind the button and the automatic one: False while the site does not look signed in."""
        with db() as d:
            c = own_connection(d, user, cid)
            try:
                sched.providers[c.provider].import_session(Context(d, auth.client_for(user.id), s, c), login.tab)
            except NeedsAuth:
                return False
            d.commit()
        sched.wake.set()
        return True

    @app.post("/connections/{cid}/login/import")
    def login_import(cid: int, user: Me) -> RedirectResponse:
        if not import_session(user, cid, own_login(user, cid)):
            return back(f"/connections/{cid}/login?failed=signed-out")
        close_login(cid)
        return back(f"/connections?open={cid}#c{cid}")

    @app.post("/connections/{cid}/login/cancel")
    def login_cancel(cid: int, user: Me) -> RedirectResponse:
        own_login(user, cid)
        close_login(cid)
        return back(f"/connections?open={cid}#c{cid}")

    # --- approvals, overview, history ------------------------------------------------------------------------

    @app.post("/ops/bulk")
    async def ops_bulk(request: Request, user: Me) -> RedirectResponse:
        form = await _form(request)
        moves = {"approve": ("planned", "approved"), "reject": ("planned", "rejected"), "retry": ("failed", "approved")}
        action = form.get("action", [""])[0]
        if action not in moves:
            raise HTTPException(422, "unknown action")
        frm, to = moves[action]
        ids = [int(i) for i in form.get("id", [])]
        with db() as d:
            for op in d.scalars(select(SureOp).where(SureOp.user_id == user.id, SureOp.id.in_(ids), SureOp.status == frm)):
                op.status, op.error = to, None
            d.commit()
        return back(urlsplit(request.headers.get("referer", "")).path.removeprefix(base) or "/")

    def ops_groups(d: Session, user: User, accounts: dict[str, dict], *statuses: str, limit: int | None = None,
                   offset: int = 0) -> list[views.Group]:
        q = select(SureOp).where(SureOp.user_id == user.id, SureOp.status.in_(statuses))
        if limit is None:
            return views.build(d, user.id, d.scalars(q.order_by(SureOp.id)).all(), accounts, tr_for(user))
        keys = d.scalars(select(SureOp.group_key).where(SureOp.user_id == user.id, SureOp.status.in_(statuses)).group_by(SureOp.group_key)
                         .order_by(func.max(SureOp.updated_at).desc()).limit(limit).offset(offset)).all()
        groups = views.build(d, user.id, d.scalars(q.where(SureOp.group_key.in_(keys)).order_by(SureOp.id)).all(), accounts, tr_for(user))
        return sorted(groups, key=lambda g: g.at, reverse=True)

    @app.get("/open/{tx_id}")
    def open_in_sure(tx_id: str, user: Me) -> RedirectResponse:
        """Sure's web routes take the entry id, the API hands out transaction ids: ask Sure, as this user, for the entry."""
        try:
            tx = auth.client_for(user.id).transaction(tx_id)
        except SureError as e:
            raise HTTPException(404 if e.status == 404 else 502, "transaction not found in Sure") from e
        return RedirectResponse(f"{s.sure_public_url}/transactions/{tx['entry_id']}", status_code=303)

    @app.get("/held", response_class=HTMLResponse)
    def held_page(user: Me) -> HTMLResponse:
        with db() as d:
            items = views.held(d, user.id, tr_for(user))
            return render("held.html", user, "held", title="On hold",
                          decide=[h for h in items if not h.ev.payload.get("ignored") and not h.waiting],
                          waiting=[h for h in items if not h.ev.payload.get("ignored") and h.waiting],
                          ignored=[h for h in items if h.ev.payload.get("ignored")])

    def own_evidence(d: Session, user: User, eid: int) -> Evidence:
        ev = d.get(Evidence, eid)
        if ev is None or ev.user_id != user.id:
            raise HTTPException(404)
        return ev

    @app.post("/held/{eid}")
    async def held_action(eid: int, request: Request, user: Me) -> RedirectResponse:
        action = (await _form(request)).get("action", [""])[0]
        with db() as d:
            ev = own_evidence(d, user, eid)
            if action in ("ignore", "restore"):
                ev.payload = {**ev.payload, "ignored": action == "ignore"}
            elif action == "retry" and ev.connection_id:
                d.get(Connection, ev.connection_id).sync_requested = True  # re-read the source; reconcile runs right after
            else:
                raise HTTPException(422, "unknown action")
            d.commit()
        return back("/held")

    @app.post("/held/{eid}/pick")
    async def held_pick(eid: int, request: Request, user: Me) -> RedirectResponse:
        form = await _form(request)
        leg, tx = form.get("leg", [""])[0], form.get("tx", [""])[0]
        with db() as d:
            ev = own_evidence(d, user, eid)
            if tx not in {c["id"] for c in (ev.payload.get("candidates") or {}).get(leg, [])}:
                raise HTTPException(422, "not one of the candidates")  # only what the matcher itself offered
            ev.payload = {**ev.payload, "picks": {**(ev.payload.get("picks") or {}), leg: tx}}
            d.commit()
        return back("/held")

    @app.get("/review", response_class=HTMLResponse)
    def review(user: Me, provider: str | None = None) -> HTMLResponse:
        accounts, error = sure_accounts(user)
        with db() as d:
            groups = ops_groups(d, user, accounts, "planned")
            failed = ops_groups(d, user, accounts, "failed")
            chips: dict[str, int] = {}
            for g in groups:
                chips[g.provider] = chips.get(g.provider, 0) + 1
            shown = [g for g in groups if not provider or g.provider == provider]
            return render("review.html", user, "review", title="To approve", groups=groups, shown=shown, failed=failed,
                          chips=sorted(chips.items(), key=lambda kv: -kv[1]), provider=provider, error=error)

    @app.get("/history", response_class=HTMLResponse)
    def history(user: Me, page: int = 0) -> HTMLResponse:
        accounts, error = sure_accounts(user)
        with db() as d:
            groups = ops_groups(d, user, accounts, "done", "rejected", "failed", limit=HISTORY_PAGE + 1, offset=page * HISTORY_PAGE)
            return render("history.html", user, "history", title="History", groups=groups[:HISTORY_PAGE], more=len(groups) > HISTORY_PAGE,
                          page_no=page, error=error)

    @app.get("/", response_class=HTMLResponse)
    def overview(user: Me) -> HTMLResponse:
        accounts, error = sure_accounts(user)
        tr = tr_for(user)
        with db() as d:
            conns = d.scalars(select(Connection).where(Connection.user_id == user.id, Connection.provider.in_(PROVIDERS)).order_by(Connection.id)).all()
            planned = ops_groups(d, user, accounts, "planned")
            n_failed = d.scalar(select(func.count(func.distinct(SureOp.group_key))).where(SureOp.user_id == user.id, SureOp.status == "failed"))
            activity = ops_groups(d, user, accounts, "done", limit=6)
            attention, sources = [], []
            for c in conns:
                ui, (cls, label) = PROVIDERS[c.provider], badge(c)
                name = c.label or tr(ui.name)
                if cls in ("warn", "bad"):
                    attention.append({"title": name, "text": tr(label).lower() + (f": {c.message}" if c.message and c.status in ("error", "blocked", "degraded") else ""),
                                      "href": f"{base}/connections/{c.id}/login" if c.status == "needs_auth" and can_import(c) else f"{base}/connections?open={c.id}#c{c.id}",
                                      "label": tr("Reconnect") if c.status == "needs_auth" and can_import(c) else tr("Open"),
                                      "warn": c.status == "needs_auth"})
                sources.append(source(d, c, ui, (cls, label), tr))
            n_held = views.needs_owner(d, user.id)
            if n_held:
                attention.append({"title": tr.n("1 purchase on hold", "%(n)s purchases on hold", n_held),
                                  "text": tr("needs your choice"), "href": f"{base}/held", "label": tr("See"), "warn": False})
            if n_failed:
                attention.append({"title": tr.n("1 change failed", "%(n)s changes failed", n_failed),
                                  "text": tr("Sure refused the write"), "href": f"{base}/review#failed", "label": tr("See"), "warn": False})
            edenred = [c for c in conns if c.provider == "edenred_it" and c.enabled and (c.cursor or {}).get("balance")]
            paypal = [c for c in conns if c.provider == "paypal_payin3" and c.enabled and "outstanding" in (c.cursor or {})]
            attempts = [c.last_attempt_at for c in conns if c.last_attempt_at]
            nexts = [c.next_run_at for c in conns if c.enabled and c.next_run_at]
            checked = " · ".join(x for x in (tr("Last check %(when)s", when=views.when(max(attempts), tr)) if attempts else tr("No check yet"),
                                             tr("next %(when)s", when=views.when(min(nexts), tr)) if nexts else "") if x)
            write_error = sched.write_errors.get(user.id) or sched.writes_halted
            return render(
                "overview.html", user, "overview", title="Overview", checked=checked, attention=attention, sources=sources,
                planned_groups=len(planned), planned_detail=views.planned_summary(planned, {k: tr(v.name) for k, v in PROVIDERS.items()}),
                week=views.done_since(d, user.id, 7), activity=activity,
                vouchers={"balance": sum(Decimal(c.cursor["balance"]) for c in edenred), "same": all(c.status == "connected" for c in edenred)} if edenred else None,
                payin3={"owed": sum(Decimal(c.cursor["outstanding"]) for c in paypal), "active": sum(c.cursor.get("active", 0) for c in paypal)} if paypal else None,
                error=tr("Writes to Sure: %(error)s", error=write_error) if write_error else error)

    def source(d: Session, c: Connection, ui: ProviderUI, b: tuple[str, str], tr: Tr) -> dict:
        cur = c.cursor or {}
        out = {"c": c, "name": ui.name, "badge": b, "reconnect": c.status == "needs_auth" and can_import(c), "l1_cls": "",
               "l2": tr("Last %(last)s · next %(next)s", last=views.when(c.last_success_at, tr), next=views.when(c.next_run_at, tr)) if c.enabled
               else tr("Paused")}
        if c.provider == "edenred_it":
            ok = c.status == "connected"
            out |= {"big": views.eur(cur.get("balance", 0), tr), "unit": "balance",
                    "l1": tr("balance checked against Sure") if ok and (c.config or {}).get("account_id") else (c.message or tr("balance not compared yet")),
                    "l1_cls": "ok" if ok else "warn"}
        elif c.provider == "paypal_payin3":
            out |= {"big": views.eur(cur.get("outstanding", 0), tr), "unit": "to repay",
                    "l1": tr("Pay in 3 · loan in Sure") if (c.config or {}).get("loan_account") else tr("Pay in 3 · notes only")}
        else:
            counts = dict(d.execute(select(Evidence.issue.is_(None), func.count()).where(Evidence.connection_id == c.id, Evidence.kind == "purchase")
                                    .group_by(Evidence.issue.is_(None))).all())
            linked, held = counts.get(True, 0), counts.get(False, 0)
            out |= {"big": str(linked + held), "unit": "receipts" if c.provider == "esselunga" else "orders",
                    "l1": tr("%(linked)s linked · %(held)s on hold", linked=linked, held=held) if held else tr("%(linked)s linked", linked=linked)}
            if (c.config or {}).get("gift_card_account") and "balance" in cur.get("gift_card", {}):
                out["l1"] += " · " + tr("gift card %(balance)s", balance=views.eur(cur["gift_card"]["balance"], tr))
        return out

    if not base:
        return app
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    outer.mount(base, app)
    outer.add_api_route(base, lambda: RedirectResponse(f"{base}/"), include_in_schema=False)
    outer.add_api_route("/healthz", healthz)  # probes hit the container directly, without the prefix
    outer.add_api_route("/readyz", readyz)
    return outer


