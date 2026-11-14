"""Sign-in through Sure's OAuth (verified against we-promise/sure@79e8a26), one token per user.

  POST {SURE_URL}/register              dynamic client registration (once per callback URL), public client, PKCE
  GET  {SURE_PUBLIC_URL}/oauth/authorize the browser signs in to Sure and approves scope read_write
  POST {SURE_URL}/oauth/token           code + verifier -> access (1 year) + refresh token
  GET  {SURE_URL}/oauth/token/info      -> resource_owner_id: who this is
  GET  {SURE_URL}/api/v1/family_settings -> family name and locale
The same token authorizes /api/v1 and /mcp. The connection shows in Sure under Settings -> MCP.
"""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spe import vault
from spe.db import Kv, User, now
from spe.settings import Settings
from spe.sure import SureClient

CLIENT_NAME = "Enrich"  # how Sure lists this app; Sure refuses client names containing the word "sure"
REFRESH_BEFORE = timedelta(days=7)
LOGIN_TTL = timedelta(minutes=15)


class UserDisconnected(Exception):
    """Sure no longer accepts this user's token (revoked in Sure, or the user was removed)."""


class SureAuth:
    def __init__(self, engine: Engine, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.engine, self.s = engine, settings
        self.transport = transport
        self.http = httpx.Client(timeout=30, transport=transport)

    @property
    def redirect_uri(self) -> str:
        return f"{self.s.public_url}/auth/callback"

    # --- sign-in ------------------------------------------------------------------------------------------

    def _client_id(self, db: Session) -> str:
        kv = db.get(Kv, "oauth_client")
        if kv is None or kv.value.get("redirect_uri") != self.redirect_uri:
            r = self.http.post(f"{self.s.sure_url}/register", json={"client_name": CLIENT_NAME, "redirect_uris": [self.redirect_uri]})
            r.raise_for_status()
            value = {"client_id": r.json()["client_id"], "redirect_uri": self.redirect_uri}
            if kv is None:
                db.add(Kv(key="oauth_client", value=value))
            else:
                kv.value = value
            db.commit()
            return value["client_id"]
        return kv.value["client_id"]

    def start(self, next_url: str) -> tuple[str, bytes]:
        """Sure URL to send the browser to, and the sealed login state to keep in a short-lived cookie."""
        with Session(self.engine) as db:
            client_id = self._client_id(db)
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = f"{self.s.sure_public_url}/oauth/authorize?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": self.redirect_uri, "scope": "read_write",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        pending = {"state": state, "verifier": verifier, "next": next_url, "until": (datetime.now(UTC) + LOGIN_TTL).isoformat()}
        return url, vault.seal(self.s.secret_key, pending)

    def finish(self, code: str, state: str, pending_blob: bytes | None) -> tuple[User, str]:
        pending = vault.unseal(self.s.secret_key, pending_blob)
        if not pending or not secrets.compare_digest(pending["state"], state) or datetime.fromisoformat(pending["until"]) < datetime.now(UTC):
            raise PermissionError("sign-in expired or was started elsewhere: open the page again")
        with Session(self.engine) as db:
            token = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                                 "client_id": self._client_id(db), "code_verifier": pending["verifier"]})
            bearer = {"Authorization": f"Bearer {token['access_token']}"}
            who = self.http.get(f"{self.s.sure_url}/oauth/token/info", headers=bearer)
            who.raise_for_status()
            family = self.http.get(f"{self.s.sure_url}/api/v1/family_settings", headers=bearer)
            family.raise_for_status()
            f = family.json()
            sure_user_id = str(who.json()["resource_owner_id"])
            user = db.scalar(select(User).filter_by(sure_user_id=sure_user_id))
            if user is None:
                user = User(sure_user_id=sure_user_id, status="active", status_since=now())
                db.add(user)
            elif user.status == "disconnected":
                user.status, user.status_since = "active", now()  # signed in again: their connections resume
            user.family_id, user.family_name, user.locale = str(f.get("id")), f.get("name"), f.get("locale")
            user.token, user.last_login_at = vault.seal(self.s.secret_key, token), now()
            db.commit()
            db.refresh(user)
            db.expunge(user)
        return user, pending["next"]

    # --- tokens -------------------------------------------------------------------------------------------

    def token(self, user_id: int) -> str:
        """A valid access token for the user, refreshed a week before expiry. UserDisconnected when Sure refuses."""
        with Session(self.engine) as db:
            user = db.get(User, user_id)
            data = vault.unseal(self.s.secret_key, user.token) if user else None
            if user is None or user.status == "disconnected" or not data:
                raise UserDisconnected(f"user {user_id} has no working Sure token")
            if datetime.fromisoformat(data["expires_at"]) - datetime.now(UTC) < REFRESH_BEFORE:
                try:
                    data = self._token({"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                        "client_id": self._client_id(db)})
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (400, 401):
                        self.disconnect(db, user)
                        raise UserDisconnected(f"Sure refused to renew user {user_id}'s token") from e
                    raise
                user.token = vault.seal(self.s.secret_key, data)
                db.commit()
            return data["access_token"]

    def check(self, user_id: int) -> None:
        """Ask Sure whether it still accepts the user's token, so removed users are noticed even when idle."""
        r = self.http.get(f"{self.s.sure_url}/oauth/token/info", headers={"Authorization": f"Bearer {self.token(user_id)}"})
        if r.status_code == 401:
            with Session(self.engine) as db:
                self.disconnect(db, db.get(User, user_id))
            raise UserDisconnected(f"Sure no longer accepts user {user_id}'s token")
        r.raise_for_status()

    def client_for(self, user_id: int) -> SureClient:
        return SureClient(self.s.sure_url, lambda: self.token(user_id), transport=self.transport)

    @staticmethod
    def disconnect(db: Session, user: User) -> None:
        if user.status != "disconnected":
            user.status, user.status_since = "disconnected", now()
            db.commit()

    def _token(self, form: dict) -> dict:
        r = self.http.post(f"{self.s.sure_url}/oauth/token", data=form)
        r.raise_for_status()
        t = r.json()
        expires = datetime.now(UTC) + timedelta(seconds=int(t.get("expires_in") or 31_536_000))
        return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token"), "expires_at": expires.isoformat()}
