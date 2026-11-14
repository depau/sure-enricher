"""Live view of a sign-in tab: CDP screencast frames out to the user's page, their mouse, keys and pastes in.

Nothing typed or pasted is logged or stored: it only goes from the page's websocket to Input.* on the tab.
"""

import asyncio
import ipaddress
import logging
import itertools
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect

# The focused field's label (for "Incollato nel campo …") and the selected text, so Ctrl+C / ⌘C in the page copies
# it to the user's own clipboard. Password fields never give their text out.
FOCUS_JS = """(() => { const e = document.activeElement;
  const field = e && e !== document.body && 'value' in e;
  const l = field && ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || e.name || e.type);
  let selection = '';
  if (field && typeof e.selectionStart === 'number') selection = e.type === 'password' ? '' : e.value.slice(e.selectionStart, e.selectionEnd);
  else selection = String(window.getSelection() || '');
  return { label: (l || '').trim().replace(/\\s+/g, ' ').slice(0, 40) || null, selection: selection.slice(0, 10000),
           secret: !!field && e.type === 'password' }; })()"""
COMMANDS = {"selectAll", "undo", "redo", "cut"}  # editing shortcuts, sent as commands so they work whatever the remote OS
log = logging.getLogger(__name__)
# CDP has no cursor events: ask the page what is under the pointer. "auto" resolved the way browsers do for the
# common cases (editable text -> I-beam, everything else -> arrow); links get "pointer" from the UA stylesheet.
CURSOR_JS = """((x, y) => { const e = document.elementFromPoint(x, y);
  if (!e) return 'default';
  const c = getComputedStyle(e).cursor.split(',').pop().trim();
  if (c !== 'auto') return c;
  const typed = e.tagName === 'INPUT' && !['button', 'submit', 'reset', 'checkbox', 'radio', 'image', 'file', 'range', 'color'].includes(e.type);
  return e.isContentEditable || e.tagName === 'TEXTAREA' || typed ? 'text' : 'default'; })(%f, %f)"""
# Browser-drawn UI is not part of the screencast, so anything that opens it would freeze the sign-in where the user
# can't see it. Passkeys are the one sites trigger by themselves (amazon.fr opens "No passkeys available" right after
# the email): make the tab look like a browser without WebAuthn, so sites go straight to the password.
NO_PASSKEYS_JS = """(() => {
  try { Object.defineProperty(window, 'PublicKeyCredential', { value: undefined, configurable: true }); } catch (e) {}
  const no = () => Promise.reject(new DOMException('Passkeys are not available here', 'NotAllowedError'));
  try { if (navigator.credentials) { navigator.credentials.get = no; navigator.credentials.create = no; } } catch (e) {}
})()"""
CURSOR_EVERY = 0.08
BUSY_TYPES = {"Document", "XHR", "Fetch"}  # what makes the page "working" for the user; beacons and media don't  # seconds between cursor lookups while the pointer moves
MODIFIERS = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}
MOUSE = {"mousePressed", "mouseReleased", "mouseMoved"}
BUTTONS = {"none", "left", "middle", "right"}


def to_cdp(msg: dict[str, Any]) -> list[tuple[str, dict]]:
    """Translate one message from the page into CDP commands. Unknown or malformed messages are dropped."""
    mods = sum(v for k, v in MODIFIERS.items() if msg.get(k))
    t = msg.get("t")
    if t == "mouse" and msg.get("type") in MOUSE:
        return [("Input.dispatchMouseEvent", {
            "type": msg["type"], "x": float(msg["x"]), "y": float(msg["y"]), "modifiers": mods,
            "button": msg.get("button") if msg.get("button") in BUTTONS else "none", "buttons": int(msg.get("buttons", 0)),
            "clickCount": int(msg.get("clicks", 0))})]
    if t == "wheel":
        return [("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": float(msg["x"]), "y": float(msg["y"]),
                                              "deltaX": float(msg.get("dx", 0)), "deltaY": float(msg.get("dy", 0)), "modifiers": mods})]
    if t == "key" and isinstance(msg.get("key"), str) and len(msg["key"]) <= 20:
        code = int(msg.get("keyCode", 0))
        params = {"key": msg["key"], "code": str(msg.get("code", ""))[:30], "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "modifiers": mods}
        if not msg.get("down"):
            return [("Input.dispatchKeyEvent", {"type": "keyUp", **params})]
        if msg.get("command") in COMMANDS:
            # without modifiers: the command does the work, so a Linux Chromium doesn't also run its own Ctrl+Z
            return [("Input.dispatchKeyEvent", {"type": "rawKeyDown", **params, "modifiers": 0, "commands": [msg["command"]]})]
        if msg["key"] == "Enter":
            return [("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", "unmodifiedText": "\r", **params})]  # submits forms
        return [("Input.dispatchKeyEvent", {"type": "rawKeyDown", **params})]
    if t == "text" and isinstance(msg.get("text"), str):
        return [("Input.insertText", {"text": msg["text"][:10_000]})]
    if t == "back":
        return [("Runtime.evaluate", {"expression": "history.back()"})]
    if t == "reload":
        return [("Page.reload", {})]
    return []


