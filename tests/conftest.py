import json
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from spe.db import Connection, User, migrate
from spe.sure import SureClient


class FakeSure:
    """Mirrors Sure's /api/v1 transactions, splits and categories controllers (we-promise/sure@79e8a26)."""

    def __init__(self, honours_idempotency: bool = True):
        self.honours_idempotency = honours_idempotency
        self.rows: list[dict] = []
        self.categories = [{"id": str(uuid.uuid4()), "name": n} for n in ("Groceries", "Household", "Electronics", "Books")]
        self.posts = 0
        self.family_syncs = 0
        self.fail_next: int | None = None
        self.merchants = [{"id": "m-bank", "name": "ESSELUNGA CENTRO", "type": "ProviderMerchant"}]
        self.tags = [{"id": "t-family", "name": "famiglia"}]

    def add(self, account: str, date: str, amount: str, name: str, **extra) -> dict:
        """A transaction as bank sync would have imported it (amount: Sure sign, outflow positive)."""
        row = {"id": str(uuid.uuid4()), "date": date, "amount_decimal": amount, "name": name, "notes": None,
               "category": None, "account": {"id": account}, "source": None, "external_id": None,
               "transfer": None, "split_parent": False, "excluded": False, "merchant": None, "tags": [], **extra}
        self.rows.append(row)
        return row

    def by_id(self, tx_id: str) -> dict:
        return next(r for r in self.rows if r["id"] == tx_id)

    def spending(self, account_ids: set[str]) -> Decimal:
        """What Sure's reports would count: split parents and excluded rows are left out."""
        return sum((Decimal(r["amount_decimal"]) for r in self.rows
                    if r["account"]["id"] in account_ids and not r["excluded"]), Decimal(0))

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/mcp":
            return self.mcp(json.loads(req.content)["params"])
        parts = req.url.path.removeprefix("/api/v1/").split("/")
        if req.method != "GET" and self.fail_next:
            self.posts += req.method == "POST"
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"error": "boom", "message": "x"})
        if parts == ["sync"]:
            self.family_syncs += 1
            return httpx.Response(202, json={})
        if parts == ["merchants"] and req.method == "GET":
            return httpx.Response(200, json=self.merchants)
        if parts == ["merchants"]:  # CSV import: one "name" column, existing names skipped
            names = req.content.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0].decode().splitlines()[1:]
            new = [{"id": f"m-{n.lower()}", "name": n, "type": "FamilyMerchant"} for n in names if n not in {m["name"] for m in self.merchants}]
            self.merchants += new
            return httpx.Response(201, json={"imported": len(new), "merchants": new})
        if parts == ["transactions"] and req.method == "POST":
            self.posts += 1
            tx = json.loads(req.content)["transaction"]
            if self.honours_idempotency:
                for r in self.rows:
                    if (r["account"]["id"], r["source"], r["external_id"]) == (tx["account_id"], tx["source"], tx["external_id"]):
                        return httpx.Response(200, json=r)
            sign = -1 if tx["nature"] == "income" else 1
            keyed = self.honours_idempotency
            row = self.add(tx["account_id"], tx["date"], str(sign * Decimal(tx["amount"])), tx["name"], notes=tx.get("notes"),
                           source=tx["source"] if keyed else None, external_id=tx["external_id"] if keyed else None,
                           merchant=next((m for m in self.merchants if m["id"] == tx.get("merchant_id")), None))
            return httpx.Response(201, json=row)
        if parts == ["transactions"]:
            q = req.url.params
            rows = [r for r in self.rows
                    if ("account_id" not in q or r["account"]["id"] == q["account_id"]) and q["start_date"] <= r["date"] <= q["end_date"]]
            return httpx.Response(200, json={"transactions": rows, "pagination": {"page": 1, "total_pages": 1}})
        row = self.by_id(parts[1])
        if len(parts) == 2 and req.method == "GET":
            return httpx.Response(200, json=row)
        if len(parts) == 2 and req.method == "PATCH":
            body = json.loads(req.content)["transaction"]
            if "notes" in body:
                row["notes"] = body["notes"]
            if "category_id" in body:
                row["category"] = next(c for c in self.categories if c["id"] == body["category_id"])
            if "merchant_id" in body:
                row["merchant"] = next(m for m in self.merchants if m["id"] == body["merchant_id"])
            return httpx.Response(200, json=row)
        if parts[2:] == ["split"] and req.method == "GET":
            return httpx.Response(200, json={"transaction_id": row["id"], "children": [r for r in self.rows if r.get("parent_id") == row["id"]]})
        if parts[2:] == ["split"] and req.method in ("POST", "PATCH"):
            splits = json.loads(req.content)["split"]["splits"]
            if (row["split_parent"] != (req.method == "PATCH")) or sum(Decimal(x["amount"]) for x in splits) != Decimal(row["amount_decimal"]):
                return httpx.Response(422, json={"error": "validation_failed", "message": "splits must sum to parent"})
            self.rows = [r for r in self.rows if r.get("parent_id") != row["id"]]  # PATCH recreates every child
            row["split_parent"] = row["excluded"] = True
            for x in splits:  # Entry#split!: no category_id -> uncategorized; no merchant_id -> the parent's; tags only if given
                cat = next((c for c in self.categories if c["id"] == x.get("category_id")), None)
                merchant = next((m for m in self.merchants if m["id"] == x.get("merchant_id")), None) or row["merchant"]
                tags = [t for t in self.tags if t["id"] in (x.get("tag_ids") or [])]
                self.add(row["account"]["id"], row["date"], x["amount"], x["name"], category=cat, parent_id=row["id"],
                         excluded=bool(x.get("excluded")), merchant=merchant, tags=tags)
            return httpx.Response(201 if req.method == "POST" else 200, json={"transaction_id": row["id"]})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    def mcp(self, params: dict) -> httpx.Response:
        assert params["name"] == "match_transfer", params
        args = params["arguments"]
        tx = self.by_id(args["transaction_id"])
        if tx["transfer"]:
            result = {"success": True, "already_matched": True}
        else:
            other = self.add(args["target_account_id"], tx["date"], str(-Decimal(tx["amount_decimal"])), "Transfer")
            tx["transfer"] = other["transfer"] = {"id": str(uuid.uuid4())}
            result = {"success": True, "created_counterpart": True}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}})

    def client(self) -> SureClient:
        return SureClient("http://sure.test", lambda: "t", transport=httpx.MockTransport(self.handler))


def conn(db: Session, provider: str, **config) -> Connection:
    """A connection of user 1 (created on first use)."""
    if db.get(User, 1) is None:
        db.add(User(id=1, sure_user_id="u1"))
        db.flush()
    c = Connection(user_id=1, provider=provider, config=config)
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def engine(tmp_path):
    return migrate(f"sqlite:///{tmp_path}/spe.db")


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s
