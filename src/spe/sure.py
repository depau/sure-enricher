"""Minimal client for Sure's REST API (/api/v1) and MCP endpoint (/mcp).

Contract checked against we-promise/sure@9d09a48 (app/controllers/api/v1/*,
app/views/api/v1/transactions/_transaction.json.jbuilder). Re-check on upgrade.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class SureError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Sure API {status} {code}: {message}")
        self.status = status
        self.code = code

    @property
    def fatal(self) -> bool:
        """Auth, rate limit and server errors abort a batch; 4xx validation errors don't."""
        return self.status in (401, 403, 429) or self.status >= 500


class _Bearer(httpx.Auth):
    def __init__(self, token: Callable[[], str]):
        self.token = token

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["Authorization"] = f"Bearer {self.token()}"
        yield request


class SureClient:
    """Acts as one Sure user: their OAuth token (scope read_write) works for /api/v1 and for /mcp alike."""

    def __init__(self, base_url: str, token: Callable[[], str], transport: httpx.BaseTransport | None = None):
        self.mcp_url = base_url.rstrip("/") + "/mcp"
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Accept": "application/json"},
            auth=_Bearer(token),
            timeout=30,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> tuple[Any, int]:
        r = self.http.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            raise SureError(r.status_code, str(body.get("error", "http_error")), str(body.get("message", ""))[:200])
        return r.json(), r.status_code

    def _paged(self, path: str, key: str, params: dict[str, Any]) -> Iterator[dict]:
        # ponytail: offset paging over a reverse-chronological list can skip/repeat rows
        # if Sure inserts concurrently; callers only use it for lookups, not full mirrors.
        page = 1
        while True:
            data, _ = self._request("GET", path, params={**params, "page": page, "per_page": 100})
            yield from data[key]
            if page >= data["pagination"]["total_pages"]:
                return
            page += 1

    def accounts(self) -> list[dict]:
        return list(self._paged("/accounts", "accounts", {}))

    def transactions(self, **filters: Any) -> Iterator[dict]:
        return self._paged("/transactions", "transactions", filters)

    def create_transaction(self, tx: dict) -> tuple[dict, bool]:
        """POST /transactions. Returns (transaction, created).

        Sure answers 200 with the existing row when (account, source, external_id)
        already exists, 201 when it created one.
        """
        data, status = self._request("POST", "/transactions", json={"transaction": tx})
        return data, status == 201

    def transaction(self, tx_id: str) -> dict:
        return self._request("GET", f"/transactions/{tx_id}")[0]

    def update_transaction(self, tx_id: str, fields: dict) -> dict:
        return self._request("PATCH", f"/transactions/{tx_id}", json={"transaction": fields})[0]

    def split_transaction(self, tx_id: str, splits: list[dict]) -> dict:
        """POST /transactions/{id}/split. Amounts use the parent's sign convention and must sum to it."""
        return self._request("POST", f"/transactions/{tx_id}/split", json={"split": {"splits": splits}})[0]

    def split(self, tx_id: str) -> dict:
        """GET /transactions/{id}/split: the children, each in the transaction format."""
        return self._request("GET", f"/transactions/{tx_id}/split")[0]

    def resplit_transaction(self, tx_id: str, splits: list[dict]) -> dict:
        """PATCH /transactions/{id}/split: replaces every child (send back what must survive, e.g. categories)."""
        return self._request("PATCH", f"/transactions/{tx_id}/split", json={"split": {"splits": splits}})[0]

    def merchant_id(self, name: str) -> str:
        """The family merchant called `name`. Created when missing through Sure's CSV import endpoint, the only
        merchant-creating API (it skips names that already exist, so a race can't duplicate)."""
        for _ in range(2):
            for m in self._request("GET", "/merchants")[0]:
                if m.get("type") == "FamilyMerchant" and m["name"].casefold() == name.casefold():
                    return m["id"]
            self._request("POST", "/merchants", files={"file": ("merchants.csv", f"name\n{name}\n".encode(), "text/csv")})
        raise SureError(422, "merchant_not_created", f"Sure did not create the merchant {name!r}")

    def sync_family(self) -> None:
        """POST /sync: Sure re-syncs and then runs the family's rules (incl. its category classifier)."""
        self._request("POST", "/sync")

    def mcp(self, tool: str, arguments: dict) -> dict:
        """tools/call on Sure's MCP endpoint, as the same user.

        Returns the tool's JSON result; tool-level failures come back as {"success": false, "error": ...}.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        r = self.http.post(self.mcp_url, json=body)
        if r.status_code >= 400:
            raise SureError(r.status_code, "mcp_http_error", r.text[:200])
        data = r.json()
        if "error" in data:
            raise SureError(422, "mcp_rpc_error", str(data["error"].get("message", ""))[:200])
        result = data["result"]
        if result.get("isError"):
            raise SureError(500, "mcp_tool_failed", result["content"][0]["text"][:200])
        return json.loads(result["content"][0]["text"])