INTERNAL_SUFFIXES = (".local", ".internal", ".lan", ".home", ".arpa", ".localhost", ".localdomain")


async def _public(host: str) -> bool:
    """A host the internet can see: not an internal name, and every address it resolves to is global."""
    if not host or "." not in host or host.endswith(INTERNAL_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        pass
    try:
        addresses = await _resolve(host)
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


async def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in await asyncio.get_running_loop().getaddrinfo(host, None)]


async def verdict(url: str, main_frame: bool, domains: tuple[str, ...]) -> str | None:
    """None when the document may load; else the host to report as blocked.

    The tab may only show the provider's own sites (so the user can't steer it to another site), and no frame may load
    from the server's network (basic SSRF: Sure's internal URL, the CDP port, cloud metadata, the LAN). Frames from
    other public sites (CAPTCHAs) are fine. ponytail: DNS can change between our check and the browser's (rebinding);
    a network policy for the login-browser container is the full fix.
    """
    u = urlsplit(url)
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme not in ("http", "https") or not await _public(host):
        return host or u.scheme
    if main_frame and not any(host == d or host.endswith("." + d) for d in domains):
        return host
    return None


LANDED_TRIES, LANDED_EVERY = 30, 2.0  # a single-page app may store its token a little after the page loaded


def landed(url: str, landing: str) -> bool:
    """On the page the provider shows only to signed-in users (its login_url: signed out, the site sends you away)."""
    u, want = urlsplit(url), urlsplit(landing)
    return u.scheme == "https" and u.hostname == want.hostname and u.path.startswith(want.path.rstrip("/") or "/")


