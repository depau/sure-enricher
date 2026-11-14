"""Sign-ins happen in the login browser (docker/login-browser), driven over CDP and shown to the user in /enrich.

Each sign-in gets its own browser context: separate cookies and storage, so users never see each other's
sessions, and disposing the context afterwards erases everything the sign-in left behind.
"""

import json
import re
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect

WINDOW = {"width": 1280, "height": 900}


def _call(ws_url: str, method: str, params: dict) -> Any:
    with connect(ws_url, max_size=2**24) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while (msg := json.loads(ws.recv())).get("id") != 1:
            pass
    if "error" in msg:
        raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
    return msg["result"]


def ws_base(cdp_url: str) -> str:
    return cdp_url.replace("http", "ws", 1).rstrip("/")


def by_ip(cdp_url: str) -> str:
    """Chromium serves CDP (HTTP and websocket) only when Host is an IP or localhost, its DNS-rebinding guard; a
    compose service name like login-browser is neither, so connect by address. ponytail: IPv4 names only."""
    u = urlsplit(cdp_url)
    return u._replace(netloc=socket.gethostbyname(u.hostname or "") + (f":{u.port}" if u.port else "")).geturl()


class LoginTab:
    """One isolated sign-in: a fresh browser context with one tab. close() disposes all of it."""

    def __init__(self, cdp_url: str, url: str):
        self.cdp_url = by_ip(cdp_url)  # resolved per sign-in: the container's address changes when it restarts
        version = httpx.get(f"{self.cdp_url}/json/version", timeout=10)
        browser = version.raise_for_status().json()["webSocketDebuggerUrl"]
        self.browser_ws = re.sub(r"^ws://[^/]+", ws_base(self.cdp_url), browser)  # CDP reports its in-container address
        self.context = _call(self.browser_ws, "Target.createBrowserContext", {})["browserContextId"]
        self.target = _call(self.browser_ws, "Target.createTarget", {"url": url, "browserContextId": self.context, "newWindow": True})["targetId"]
        window = _call(self.browser_ws, "Browser.getWindowForTarget", {"targetId": self.target})["windowId"]
        _call(self.browser_ws, "Browser.setWindowBounds", {"windowId": window, "bounds": {"left": 0, "top": 0, **WINDOW}})

    @property
    def page_ws(self) -> str:
        return f"{ws_base(self.cdp_url)}/devtools/page/{self.target}"

    def cookies(self, domain_suffix: str) -> list[dict]:
        """This sign-in's cookies (httpOnly included) whose domain ends with domain_suffix: name, value, domain, path."""
        found = _call(self.browser_ws, "Storage.getCookies", {"browserContextId": self.context})["cookies"]
        return [{k: c[k] for k in ("name", "value", "domain", "path")} for c in found if c["domain"].lstrip(".").endswith(domain_suffix)]

    def evaluate(self, expression: str) -> Any:
        """Runs in the tab through a session on the browser connection: the page's own websocket endpoint refuses a
        second client while the live view holds it (HTTP 500)."""
        with connect(self.browser_ws, max_size=2**24) as ws:
            def call(i: int, method: str, params: dict, session: str | None = None) -> dict:
                ws.send(json.dumps({"id": i, "method": method, "params": params} | ({"sessionId": session} if session else {})))
                while (msg := json.loads(ws.recv())).get("id") != i:
                    pass
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
                return msg["result"]

            session = call(1, "Target.attachToTarget", {"targetId": self.target, "flatten": True})["sessionId"]
            try:
                return call(2, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)["result"].get("value")
            finally:
                call(3, "Target.detachFromTarget", {"sessionId": session})

    def close(self) -> None:
        _call(self.browser_ws, "Target.disposeBrowserContext", {"browserContextId": self.context})

    def dispose_others(self, keep: set[str]) -> None:
        """Erase sign-ins nobody tracks any more (left behind by a daemon restart)."""
        for ctx in _call(self.browser_ws, "Target.getBrowserContexts", {})["browserContextIds"]:
            if ctx != self.context and ctx not in keep:
                _call(self.browser_ws, "Target.disposeBrowserContext", {"browserContextId": ctx})
