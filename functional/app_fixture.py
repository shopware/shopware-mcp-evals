"""A Shopware app that exists for one run: its zip and the server behind it.

`functional/app_capabilities.py` installs it to check that an app's MCP tool,
prompt and resource reach a client with the app's content. That needs a real
app server, because Shopware proxies every app capability to the app's URL as a
signed POST, and prompts and resources are only registered for an app that went
through registration and holds a secret.

The server is stdlib only and runs in a thread of the suite's own process, so CI
starts nothing extra and nothing outlives the run. It speaks the two halves of
the app protocol the check needs:

  registration  GET  /register   signed with the manifest secret, answered with
                                 the proof and the app secret
                POST /confirm    signed with the app secret
  capabilities  POST /tool       `{"success": true, "data": {"echo": <text>}}`
                POST /prompt     one user message
                POST /resource   `{"uri", "mimeType", "text"}`

Every request is checked against its HMAC, and one that does not verify is
answered 401. Each is recorded, so a failing check can say whether Shopware
reached the app at all.

Both secrets are minted per run with `secrets.token_hex` and only ever live in
this process and in the shop the app is installed into.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import cast, override
from urllib.parse import parse_qsl, urlsplit

APP_NAME = "McpEvalsFixture"
TOOL_NAME = f"{APP_NAME}-echo"
PROMPT_NAME = f"{APP_NAME}-briefing"
RESOURCE_URI = "mcp-evals-fixture://status"
RESOURCE_MIME_TYPE = "application/json"

PROMPT_TEXT = "You are checking that an app prompt reaches the client with the app's own words."
# JSON on purpose: a server that forwards the app's whole response as the text
# would still produce valid JSON, so only the exact text tells the two apart.
RESOURCE_TEXT = json.dumps({"fixture": APP_NAME, "status": "ok"})

TEMPLATE = Path(__file__).resolve().parent / "assets" / APP_NAME
APP_SIGNATURE = "shopware-app-signature"
SHOP_SIGNATURE = "shopware-shop-signature"


def sign(secret: str, payload: bytes) -> str:
    """HMAC-SHA256 as hex, the way Shopware signs both directions."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def render_app(base_url: str, secret: str) -> bytes:
    """The app as an uploadable zip, pointing at `base_url` and registering with `secret`.

    The zip holds a single `McpEvalsFixture/` root, which is what the extension
    upload unpacks into `custom/apps/`.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(TEMPLATE.rglob("*")):
            if path.is_file():
                text = path.read_text().replace("{{BASE_URL}}", base_url).replace("{{SECRET}}", secret)
                archive.writestr(f"{APP_NAME}/{path.relative_to(TEMPLATE).as_posix()}", text)
    return buffer.getvalue()


@dataclass(frozen=True)
class Received:
    """One request the app server answered."""

    method: str
    path: str
    verified: bool
    body: object = None


@dataclass
class AppState:
    """What the handler needs to know and what it saw. Shared across threads,
    so `received` is only appended to under the lock."""

    manifest_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    app_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    received: list[Received] = field(default_factory=list[Received])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, entry: Received) -> None:
        with self.lock:
            self.received.append(entry)

    def seen(self, path: str) -> list[Received]:
        with self.lock:
            return [entry for entry in self.received if entry.path == path]


class _Handler(BaseHTTPRequestHandler):
    server: _Server  # pyright: ignore[reportIncompatibleVariableOverride]

    @override
    def log_message(self, format: str, *args: object) -> None:  # the base class names it `format`
        """Quiet: the suite prints its own report, and a request log would interleave with it."""

    def do_GET(self) -> None:  # BaseHTTPRequestHandler dispatches on this name
        state = self.server.state
        url = urlsplit(self.path)
        if url.path != "/register":
            self._answer(404, {"success": False, "error": f"no GET {url.path}"})
            return
        verified = hmac.compare_digest(
            self.headers.get(APP_SIGNATURE, ""), sign(state.manifest_secret, url.query.encode())
        )
        state.record(Received("GET", url.path, verified))
        if not verified:
            self._answer(401, {"success": False, "error": "bad signature"})
            return
        query = dict(parse_qsl(url.query))
        proof = sign(state.manifest_secret, f"{query.get('shop-id', '')}{query.get('shop-url', '')}{APP_NAME}".encode())
        host = self.headers.get("Host", "")
        self._answer(200, {"proof": proof, "secret": state.app_secret, "confirmation_url": f"http://{host}/confirm"})

    def do_POST(self) -> None:  # BaseHTTPRequestHandler dispatches on this name
        state = self.server.state
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        verified = hmac.compare_digest(self.headers.get(SHOP_SIGNATURE, ""), sign(state.app_secret, raw))
        try:
            body = cast(object, json.loads(raw)) if raw else None
        except ValueError:
            body = None
        state.record(Received("POST", self.path, verified, body))
        if not verified:
            self._answer(401, {"success": False, "error": "bad signature"})
            return
        answer = _ANSWERS.get(self.path)
        if answer is None:
            self._answer(404, {"success": False, "error": f"no POST {self.path}"})
            return
        self._answer(200, answer(body))

    def _answer(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _arguments(body: object) -> dict[str, object]:
    if isinstance(body, dict):
        arguments = cast(dict[str, object], body).get("arguments")
        if isinstance(arguments, dict):
            return cast(dict[str, object], arguments)
    return {}


def confirm_answer(_body: object) -> object:
    return {"success": True}


def tool_answer(body: object) -> object:
    return {"success": True, "data": {"echo": _arguments(body).get("text")}}


def prompt_answer(_body: object) -> object:
    return [{"role": "user", "content": PROMPT_TEXT}]


def resource_answer(body: object) -> object:
    return {"uri": _arguments(body).get("uri", RESOURCE_URI), "mimeType": RESOURCE_MIME_TYPE, "text": RESOURCE_TEXT}


_ANSWERS: dict[str, Callable[[object], object]] = {
    "/confirm": confirm_answer,
    "/tool": tool_answer,
    "/prompt": prompt_answer,
    "/resource": resource_answer,
}


class _Server(ThreadingHTTPServer):
    daemon_threads: bool = True

    def __init__(self, bind: str, state: AppState) -> None:
        super().__init__((bind, 0), _Handler)
        self.state: AppState = state


class AppServer:
    """The app's server for the length of a `with` block, on a free port.

    `bind` is the address it listens on; the URL the shop is given is built
    from the host the shop reaches this process by, which differs whenever the
    shop runs in a container.
    """

    def __init__(self, bind: str = "127.0.0.1") -> None:
        self.state: AppState = AppState()
        self._server: _Server = _Server(bind, self.state)
        self._thread: threading.Thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def base_url(self, host: str) -> str:
        return f"http://{host}:{self.port}"

    def __enter__(self) -> AppServer:
        self._thread.start()
        return self

    def __exit__(
        self, _type: type[BaseException] | None, _value: BaseException | None, _traceback: TracebackType | None
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
