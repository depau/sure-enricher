"""The only code that mutates Sure. Ops are staged (planned/approved) and applied from the queue.

Duplicate protection for creates:
- the queue: a `done` op is never sent again, and its key is unique;
- Sure's own (account, source, external_id) dedupe on POST;
- an op that may have landed (crash mid-POST, 5xx, timeout) is first looked up in Sure by
  source + external_id, so recovery is safe even on a Sure build that ignores those fields.
"""

from collections import Counter
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from spe.db import SureOp
from spe.sure import SureClient, SureError


class IdempotencyUnsupported(RuntimeError):
    """Sure dropped source/external_id: replays could duplicate, so writing stops."""


def create_key(payload: dict) -> str:
    return f"create:{payload['account_id']}:{payload['source']}:{payload['external_id']}"


def stage(
    db: Session,
    *,
    key: str,
    kind: str,
    provider: str,
    payload: dict,
    summary: str,
    approve: bool,
    user_id: int | None,
    group: str | None = None,
    account_id: str | None = None,
    transaction_id: str | None = None,
    before: dict | None = None,
) -> SureOp:
    """Insert a planned op, or refresh one that is still only planned. Caller commits.

    `before` is the Sure transaction as seen when planning; applying refuses if it changed since.
    """
    op = db.scalar(select(SureOp).filter_by(key=key))
    if op is None:
        op = SureOp(
            user_id=user_id,
            key=key,
            group_key=group or key,
            kind=kind,
            provider=provider,
            payload=payload,
            summary=summary,
            account_id=account_id,
            transaction_id=transaction_id,
            before=before,
            status="approved" if approve else "planned",
        )
        db.add(op)
    elif op.status == "planned" and (op.payload, op.before) != (payload, before):
        op.payload, op.summary, op.before = payload, summary, before
    return op


def stage_create(db: Session, provider: str, payload: dict, summary: str, approve: bool, user_id: int | None) -> SureOp:
    return stage(
        db,
        user_id=user_id,
        key=create_key(payload),
        kind="create",
        provider=provider,
        payload=payload,
        summary=summary,
        approve=approve,
        account_id=payload["account_id"],
    )


def _find_in_sure(sure: SureClient, p: dict) -> dict | None:
    for tx in sure.transactions(account_id=p["account_id"], start_date=p["date"], end_date=p["date"]):
        if tx.get("source") == p["source"] and tx.get("external_id") == p["external_id"]:
            return tx
    return None


def _done(db: Session, op: SureOp, tx: dict, error: str | None = None) -> None:
    op.status, op.transaction_id, op.response, op.error = "done", tx["id"], tx, error
    db.commit()


def _create(db: Session, sure: SureClient, op: SureOp) -> str:
    p = op.payload
    if (op.status == "sending" or op.error) and (tx := _find_in_sure(sure, p)):
        _done(db, op, tx)
        return "recovered"

    body = {k: v for k, v in p.items() if k != "merchant"}
    if p.get("merchant"):
        body["merchant_id"] = sure.merchant_id(p["merchant"])  # a family merchant, created once
    op.status = "sending"
    db.commit()
    try:
        tx, created = sure.create_transaction(body)
    except SureError as e:
        op.status, op.error = ("approved" if e.fatal else "failed"), str(e)
        db.commit()
        if e.fatal:
            raise
        return "failed"

    if tx.get("source") != p["source"] or tx.get("external_id") != p["external_id"]:
        # It exists in Sure now, just unkeyed: mark done so it is never re-sent.
        _done(db, op, tx, "Sure ignored source/external_id")
        raise IdempotencyUnsupported(f"Sure transaction {tx.get('id')} lacks our idempotency key; stopping")
    _done(db, op, tx)
    return "created" if created else "deduped_by_sure"


def _fail(db: Session, op: SureOp, error: str) -> str:
    op.status, op.error = "failed", error
    db.commit()
    return "failed"


def _send(db: Session, op: SureOp, call: Callable[[], dict]) -> str | None:
    """Run a mutating call; on error record it and return the stat, else mark done and return None."""
    op.status = "sending"
    db.commit()
    try:
        tx = call()
    except SureError as e:
        op.status, op.error = ("approved" if e.fatal else "failed"), str(e)
        db.commit()
        if e.fatal:
            raise
        return "failed"
    op.status, op.response, op.error = "done", tx, None
    db.commit()
    return None


def _cat(tx: dict | None) -> str | None:
    return ((tx or {}).get("category") or {}).get("id")


def _changed(op: SureOp, tx: dict, fields: tuple[str, ...]) -> list[str]:
    before = op.before or {}
    return [f for f in fields if (_cat(tx) != _cat(before) if f == "category" else tx.get(f) != before.get(f))]


