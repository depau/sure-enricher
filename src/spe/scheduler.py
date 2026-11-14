"""One worker thread. Per active user, as that user: due connections run one at a time (never overlapping),
then their purchases are reconciled and their approved ops applied.

Users Sure no longer accepts (token revoked, user removed) are marked disconnected and skipped; their pending
ops stay queued and everything resumes if they sign in again. After SPE_DISCONNECTED_WIPE_DAYS their provider
sessions (Edenred/Esselunga/Amazon logins) are erased; deleting the rest is an admin decision (`spe users delete`).
"""

import logging
import random
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spe import purchases, vouchers, writer
from spe.auth import SureAuth, UserDisconnected
from spe.db import Connection, User, now
from spe.providers import Blocked, Context, NeedsAuth, Provider
from spe.settings import Settings
from spe.sure import SureClient, SureError

log = logging.getLogger(__name__)
FIRST_RETRY = timedelta(minutes=15)


def _jitter(d: timedelta) -> timedelta:
    return d * random.uniform(0.9, 1.1)


def backoff(failures: int, interval: timedelta) -> timedelta:
    return _jitter(min(interval, FIRST_RETRY * 2 ** (failures - 1)))


def due(c: Connection, at: datetime) -> bool:
    if not c.enabled:
        return False
    if c.sync_requested:
        return True
    if c.status == "needs_auth":  # waits for the owner; retrying cannot help
        return False
    return c.next_run_at is None or c.next_run_at <= at


def _rejected(e: Exception) -> bool:
    return isinstance(e, UserDisconnected) or (isinstance(e, SureError) and e.status == 401)


def run_connection(db: Session, sure: SureClient, settings: Settings, p: Provider, c: Connection) -> None:
    interval = timedelta(hours=c.interval_hours)
    c.status, c.last_attempt_at, c.sync_requested = "syncing", now(), False
    db.commit()
    try:
        msg = p.sync(Context(db, sure, settings, c))
    except NeedsAuth as e:
        db.rollback()
        c.status, c.message, c.next_run_at = "needs_auth", str(e), None
    except Blocked as e:
        db.rollback()
        c.status, c.message, c.next_run_at = "blocked", str(e), now() + _jitter(interval)
    except Exception as e:  # noqa: BLE001 - any provider failure must leave Sure untouched and be visible
        db.rollback()
        if _rejected(e):
            c.status, c.message = "error", "Sure refused this user's token"
            db.commit()
            raise UserDisconnected(str(e)) from e
        c.failures += 1
        c.status, c.message = "error", f"{type(e).__name__}: {e}"[:500]
        c.next_run_at = now() + backoff(c.failures, interval)
        log.warning("connection %s (%s) failed (%d): %s", c.id, p.name, c.failures, c.message)
    else:
        if c.status == "syncing":
            c.status = "connected"
        c.message, c.failures = msg, 0
        c.last_success_at, c.next_run_at = now(), now() + _jitter(interval)
    db.commit()


class Scheduler:
    def __init__(self, engine: Engine, settings: Settings, auth: SureAuth, providers: dict[str, Provider]):
        self.engine, self.settings, self.auth, self.providers = engine, settings, auth, providers
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.heartbeat: datetime | None = None
        self.write_errors: dict[int, str] = {}
        self.checked: dict[int, datetime] = {}  # last time Sure confirmed each user's token
        self.writes_halted: str | None = None  # Sure dropped our idempotency keys: needs a human and a restart

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 60) -> None:
        self.stopping.set()
        self.wake.set()
        self.thread.join(timeout)

    def _loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the daemon alive; state shows the error
                log.exception("scheduler tick failed")
            self.heartbeat = now()
            self.wake.wait(60)
            self.wake.clear()

    def tick(self) -> None:
        with Session(self.engine) as db:
            for user in db.scalars(select(User).where(User.status == "active")).all():
                if self.stopping.is_set():
                    return
                try:
                    self.tick_user(db, user)
                except UserDisconnected as e:
                    db.rollback()
                    SureAuth.disconnect(db, user)
                    log.warning("user %s disconnected: %s", user.id, e)
            self.wipe_disconnected(db)

    def tick_user(self, db: Session, user: User) -> None:
        if self.checked.get(user.id, datetime.min.replace(tzinfo=now().tzinfo)) < now() - timedelta(days=1):
            self.auth.check(user.id)
            self.checked[user.id] = now()
        sure = self.auth.client_for(user.id)
        conns = db.scalars(select(Connection).where(Connection.user_id == user.id).order_by(Connection.id)).all()
        for c in conns:
            p = self.providers.get(c.provider)
            if p is not None and due(c, now()):
                run_connection(db, sure, self.settings, p, c)
        wallets = {c.config["account_id"] for c in conns if c.provider == "edenred_it" and (c.config or {}).get("account_id")}
        try:
            purchases.reconcile(db, sure, wallets, user.id)
            purchases.backfill_split_metadata(db, user.id)
            vouchers.backfill_merchants(db, user.id)
            db.commit()
        except Exception as e:  # noqa: BLE001 - unmatched purchases stay held; retried next tick
            db.rollback()
            if _rejected(e):
                raise UserDisconnected(str(e)) from e
            log.exception("reconciliation failed for user %s", user.id)
        if self.writes_halted:
            return
        try:
            writer.apply(db, sure, user.id)
            self.write_errors.pop(user.id, None)
        except writer.IdempotencyUnsupported as e:
            self.writes_halted = str(e)
            log.error("writes halted: %s", e)
        except Exception as e:  # noqa: BLE001 - surfaced on the dashboard, retried next tick
            db.rollback()
            if _rejected(e):
                raise UserDisconnected(str(e)) from e
            self.write_errors[user.id] = f"{type(e).__name__}: {e}"[:500]
            log.warning("applying Sure ops for user %s failed: %s", user.id, self.write_errors[user.id])

    def wipe_disconnected(self, db: Session) -> None:
        cutoff = now() - timedelta(days=self.settings.disconnected_wipe_days)
        stale = select(Connection).join(User).where(User.status == "disconnected", User.status_since < cutoff, Connection.session.is_not(None))
        for c in db.scalars(stale).all():
            c.session, c.status = None, "needs_auth"
            c.message = f"login erased: Sure has not accepted this user for {self.settings.disconnected_wipe_days} days"
        db.commit()
