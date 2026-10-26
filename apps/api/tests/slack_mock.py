"""A fake Slack Web API on localhost for tests (no network).

slack_sdk's async client runs on aiohttp, which respx can't intercept (and aioresponses
doesn't support aiohttp 3.14), so tests point SLACK_API_URL at this in-process server.
Responses are queued per method; the last one repeats. Every call is recorded.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

SIGNING_SECRET = "test-slack-signing-secret"


@dataclass
class Call:
    method: str
    params: dict[str, str]
    token: str | None


@dataclass
class FakeSlack:
    responses: dict[str, list[tuple[int, dict[str, Any], dict[str, str]]]] = field(
        default_factory=dict
    )
    calls: list[Call] = field(default_factory=list)
    # method -> fn(params) -> response body, for answers that depend on the request
    dynamic: dict[str, Callable[[dict[str, str]], dict[str, Any]]] = field(default_factory=dict)

    def on(self, method: str, *bodies: dict[str, Any], status: int = 200, headers=None):
        """Queue responses for a method (replaces earlier ones). `ok` defaults to true."""
        self.dynamic.pop(method, None)
        self.responses[method] = [
            (status, {"ok": status == 200, **b}, headers or {}) for b in bodies
        ]

    def on_call(self, method: str, fn: Callable[[dict[str, str]], dict[str, Any]]) -> None:
        self.dynamic[method] = fn

    def calls_to(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = dict(request.query)
        if request.method == "POST":
            params |= {k: str(v) for k, v in (await request.post()).items()}
        auth = request.headers.get("Authorization", "")
        self.calls.append(Call(method, params, auth.removeprefix("Bearer ") or None))
        if method in self.dynamic:
            return web.json_response({"ok": True, **self.dynamic[method](params)})
        queue = self.responses.get(method)
        if not queue:
            return web.json_response({"ok": False, "error": "unknown_method"}, status=200)
        status, body, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        return web.json_response(body, status=status, headers=headers)


@pytest.fixture
async def slack_api(monkeypatch):
    from tacit_api.config import get_settings

    fake = FakeSlack()
    app = web.Application()
    app.router.add_route("*", "/api/{method}", fake.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    monkeypatch.setenv("SLACK_API_URL", str(server.make_url("/api/")))
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    get_settings.cache_clear()
    yield fake
    await server.close()
    get_settings.cache_clear()


def sign(body: bytes, ts: int | None = None, secret: str = SIGNING_SECRET) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": sig,
        "Content-Type": "application/json",
    }


def user(uid: str, name: str, **flags: Any) -> dict[str, Any]:
    return {"id": uid, "name": name, "profile": {"real_name": name.title()}, **flags}


def message(ts: str, text: str, user_id: str = "UA", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "ts": ts, "user": user_id, "text": text, **extra}