def _update(db: Session, sure: SureClient, op: SureOp) -> str:
    tx = sure.transaction(op.transaction_id)
    want = op.payload
    if "merchant" in want:  # set a merchant where Sure has none; one chosen in Sure is never replaced
        merchant_id = sure.merchant_id(want["merchant"])
        current = (tx.get("merchant") or {}).get("id")
        if current == merchant_id:
            op.status, op.response, op.error = "done", tx, None
            db.commit()
            return "recovered"
        if current:
            return _fail(db, op, f"merchant already set in Sure ({tx['merchant'].get('name')}): left as is")
        return _send(db, op, lambda: sure.update_transaction(op.transaction_id, {"merchant_id": merchant_id})) or "updated"
    if tx.get("notes") == want.get("notes") and ("category_id" not in want or _cat(tx) == want["category_id"]):
        op.status, op.response, op.error = "done", tx, None  # landed before a crash, or already true
        db.commit()
        return "recovered"
    if changed := _changed(op, tx, ("notes", "category", "amount_decimal", "date")):
        return _fail(db, op, f"changed in Sure since planned: {', '.join(changed)}; reject and let it re-plan")
    return _send(db, op, lambda: sure.update_transaction(op.transaction_id, want)) or "updated"


def _split(db: Session, sure: SureClient, op: SureOp) -> str:
    tx = sure.transaction(op.transaction_id)
    if tx.get("split_parent"):
        if op.status == "sending":
            op.status, op.error = "done", None  # our POST landed before a crash
            db.commit()
            return "recovered"
        return _fail(db, op, "already split in Sure: left as is")
    if changed := _changed(op, tx, ("notes", "category", "amount_decimal", "date", "split_parent")):
        return _fail(db, op, f"changed in Sure since planned: {', '.join(changed)}")
    children = _with_metadata(sure, tx, op.payload["splits"], op.payload.get("merchant"))
    return _send(db, op, lambda: sure.split_transaction(op.transaction_id, children)) or "split"


def _with_metadata(sure: SureClient, parent: dict, splits: list[dict], merchant: str | None) -> list[dict]:
    """Children carry the purchase's merchant (for merchant rules) and the parent's tags; Sure gives them neither
    by default when the bank row has no merchant, and never the tags."""
    merchant_id = sure.merchant_id(merchant) if merchant else None
    tags = [t["id"] for t in parent.get("tags") or []]
    out = []
    for s in splits:
        child = dict(s)
        if merchant_id and not s.get("merchant_id"):
            child["merchant_id"] = merchant_id
        if tags or s.get("tag_ids"):
            child["tag_ids"] = sorted(set(tags) | set(s.get("tag_ids") or []))
        out.append(child)
    return out


def _resplit(db: Session, sure: SureClient, op: SureOp) -> str:
    """Re-send a split we made earlier with the metadata it lacked, keeping what Sure added to the children since
    (categories from the classifier, exclusions, tags). Refuses if the products are no longer the ones we split."""
    tx = sure.transaction(op.transaction_id)
    if not tx.get("split_parent"):
        return _fail(db, op, "no longer split in Sure: left as is")
    current = sure.split(op.transaction_id)["children"]
    ours = sorted((s["name"], Decimal(s["amount"])) for s in op.payload["expect"])
    if sorted((c["name"], Decimal(c["amount_decimal"])) for c in current) != ours:
        return _fail(db, op, "the split was changed in Sure since we made it: left as is")
    inherited = (tx.get("merchant") or {}).get("id")  # what Sure gave children without a merchant_id; anything else was chosen by hand

    def own_merchant(c: dict) -> str | None:
        m = (c.get("merchant") or {}).get("id")
        return m if m and m != inherited else None

    kept = [{"name": c["name"], "amount": c["amount_decimal"], "category_id": (c.get("category") or {}).get("id"),
             "merchant_id": own_merchant(c), "excluded": bool(c.get("excluded")), "tag_ids": [t["id"] for t in c.get("tags") or []]}
            for c in current]
    children = _with_metadata(sure, tx, kept, op.payload["merchant"])
    if all((c.get("merchant") or {}).get("id") == want["merchant_id"] and sorted(t["id"] for t in c.get("tags") or []) == want.get("tag_ids", [])
           for c, want in zip(current, children, strict=True)):
        op.status, op.response, op.error = "done", {"children": current}, None  # landed before a crash, or already true
        db.commit()
        return "recovered"
    return _send(db, op, lambda: sure.resplit_transaction(op.transaction_id, children)) or "resplit"


def _mcp(db: Session, sure: SureClient, op: SureOp) -> str:
    """MCP tool call (match_transfer is idempotent: a repeat answers already_matched)."""
    op.status = "sending"
    db.commit()
    try:
        result = sure.mcp(op.payload["tool"], op.payload["arguments"])
    except SureError as e:
        op.status, op.error = ("approved" if e.fatal else "failed"), str(e)
        db.commit()
        if e.fatal:
            raise
        return "failed"
    if not result.get("success"):
        return _fail(db, op, f"{result.get('error')}: {result.get('message')}"[:500])
    op.status, op.response, op.error = "done", result, None
    db.commit()
    return op.payload["tool"]


APPLY = {"create": _create, "update": _update, "split": _split, "resplit": _resplit, "mcp": _mcp}


def apply(db: Session, sure: SureClient, user_id: int | None) -> Counter[str]:
    """Send the user's approved (or interrupted) ops, oldest first, as that user. Fatal Sure errors propagate."""
    stats: Counter[str] = Counter()
    ops = db.scalars(select(SureOp).where(SureOp.user_id == user_id, SureOp.status.in_(("approved", "sending"))).order_by(SureOp.id)).all()
    for op in ops:
        stats[APPLY[op.kind](db, sure, op)] += 1
    if stats["split"] or stats["resplit"]:
        sure.sync_family()  # new split children: let Sure's rules (classifier, merchant rules) run on them
    return stats