async def bridge(websocket: WebSocket, page_ws: str, domains: tuple[str, ...], landing: str,
                 try_import: Callable[[], bool]) -> bool:
    """Runs until either side closes or the session is imported (returns True then).

    While it runs, every document the tab loads goes through verdict(). Once the tab lands on `landing`, try_import()
    (the same check and import as the button, in a thread) is retried for a while: no button press needed.
    """
    async with connect(page_ws, max_size=2**25) as cdp:
        ids = itertools.count(1)
        replies: dict[int, str] = {}  # CDP request id -> what to tell the page about its result

        async def send(method: str, params: dict, reply_as: str | None = None) -> None:
            i = next(ids)
            if reply_as:
                replies[i] = reply_as
            await cdp.send(json.dumps({"id": i, "method": method, "params": params}))

        main: dict = {"frame": None, "url": "", "importing": False, "imported": False}

        async def import_when_signed_in() -> None:
            try:
                for _ in range(LANDED_TRIES):
                    if not landed(main["url"], landing):
                        return
                    try:
                        ok = await run_in_threadpool(try_import)
                    except Exception as e:  # noqa: BLE001 - e.g. the tab was cancelled meanwhile; the button is still there
                        log.info("automatic session import stopped: %s", e)
                        return
                    if ok:
                        main["imported"] = True
                        await websocket.send_text(json.dumps({"t": "imported"}))
                        await cdp.close()  # ends the bridge: the caller disposes the tab
                        return
                    await asyncio.sleep(LANDED_EVERY)
            finally:
                main["importing"] = False
        busy: dict = {"page": False, "requests": set(), "shown": False}

        async def report_busy() -> None:
            now = bool(busy["page"] or busy["requests"])
            if now != busy["shown"]:
                busy["shown"] = now
                await websocket.send_text(json.dumps({"t": "loading", "value": now}))

        async def guard(p: dict) -> None:
            # A paused request waits until it is answered: never leave one hanging (the page would just stop).
            try:
                blocked = await verdict(p["request"]["url"], p.get("frameId") == main["frame"], domains)
            except Exception:  # noqa: BLE001 - unknown means no
                log.exception("could not check a sign-in navigation: blocking it")
                blocked = urlsplit(p["request"]["url"]).hostname or "?"
            if blocked is None:
                await send("Fetch.continueRequest", {"requestId": p["requestId"]})
            else:
                log.info("sign-in tab: blocked a document from %s", blocked)
                await send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"})
                await websocket.send_text(json.dumps({"t": "blocked", "value": blocked}))

        await send("Page.enable", {})
        await send("Page.addScriptToEvaluateOnNewDocument", {"source": NO_PASSKEYS_JS, "runImmediately": True})
        await send("Runtime.evaluate", {"expression": NO_PASSKEYS_JS})  # the page already loaded before we attached
        await send("Network.enable", {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0})  # events only, no bodies kept
        await send("Page.getFrameTree", {}, reply_as="frametree")
        await send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]})
        await send("Runtime.evaluate", {"expression": "location.href", "returnByValue": True}, reply_as="url")
        await send("Page.startScreencast", {"format": "jpeg", "quality": 70})

        async def from_cdp() -> None:
            async for raw in cdp:
                m = json.loads(raw)
                method = m.get("method")
                if method == "Page.screencastFrame":
                    p = m["params"]
                    await send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
                    meta = p["metadata"]
                    await websocket.send_text(json.dumps({"t": "frame", "data": p["data"], "w": meta["deviceWidth"], "h": meta["deviceHeight"]}))
                elif method == "Page.javascriptDialogOpening":
                    # alert/confirm/prompt are browser-drawn too: answer them and show the text instead
                    await send("Page.handleJavaScriptDialog", {"accept": True, "promptText": m["params"].get("defaultPrompt", "")})
                    await websocket.send_text(json.dumps({"t": "dialog", "value": m["params"].get("message", "")[:300]}))
                elif method == "Fetch.requestPaused":
                    asyncio.create_task(guard(m["params"]))  # noqa: RUF006 - answered or failed by guard() itself
                elif m.get("id") in replies and replies[m["id"]] == "frametree":
                    main["frame"] = m["result"]["frameTree"]["frame"]["id"]
                    del replies[m["id"]]
                elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading") and m["params"]["frameId"] == main["frame"]:
                    busy["page"] = method == "Page.frameStartedLoading"
                    await report_busy()
                    if not busy["page"] and not main["importing"] and landed(main["url"], landing):
                        main["importing"] = True
                        asyncio.create_task(import_when_signed_in())  # noqa: RUF006 - ends on its own
                elif method == "Network.requestWillBeSent" and m["params"].get("type") in BUSY_TYPES:
                    busy["requests"].add(m["params"]["requestId"])  # e.g. a sign-in form posted by script
                    await report_busy()
                elif method in ("Network.loadingFinished", "Network.loadingFailed") and m["params"]["requestId"] in busy["requests"]:
                    busy["requests"].discard(m["params"]["requestId"])
                    await report_busy()
                elif method == "Page.frameNavigated" and not m["params"]["frame"].get("parentId"):
                    main["url"] = m["params"]["frame"]["url"]
                    await websocket.send_text(json.dumps({"t": "url", "value": main["url"]}))
                elif m.get("id") in replies:
                    value = m.get("result", {}).get("result", {}).get("value")
                    await websocket.send_text(json.dumps({"t": replies.pop(m["id"]), "value": value}))

        async def from_page() -> None:
            last_cursor = 0.0
            while True:
                msg = json.loads(await websocket.receive_text())
                if not isinstance(msg, dict):
                    continue
                commands = to_cdp(msg)
                for method, params in commands:
                    await send(method, params)
                if commands and msg.get("t") == "mouse" and asyncio.get_running_loop().time() - last_cursor > CURSOR_EVERY:
                    last_cursor = asyncio.get_running_loop().time()
                    p = commands[0][1]
                    await send("Runtime.evaluate", {"expression": CURSOR_JS % (p["x"], p["y"]), "returnByValue": True}, reply_as="cursor")
                if (msg.get("t") == "mouse" and msg.get("type") == "mouseReleased") or msg.get("t") == "text" or (
                        msg.get("t") == "key" and not msg.get("down")):
                    await send("Runtime.evaluate", {"expression": FOCUS_JS, "returnByValue": True}, reply_as="focus")

        tasks = [asyncio.create_task(from_cdp()), asyncio.create_task(from_page())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if (e := t.exception()) is not None:
                log.debug("live login ended: %r", e)  # a side closing is the normal way out
        return main["imported"]
