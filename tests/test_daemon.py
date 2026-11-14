from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import FakeSure, conn
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from spe import vault, writer
from spe.__main__ import main
from spe.auth import SureAuth
from spe.db import Connection, Evidence, SureOp, User, now
from spe.providers import NeedsAuth, discover
from spe.providers.edenred_it import opening_balance
from spe.scheduler import Scheduler, due, run_connection
from spe.settings import Settings
from spe.web import create_app

SETTINGS = Settings(sure_url="http://web:3000", secret_key="s", sure_public_url="https://sure.example",
                    public_url="https://sure.example/enrich")


class FakeProvider:
    name, label = "fake", "Fake"

    def __init__(self):
        self.outcomes = []

    def sync(self, ctx):
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeSureOAuth:
    """Sure's Doorkeeper endpoints as the daemon uses them; tokens are 'tok-<user>'."""

    def __init__(self, visible: dict[str, set[str]]):
        self.visible = visible  # sure user id -> account ids they can see
        self.refuse_refresh = False
        self.accounts = [{"id": "acc-pp", "name": "PayPal", "account_type": "depository"},
                         {"id": "acc-loan", "name": "Paga in 3", "account_type": "loan"},
                         {"id": "acc-other", "name": "Someone else's", "account_type": "loan"}]

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "web", "server-side calls must use SURE_URL"
        who = req.headers.get("authorization", "").removeprefix("Bearer tok-")
        if req.url.path == "/register":
            return httpx.Response(201, json={"client_id": "cid"})
        if req.url.path == "/oauth/token":
            form = parse_qs(req.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(400, json={"error": "invalid_grant"}) if self.refuse_refresh else \
                    httpx.Response(200, json={"access_token": f"tok-{form['refresh_token'][0]}", "refresh_token": form["refresh_token"][0], "expires_in": 31_536_000})
            user = form["code"][0]
            return httpx.Response(200, json={"access_token": f"tok-{user}", "refresh_token": user, "expires_in": 3600})  # expires soon
        if req.url.path == "/oauth/token/info":
            return httpx.Response(200, json={"resource_owner_id": who})
        if req.url.path == "/api/v1/family_settings":
            return httpx.Response(200, json={"id": "fam", "name": f"Family of {who}", "locale": "it"})
        if req.url.path == "/api/v1/accounts":
            mine = [a for a in self.accounts if a["id"] in self.visible.get(who, set())]
            return httpx.Response(200, json={"accounts": mine, "pagination": {"total_pages": 1}})
        if req.url.path.startswith("/api/v1/accounts/"):
            ok = req.url.path.rsplit("/", 1)[1] in self.visible.get(who, set())
            return httpx.Response(200 if ok else 404, json={"id": "x"} if ok else {"error": "not_found"})
        raise AssertionError(f"unexpected {req.method} {req.url}")


def sign_in(c: TestClient, user: str) -> None:
    r = c.get("/enrich/", follow_redirects=False)
    assert r.status_code == 303
    to = urlsplit(r.headers["location"])
    q = parse_qs(to.query)
    assert f"{to.scheme}://{to.netloc}{to.path}" == "https://sure.example/oauth/authorize"
    assert (q["redirect_uri"], q["scope"], q["code_challenge_method"]) == (["https://sure.example/enrich/auth/callback"], ["read_write"], ["S256"])
    r = c.get("/enrich/auth/callback", params={"code": user, "state": q["state"][0]}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/enrich/")


def test_connection_state_machine(db):
    p, sure = FakeProvider(), FakeSure().client()
    p.outcomes = [RuntimeError("upstream 500"), NeedsAuth("session expired"), "3 movements"]
    c = conn(db, "fake")

    run_connection(db, sure, SETTINGS, p, c)
    assert (c.status, c.failures) == ("error", 1)
    assert timedelta(minutes=13) < c.next_run_at - now() < timedelta(minutes=17)  # backoff, not interval

    run_connection(db, sure, SETTINGS, p, c)
    assert c.status == "needs_auth" and not due(c, now() + timedelta(days=30))

    c.sync_requested = True  # owner signed in again / pressed sync now
    assert due(c, now())
    run_connection(db, sure, SETTINGS, p, c)
    assert (c.status, c.failures, c.message) == ("connected", 0, "3 movements")
    assert c.next_run_at - now() > timedelta(hours=10)


def test_sign_in_through_sure_scopes_everything_to_that_user(engine):
    fake = FakeSureOAuth({"alice": {"acc-ticket"}})
    auth = SureAuth(engine, SETTINGS, transport=httpx.MockTransport(fake.handler))
    app = create_app(Scheduler(engine, SETTINGS, auth, {}), auth)
    bob, alice = TestClient(app, base_url="https://sure.example"), TestClient(app, base_url="https://sure.example")
    sign_in(alice, "alice")
    sign_in(bob, "bob")
    with Session(engine) as db:
        owner = db.scalar(select(User).filter_by(sure_user_id="alice")).id
        writer.stage_create(db, "edenred_it", {"account_id": "acc-ticket", "source": "s", "external_id": "x", "date": "2026-11-12"},
                            "x", approve=False, user_id=owner)
        db.commit()

    assert "Da approvare" in bob.get("/enrich/").text
    bob.post("/enrich/ops/bulk", data={"action": "approve", "id": "1"}, headers={"origin": "https://sure.example"})
    with Session(engine) as db:
        assert db.get(SureOp, 1).status == "planned"  # not his op: ignored

    assert "Family of alice" in alice.get("/enrich/").text
    approve = {"action": "approve", "id": "1"}
    assert alice.post("/enrich/ops/bulk", data=approve, headers={"origin": "https://evil.example"}).status_code == 403
    assert alice.post("/enrich/ops/bulk", data=approve, headers={"origin": "https://sure.example"}, follow_redirects=False).status_code == 303
    with Session(engine) as db:
        assert db.get(SureOp, 1).status == "approved"
        # the token Sure issued expires within a week: asking for it refreshes it (refresh grant -> 1 year)
        assert auth.token(owner) == "tok-alice"
        assert vault.unseal("s", db.get(User, owner).token)["expires_at"] > (now() + timedelta(days=300)).isoformat()


def test_user_sure_stops_accepting_is_disconnected_wiped_then_deletable(engine, monkeypatch):
    fake = FakeSureOAuth({})
    auth = SureAuth(engine, SETTINGS, transport=httpx.MockTransport(fake.handler))
    sched = Scheduler(engine, SETTINGS, auth, {"fake": FakeProvider()})
    sign_in(TestClient(create_app(sched, auth), base_url="https://sure.example"), "carol")
    with Session(engine) as db:
        user = db.scalar(select(User))
        c = Connection(user_id=user.id, provider="fake", session=b"sealed-login", next_run_at=now() + timedelta(days=1))
        db.add(c)
        db.commit()
        db.add(Evidence(user_id=user.id, connection_id=c.id, provider="fake", key="k", kind="purchase", payload={}))
        db.commit()

    fake.refuse_refresh = True  # e.g. carol was removed from Sure
    sched.tick()
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.status == "disconnected"
        uid = user.id
        assert db.scalar(select(Connection)).session == b"sealed-login"  # kept: she may come back
        user.status_since = now() - timedelta(days=31)
        db.commit()
    sched.tick()
    with Session(engine) as db:
        c = db.scalar(select(Connection))
        assert (c.session, c.status) == (None, "needs_auth")

    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    assert main(["users", "delete", str(uid), "--yes"]) == 0
    with Session(engine) as db:
        assert db.scalar(select(Connection)) is None and db.scalar(select(Evidence)) is None


def test_connections_are_added_configured_with_own_accounts_and_removed(engine):
    fake = FakeSureOAuth({"dana": {"acc-pp", "acc-loan"}})
    settings = replace(SETTINGS, login_browser_cdp="http://127.0.0.1:9")  # nothing listens there
    auth = SureAuth(engine, settings, transport=httpx.MockTransport(fake.handler))
    sched = Scheduler(engine, settings, auth, discover())
    c = TestClient(create_app(sched, auth), base_url="https://sure.example")
    sign_in(c, "dana")
    post = {"headers": {"origin": "https://sure.example"}, "follow_redirects": False}

    for page in ("/enrich/", "/enrich/review", "/enrich/history", "/enrich/connections", "/enrich/connections/new"):
        assert c.get(page).status_code == 200, page
    assert "PayPal Paga in 3 rate" in c.get("/enrich/connections/new").text
    r = c.post("/enrich/connections", data={"provider": "paypal_payin3"}, **post)
    assert r.headers["location"] == "/enrich/connections?open=1#c1"
    page = c.get("/enrich/connections?open=1").text
    assert "Da configurare" in page and "Paga in 3 · Prestito" in page and "Someone else" not in page

    form = {"label": "", "paypal_account": "acc-pp", "loan_account": "acc-other", "since": "", "interval_hours": "24", "enabled": "1"}
    assert c.post("/enrich/connections/1", data=form, **post).status_code == 422  # not hers
    assert c.post("/enrich/connections/1", data=form | {"loan_account": "acc-loan"}, **post).status_code == 303
    with Session(engine) as db:
        conn_ = db.get(Connection, 1)
        assert conn_.config == {"paypal_account": "acc-pp", "loan_account": "acc-loan", "since": None}
        assert (conn_.interval_hours, conn_.sync_requested) == (24, True)
    assert "Manca il prestito" not in c.get("/enrich/connections").text

    c.post("/enrich/connections", data={"provider": "amazon_it"}, **post)
    r = c.get("/enrich/connections/2/login", follow_redirects=False)  # the login browser isn't running
    assert r.headers["location"] == "/enrich/connections?open=2&failed=unreachable#c2"
    assert "Il browser di login non risponde" in c.get(r.headers["location"]).text
    assert c.post("/enrich/connections/1/delete", **post).status_code == 303
    with Session(engine) as db:
        assert db.get(Connection, 1) is None


def test_edenred_opening_balance_lands_on_the_portal_balance(db):
    c = conn(db, "edenred_it")
    c.cursor = {"balance": "80.00"}
    for key, day, kind, amount, pending in [("a", "2026-03-02", "issuance", "160.00", False), ("b", "2026-03-05", "redemption", "24.00", False),
                                            ("c", "2026-03-06", "transfer", "8.00", False), ("d", "2026-03-09", "redemption", "8.00", True),
                                            ("e", "2026-03-07", "expiry", "8.00", False)]:
        db.add(Evidence(user_id=1, connection_id=c.id, provider="edenred_it", key=key, kind="voucher_movement",
                        payload={"booked_on": day, "kind": kind, "amount": amount, "pending": pending}))
    db.commit()
    # only what gets written counts: 80 - (160 - 24) = -56 the day before the first movement
    assert opening_balance(db, c) == (D("-56.00"), date(2026, 3, 1), D("80.00"))


def test_review_shows_one_card_per_purchase_and_approves_it_whole(engine):
    fake = FakeSureOAuth({"erin": {"acc-pp"}})
    auth = SureAuth(engine, SETTINGS, transport=httpx.MockTransport(fake.handler))
    c = TestClient(create_app(Scheduler(engine, SETTINGS, auth, discover()), auth), base_url="https://sure.example")
    sign_in(c, "erin")
    marker = "[spe:esselunga:R1]"
    with Session(engine) as db:
        uid = db.scalar(select(User)).id
        conn_ = Connection(user_id=uid, provider="esselunga")
        db.add(conn_)
        db.commit()
        db.add(Evidence(user_id=uid, connection_id=conn_.id, provider="esselunga", key="R1", kind="purchase",
                        payload={"id": "R1", "merchant": "Esselunga di Prova", "date": "2026-11-10", "total": "3.50",
                                 "payments": [{"method": "card", "amount": "3.50"}],
                                 "lines": [{"description": "PANE", "amount": "1.20"}, {"description": "LATTE", "amount": "2.30"}]}))
        before = {"name": "ESSELUNGA SPA", "amount_decimal": "3.50", "date": "2026-11-10", "notes": None}
        for kind, payload in [("update", {"notes": f"Esselunga R1 {marker}"}),
                              ("split", {"splits": [{"name": "PANE", "amount": "1.20"}, {"name": "LATTE (parte)", "amount": "2.30"}]})]:
            writer.stage(db, user_id=uid, group=marker, key=f"{kind}:tx1:{marker}", kind=kind, provider="esselunga", payload=payload,
                         summary="x", approve=False, account_id="acc-pp", transaction_id="tx1", before=before)
        db.commit()

    page = c.get("/enrich/review").text
    assert page.count('class="card p"') == 1
    assert "Esselunga di Prova · scontrino R1" in page and "Dividi in 2 prodotti" in page and "€3,50 = transazione" in page
    with Session(engine) as db:  # a family whose Sure speaks English gets the UI in English
        db.get(User, uid).locale = "en"
        db.commit()
    page = c.get("/enrich/review").text
    assert "Esselunga di Prova · receipt R1" in page and "Split into 2 products" in page and "€3.50 = transaction" in page and 'lang="en"' in page
    with Session(engine) as db:
        db.get(User, uid).locale = "it"
        db.commit()
    ids = [str(i) for i in range(1, 3)]
    r = c.post("/enrich/ops/bulk", data={"action": "approve", "id": ids}, headers={"origin": "https://sure.example", "referer": "https://sure.example/enrich/review"},
               follow_redirects=False)
    assert r.headers["location"] == "/enrich/review"
    with Session(engine) as db:
        assert {o.status for o in db.scalars(select(SureOp))} == {"approved"}
    assert "Niente da approvare" in c.get("/enrich/review").text
    assert "Rivedi" not in c.get("/enrich/").text


def test_live_sign_in_imports_the_session_and_erases_the_browser_context(engine, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from spe import web
    from spe.livelogin import to_cdp

    class FakeTab:
        opened: list["FakeTab"] = []

        def __init__(self, cdp_url, url):
            self.url, self.context, self.closed, self.signed_in = url, f"ctx{len(self.opened)}", False, False
            self.page_ws = "ws://unused"
            self.opened.append(self)

        def dispose_others(self, keep):
            pass

        def cookies(self, domain):
            return [{"name": "at-acbit", "value": "v", "domain": ".amazon.it", "path": "/"}] if self.signed_in else []

        def close(self):
            self.closed = True

    monkeypatch.setattr(web, "LoginTab", FakeTab)
    fake = FakeSureOAuth({"finn": set()})
    settings = replace(SETTINGS, login_browser_cdp="http://login-browser:9222")
    auth = SureAuth(engine, settings, transport=httpx.MockTransport(fake.handler))
    c = TestClient(create_app(Scheduler(engine, settings, auth, discover()), auth), base_url="https://sure.example")
    sign_in(c, "finn")
    post = {"headers": {"origin": "https://sure.example"}, "follow_redirects": False}
    assert c.post("/enrich/connections", data={"provider": "amazon_it"}, **post).headers["location"] == "/enrich/connections/1/login"

    assert "Riconnetti Amazon" in c.get("/enrich/connections/1/login").text
    [tab] = FakeTab.opened
    assert tab.url.startswith("https://www.amazon.it/")
    with pytest.raises(WebSocketDisconnect) as refused, c.websocket_connect("/enrich/connections/1/login/ws", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_text()
    assert refused.value.code == 4403

    r = c.post("/enrich/connections/1/login/import", **post)  # not signed in yet: back to the browser, nothing stored
    assert r.headers["location"] == "/enrich/connections/1/login?failed=signed-out" and not tab.closed
    tab.signed_in = True
    assert c.post("/enrich/connections/1/login/import", **post).headers["location"] == "/enrich/connections?open=1#c1"
    assert tab.closed
    with Session(engine) as db:
        conn_ = db.get(Connection, 1)
        assert vault.unseal("s", conn_.session)["cookies"][0]["name"] == "at-acbit" and conn_.sync_requested
    assert c.post("/enrich/connections/1/login/cancel", **post).status_code == 404  # nothing left open

    # what the page sends becomes CDP input; anything else is dropped
    assert to_cdp({"t": "key", "down": True, "key": "Enter", "code": "Enter", "keyCode": 13})[0][1]["text"] == "\r"
    assert to_cdp({"t": "text", "text": "hunter2"}) == [("Input.insertText", {"text": "hunter2"})]
    # automatic import only once the tab shows the page signed-out users get sent away from
    from spe.livelogin import landed
    payments = "https://www.amazon.de/cpe/yourpayments/transactions"
    assert landed(payments + "?ref=x", payments) and not landed("https://www.amazon.de/ap/signin?openid.return_to=" + payments, payments)
    assert not landed("https://evil.example/cpe/yourpayments/transactions", payments) and landed("https://beneficiari.edenred.it/x", "https://beneficiari.edenred.it/")
    [(_, select_all)] = to_cdp({"t": "key", "down": True, "key": "a", "keyCode": 65, "ctrl": True, "command": "selectAll"})
    assert (select_all["commands"], select_all["modifiers"]) == (["selectAll"], 0)
    assert "commands" not in to_cdp({"t": "key", "down": True, "key": "a", "keyCode": 65, "command": "Page.navigate"})[0][1]
    assert to_cdp({"t": "mouse", "type": "Runtime.evaluate", "x": 1, "y": 1}) == [] and to_cdp({"t": "eval"}) == []


def test_sign_in_tab_only_shows_the_provider_and_never_the_servers_network(monkeypatch):
    import asyncio

    from spe import livelogin

    dns = {"www.amazon.it": "52.95.120.1", "www.google.com": "142.250.180.4", "127.0.0.1.nip.io": "127.0.0.1", "evil.example": "93.184.215.14"}

    async def resolve(host):
        if host not in dns:
            raise OSError("no such host")
        return [dns[host]]

    monkeypatch.setattr(livelogin, "_resolve", resolve)
    amazon = ("amazon.it", "amazon.com")

    def v(url, main=True):
        return asyncio.run(livelogin.verdict(url, main, amazon))

    assert v("https://www.amazon.it/ap/signin") is None
    assert v("https://www.google.com/recaptcha/api2/anchor", main=False) is None  # a CAPTCHA frame from another public site
    assert v("https://evil.example/") == "evil.example"  # leaving the provider
    assert v("https://www.amazon.it.evil.example/") == "www.amazon.it.evil.example"
    for internal in ("http://web:3000/", "http://login-browser:9222/json", "http://169.254.169.254/latest/meta-data", "http://10.0.0.1/",
                     "http://[::1]:8790/", "https://127.0.0.1.nip.io/", "http://router.lan/", "file:///etc/passwd"):
        assert v(internal, main=False) is not None, internal


def test_in_sospeso_lets_the_owner_pick_among_the_offered_candidates_or_ignore(engine):
    fake = FakeSureOAuth({"gina": set()})
    auth = SureAuth(engine, SETTINGS, transport=httpx.MockTransport(fake.handler))
    c = TestClient(create_app(Scheduler(engine, SETTINGS, auth, discover()), auth), base_url="https://sure.example")
    sign_in(c, "gina")
    post = {"headers": {"origin": "https://sure.example"}, "follow_redirects": False}
    cand = [{"id": f"tx{i}", "date": "2026-11-04", "name": "ESSELUNGA SPA", "amount": "24.0", "account": "Carta"} for i in (1, 2)]
    with Session(engine) as db:
        uid = db.scalar(select(User)).id
        shop = Connection(user_id=uid, provider="esselunga")
        db.add(shop)
        db.commit()
        db.add_all([
            Evidence(user_id=uid, connection_id=shop.id, provider="esselunga", key="R1", kind="purchase",
                     issue="card €24.00: 2 candidates, needs review",
                     payload={"id": "R1", "merchant": "Esselunga", "date": "2026-11-03", "total": "24.00",
                              "payments": [{"method": "card", "amount": "24.00"}], "candidates": {"0": cand}}),
            Evidence(user_id=uid, connection_id=shop.id, provider="esselunga", key="R2", kind="purchase", issue="not linked yet",
                     payload={"id": "R2", "merchant": "Esselunga", "date": "2026-11-05", "total": "5.00", "payments": []}),
        ])
        db.commit()
        r1 = db.scalar(select(Evidence).filter_by(key="R1")).id

    page = c.get("/enrich/held").text
    assert "2 transazioni possibili in Sure" in page and "In attesa del collegamento con Sure" in page
    assert 'class="n w">1<' in page  # only the one waiting on a decision counts
    assert c.post(f"/enrich/held/{r1}/pick", data={"leg": "0", "tx": "tx-from-elsewhere"}, **post).status_code == 422
    assert c.post(f"/enrich/held/{r1}/pick", data={"leg": "0", "tx": "tx2"}, **post).status_code == 303
    assert c.post(f"/enrich/held/{r1}", data={"action": "ignore"}, **post).status_code == 303
    with Session(engine) as db:
        assert db.get(Evidence, r1).payload["picks"] == {"0": "tx2"} and db.get(Evidence, r1).payload["ignored"]
    assert 'class="n w"' not in c.get("/enrich/held").text and "Ignorati (1)" in c.get("/enrich/held").text
