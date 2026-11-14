"""spe serve                          run the daemon: scheduler + web UI on $SPE_LISTEN (default 127.0.0.1:8790)
spe users list                     who signed in, their status and what they have queued
spe users pause|resume ID          stop / restart syncing and writing for a user (resume works on paused users only;
                                   disconnected users resume by signing in again)
spe users delete ID [--yes]        forget a user: their token, connections, provider logins, queue and history.
                                   Nothing in Sure is touched.
spe connections list [--user ID]
spe connections delete ID [--yes]  forget one connection, its provider login and its evidence

Configuration is read from the environment, see .env.example. Admin commands only need DATABASE_URL.
"""

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="spe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    for name in ("users", "connections"):
        sp = sub.add_parser(name).add_subparsers(dest="action", required=True)
        ls = sp.add_parser("list")
        if name == "connections":
            ls.add_argument("--user", type=int)
        actions = ("pause", "resume", "delete") if name == "users" else ("delete",)
        for action in actions:
            a = sp.add_parser(action)
            a.add_argument("id", type=int)
            if action == "delete":
                a.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        return serve()
    from spe.db import migrate

    with Session(migrate(os.environ.get("DATABASE_URL", "sqlite:///spe.db"))) as db:
        return admin(db, args)


def admin(db: Session, args: argparse.Namespace) -> int:
    from spe.db import Connection, Evidence, SureOp, User

    def confirm(what: str) -> bool:
        return args.yes or input(f"Delete {what}? Type 'yes': ").strip() == "yes"

    if args.cmd == "users":
        if args.action == "list":
            conns = dict(db.execute(select(Connection.user_id, func.count()).group_by(Connection.user_id)).all())
            queued = dict(db.execute(select(SureOp.user_id, func.count()).where(SureOp.status == "planned").group_by(SureOp.user_id)).all())
            print(f"{'id':>4}  {'status':<13} {'since':<17} {'last sign-in':<17} {'conns':>5} {'to approve':>10}  family / Sure user")
            for u in db.scalars(select(User).order_by(User.id)):
                print(f"{u.id:>4}  {u.status:<13} {_t(u.status_since):<17} {_t(u.last_login_at):<17} {conns.get(u.id, 0):>5}"
                      f" {queued.get(u.id, 0):>10}  {u.family_name or '?'} / {u.sure_user_id}")
            return 0
        user = db.get(User, args.id)
        if user is None:
            print(f"no user {args.id}", file=sys.stderr)
            return 1
        if args.action == "delete":
            if not confirm(f"user {user.id} ({user.family_name or user.sure_user_id}) and all their data in this server"):
                return 1
            db.delete(user)  # connections, evidence and ops cascade
        elif args.action == "pause":
            user.status = "paused"
        elif user.status != "paused":
            print(f"user {user.id} is {user.status}; only paused users can be resumed here", file=sys.stderr)
            return 1
        else:
            user.status = "active"
        db.commit()
        print(f"user {args.id}: {'deleted' if args.action == 'delete' else user.status}")
        return 0

    if args.action == "list":
        q = select(Connection).order_by(Connection.id)
        if args.user is not None:
            q = q.where(Connection.user_id == args.user)
        held = dict(db.execute(select(Evidence.connection_id, func.count()).where(Evidence.issue.is_not(None)).group_by(Evidence.connection_id)).all())
        print(f"{'id':>4} {'user':>4}  {'provider':<14} {'status':<11} {'last success':<17} {'held':>5}  label / message")
        for c in db.scalars(q):
            print(f"{c.id:>4} {c.user_id or '-':>4}  {c.provider:<14} {c.status:<11} {_t(c.last_success_at):<17} {held.get(c.id, 0):>5}"
                  f"  {c.label or ''}{' / ' if c.label else ''}{(c.message or '')[:60]}{'' if c.enabled else ' [disabled]'}")
        return 0
    c = db.get(Connection, args.id)
    if c is None:
        print(f"no connection {args.id}", file=sys.stderr)
        return 1
    if not confirm(f"connection {c.id} ({c.provider} of user {c.user_id}) and its evidence"):
        return 1
    db.delete(c)
    db.commit()
    print(f"connection {args.id}: deleted")
    return 0


def _t(d: object) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def serve() -> int:
    import uvicorn

    from spe.auth import SureAuth
    from spe.db import migrate
    from spe.providers import discover
    from spe.scheduler import Scheduler
    from spe.settings import Settings
    from spe.web import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry tokens (Edenred SSO handoff, OAuth codes)
    settings = Settings.from_env()
    engine = migrate(settings.database_url)
    auth = SureAuth(engine, settings)
    sched = Scheduler(engine, settings, auth, discover())
    app = create_app(sched, auth)

    @asynccontextmanager
    async def lifespan(_: object) -> AsyncIterator[None]:
        sched.start()
        yield
        sched.stop()  # finishes the provider call in flight (HTTP timeouts bound it)

    app.router.lifespan_context = lifespan
    host, _, port = os.environ.get("SPE_LISTEN", "127.0.0.1:8790").rpartition(":")
    # no access log: OAuth callbacks carry the authorization code in the query string
    uvicorn.run(app, host=host, port=int(port), proxy_headers=True, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
