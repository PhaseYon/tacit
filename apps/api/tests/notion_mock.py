"""A fake Notion API for tests, served through respx (notion-client uses httpx).

State is plain dicts: objects (pages and data sources) by id, block children by parent
id, users. `access` maps a bearer token to the object ids it may read (default: all),
so tests can model per-connection sharing. Every call is recorded.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

API = "https://notion.test"
WEBHOOK_TOKEN = "secret_test_notion_verification"


@dataclass
class Call:
    method: str
    path: str
    token: str | None
    body: dict[str, Any] | None
    auth: str = ""


@dataclass
class FakeNotion:
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    children: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    users: list[dict[str, Any]] = field(default_factory=list)
    access: dict[str, set[str]] = field(default_factory=dict)
    hidden_from_search: set[str] = field(default_factory=set)
    oauth: dict[str, Any] = field(default_factory=dict)
    # path -> (status, body), checked before normal handling (e.g. 429s, 401s)
    failures: dict[str, tuple[int, dict[str, Any], dict[str, str]]] = field(default_factory=dict)
    search_page_size: int = 100
    children_page_size: int = 100
    calls: list[Call] = field(default_factory=list)

    def add(self, *objs: dict[str, Any]) -> None:
        for o in objs:
            self.objects[o["id"]] = o

    def calls_to(self, prefix: str) -> list[Call]:
        return [c for c in self.calls if c.path.startswith(prefix)]

    def _visible(self, token: str | None, oid: str) -> bool:
        return oid in self.objects and (token not in self.access or oid in self.access[token])

    @staticmethod
    def _error(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
        body = {"object": "error", "status": status, "code": code, "message": code}
        return httpx.Response(status, json=body, headers=headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        body = json.loads(request.content) if request.content else None
        self.calls.append(Call(request.method, path, token, body, auth))
        if path in self.failures:
            status, fbody, headers = self.failures[path]
            return httpx.Response(status, json=fbody, headers=headers)

        if path == "oauth/token":
            return httpx.Response(200, json=self.oauth)
        if path == "search":
            visible = [
                o
                for oid, o in self.objects.items()
                if self._visible(token, oid) and oid not in self.hidden_from_search
            ]
            visible.sort(key=lambda o: o.get("last_edited_time", ""), reverse=True)
            return self._paginate(visible, body.get("start_cursor"), self.search_page_size)
        if path == "users":
            return self._paginate(self.users, request.url.params.get("start_cursor"), 100)
        if path.startswith("pages/"):
            oid = path.split("/")[1]
            if not self._visible(token, oid):
                return self._error(404, "object_not_found")
            return httpx.Response(200, json=self.objects[oid])
        if path.startswith("blocks/") and path.endswith("/children"):
            bid = path.split("/")[1]
            if bid not in self.children and not self._visible(token, bid):
                return self._error(404, "object_not_found")
            return self._paginate(
                self.children.get(bid, []),
                request.url.params.get("start_cursor"),
                self.children_page_size,
            )
        return self._error(400, "invalid_request_url")

    @staticmethod
    def _paginate(items: list[Any], cursor: str | None, size: int) -> httpx.Response:
        start = int(cursor or 0)
        page = items[start : start + size]
        more = start + size < len(items)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": page,
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
            },
        )


@pytest.fixture
def notion_api(monkeypatch):
    from tacit_api.config import get_settings

    fake = FakeNotion()
    monkeypatch.setenv("NOTION_API_URL", API)
    monkeypatch.setenv("NOTION_CLIENT_ID", "notion-cid")
    monkeypatch.setenv("NOTION_CLIENT_SECRET", "notion-csecret")
    monkeypatch.setenv("NOTION_WEBHOOK_VERIFICATION_TOKEN", WEBHOOK_TOKEN)
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as router:
        # Late-bound, so a test can wrap fake.handle.
        router.route(host="notion.test").mock(side_effect=lambda request: fake.handle(request))
        yield fake
    get_settings.cache_clear()


def sign(body: bytes, secret: str = WEBHOOK_TOKEN) -> dict[str, str]:
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Notion-Signature": sig, "Content-Type": "application/json"}


# --- Builders -------------------------------------------------------------------------


def rt(text: str, href: str | None = None) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text, "href": href, "text": {"content": text}}]


def page(
    pid: str,
    title: str,
    *,
    parent: tuple[str, str | None] = ("workspace", None),
    edited: str = "2025-10-01T10:00:00.000Z",
    author: str = "nu-alice",
    props: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    ptype, pid_ = parent
    parent_obj: dict[str, Any] = (
        {"type": "workspace", "workspace": True}
        if ptype == "workspace"
        else {"type": f"{ptype}_id", f"{ptype}_id": pid_}
    )
    return {
        "object": "page",
        "id": pid,
        "url": f"https://www.notion.so/{title.replace(' ', '-')}-{pid.replace('-', '')}",
        "created_time": "2025-09-01T09:00:00.000Z",
        "last_edited_time": edited,
        "created_by": {"object": "user", "id": author},
        "parent": parent_obj,
        "in_trash": False,
        "properties": {
            "Name": {"id": "title", "type": "title", "title": rt(title)},
            **(props or {}),
        },
        **extra,
    }


def data_source(dsid: str, title: str, database_id: str, parent: tuple[str, str | None]):
    ptype, pid = parent
    return {
        "object": "data_source",
        "id": dsid,
        "title": rt(title),
        "url": f"https://www.notion.so/{dsid.replace('-', '')}",
        "last_edited_time": "2025-09-15T10:00:00.000Z",
        "parent": {"type": "database_id", "database_id": database_id},
        "database_parent": {"type": f"{ptype}_id", f"{ptype}_id": pid}
        if ptype != "workspace"
        else {"type": "workspace", "workspace": True},
    }


def block(btype: str, text: str = "", *, bid: str | None = None, **data: Any) -> dict[str, Any]:
    content: dict[str, Any] = {"rich_text": rt(text)} if text else {}
    content.update(data)
    return {
        "object": "block",
        "id": bid or f"b-{btype}-{abs(hash(text)) % 10**8}",
        "type": btype,
        "has_children": False,
        btype: content,
    }
