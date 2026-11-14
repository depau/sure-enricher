from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, DateTime, ForeignKey, LargeBinary, String, TypeDecorator, UniqueConstraint, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Always aware UTC: SQLite drops the offset, so reattach it on load."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        return value.astimezone(UTC) if value else None

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict: JSON, list: JSON}


class User(Base):
    """A Sure user who signed in through Sure's OAuth. Their token is used for every Sure call made for them.

    status: active | paused (by an admin) | disconnected (Sure refused the token: revoked, or user removed)
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    sure_user_id: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str | None] = mapped_column(String(64))
    family_name: Mapped[str | None] = mapped_column(String(200))
    locale: Mapped[str | None] = mapped_column(String(10))
    token: Mapped[bytes | None] = mapped_column(LargeBinary)  # sealed {access_token, refresh_token, expires_at}
    status: Mapped[str] = mapped_column(String(20), default="active")
    status_since: Mapped[datetime] = mapped_column(default=now)
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=now)


class Connection(Base):
    """One provider account of one user (an Edenred login, an Amazon login, a PayPal account in Sure...).

    config: provider-specific choices, e.g. {"account_id": ...} for Edenred, {"paypal_account": ...,
    "loan_account": ..., "since": ...} for Pay in 3, {"auto_apply": true}.
    status: never_run | connected | syncing | needs_auth | blocked | error | degraded
    """

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    label: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    interval_hours: Mapped[int] = mapped_column(default=12)
    config: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="never_run")
    message: Mapped[str | None] = mapped_column(String(500))
    cursor: Mapped[dict] = mapped_column(default=dict)
    session: Mapped[bytes | None] = mapped_column(LargeBinary)  # sealed provider session (spe.vault)
    failures: Mapped[int] = mapped_column(default=0)
    sync_requested: Mapped[bool] = mapped_column(default=False)
    last_attempt_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    next_run_at: Mapped[datetime | None]


class SureOp(Base):
    """Plan, approval queue, idempotency ledger and audit log for every Sure mutation.

    status: planned -> approved -> sending -> done
            planned -> rejected;  sending -> failed (4xx, needs a human) | approved (retryable)
    """

    __tablename__ = "sure_ops"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(300), unique=True)
    group_key: Mapped[str] = mapped_column(String(300), index=True)  # ops approved together: one purchase, one plan
    kind: Mapped[str] = mapped_column(String(20))  # create | update | split | mcp
    provider: Mapped[str] = mapped_column(String(50))
    account_id: Mapped[str | None] = mapped_column(String(36))
    transaction_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict]
    before: Mapped[dict | None]
    response: Mapped[dict | None]
    status: Mapped[str] = mapped_column(String(20), index=True)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class Evidence(Base):
    """Normalized source records (voucher movements, receipts, orders, plans) of one connection, redacted."""

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("connection_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict]
    issue: Mapped[str | None] = mapped_column(String(300))  # why it is not (yet) reflected in Sure
    first_seen: Mapped[datetime] = mapped_column(default=now)
    last_seen: Mapped[datetime] = mapped_column(default=now)


class Kv(Base):
    """Small server-wide values, e.g. the OAuth client this server registered with Sure."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict]


OWNER_KEYS = ("ignored", "picks")  # decisions taken in In sospeso: a re-sync of the source must not undo them


def record_evidence(db: Session, conn: Connection, key: str, kind: str, payload: dict, issue: str | None) -> None:
    ev = db.scalar(select(Evidence).filter_by(connection_id=conn.id, key=key))
    if ev is None:
        db.add(Evidence(user_id=conn.user_id, connection_id=conn.id, provider=conn.provider, key=key, kind=kind,
                        payload=payload, issue=issue))
    else:
        kept = {k: ev.payload[k] for k in OWNER_KEYS if k in ev.payload}
        ev.payload, ev.issue, ev.last_seen = payload | kept, issue, now()


def migrate(url: str) -> Engine:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _record):  # noqa: ANN001 - SQLAlchemy callback signature
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # cascades when an admin deletes a user

    return engine
