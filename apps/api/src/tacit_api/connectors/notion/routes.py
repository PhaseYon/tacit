import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ...config import get_settings
from ...deps import CurrentWorkspace
from . import handlers, sync
from .client import exchange, verify_signature

log = logging.getLogger(__name__)
router = APIRouter(tags=["notion"])

STATE_AUDIENCE = "tacit:notion-connect"
STATE_TTL = 600
ADMIN_ROLES = ("owner", "admin")


# --- Webhooks -------------------------------------------------------------------------


@router.post("/webhooks/notion")
async def notion_webhook(
    request: Request, x_notion_signature: str | None = Header(default=None)
) -> JSONResponse:
    """Verify, store once, enqueue. No Notion calls in the request path."""
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from exc

    if not verify_signature(body, x_notion_signature):
        # Setup only: before a token is configured, Notion's one unsigned verification
        # request (exactly {"verification_token": ...}) is stored for the operator.
        configured = get_settings().notion_webhook_verification_token.get_secret_value()
        if (
            not configured
            and isinstance(payload, dict)
            and set(payload) == {"verification_token"}
            and isinstance(payload["verification_token"], str)
        ):
            await handlers.record_verification(payload)
            return JSONResponse({"received": True})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")

    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("type"):
        return JSONResponse({"ignored": True})
    # Notion's retries reuse the event id; so does a replayed capture.
    did = await handlers.record_delivery(payload)
    if did is None:
        return JSONResponse({"duplicate": True})
    handlers.enqueue_delivery(did)
    return JSONResponse({"queued": True})


# --- Connect flow (OAuth) -------------------------------------------------------------


def _state_secret() -> bytes:
    secret = get_settings().better_auth_secret.get_secret_value().encode()
    return hmac.new(secret, b"tacit:notion-connect-state", hashlib.sha256).digest()


def make_state(workspace_id: str, user_id: str, return_to: str) -> str:
    now = int(time.time())
    claims = {
        "aud": STATE_AUDIENCE,
        "ws": workspace_id,
        "sub": user_id,
        "rt": return_to,
        "nonce": secrets.token_urlsafe(8),
        "iat": now,
        "exp": now + STATE_TTL,
    }
    return jwt.encode(claims, _state_secret(), algorithm="HS256")


def read_state(token: str) -> dict:
    return jwt.decode(token, _state_secret(), algorithms=["HS256"], audience=STATE_AUDIENCE)


def redirect_uri() -> str:
    return f"{get_settings().api_public_url}/notion/oauth/callback"


class ConnectIn(BaseModel):
    return_to: str = "/dashboard"


class ConnectOut(BaseModel):
    url: str


@router.post("/workspaces/{workspace_id}/notion/connect")
async def notion_connect(ws: CurrentWorkspace, body: ConnectIn) -> ConnectOut:
    """Any member may connect their own Notion: its content is visible only to them."""
    s = get_settings()
    if not s.notion_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Notion is not configured")
    return_to = body.return_to if body.return_to.startswith("/") else "/dashboard"
    params = {
        "client_id": s.notion_client_id,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": redirect_uri(),
        "state": make_state(ws.workspace_id, ws.principal.user_id, return_to),
    }
    return ConnectOut(url=f"https://api.notion.com/v1/oauth/authorize?{urlencode(params)}")


def _back(return_to: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(f"{get_settings().web_origin}{return_to}{sep}{urlencode(params)}")


@router.get("/notion/oauth/callback")
async def notion_oauth_callback(
    state: str, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Notion redirects here. `state` (signed by us) says which Tacit user started it;
    the token response says which Notion user approved. Together: the identity link."""
    try:
        st = read_state(state)
    except jwt.PyJWTError:
        return _back("/dashboard", notion_error="expired")
    return_to, workspace_id, user_id = st["rt"], st["ws"], st["sub"]
    if error or not code:
        return _back(return_to, notion_error="cancelled")
    try:
        oauth = await exchange(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}
        )
        connection_id = await sync.upsert_connection(workspace_id, user_id, oauth)
    except sync.ConnectError:
        return _back(return_to, notion_error="not_allowed")
    except DBAPIError:
        # This Notion authorization is already linked to another Tacit workspace.
        return _back(return_to, notion_error="taken")
    except Exception:
        log.exception("Notion connect failed")
        return _back(return_to, notion_error="failed")

    from .tasks import sync_connection

    sync_connection.delay(workspace_id, connection_id, "full")
    return _back(return_to, notion="connected")


# --- Status, visibility, disconnect (UI) ----------------------------------------------


class ConnectionOut(BaseModel):
    id: str
    notion_workspace_name: str | None
    owner_name: str | None
    is_mine: bool
    created_at: datetime


class TreeOut(BaseModel):
    id: str
    connection_id: str
    title: str
    url: str | None
    workspace_visible: bool
    is_mine: bool
    can_read: bool
    documents: int


class NotionStatusOut(BaseModel):
    configured: bool
    is_admin: bool
    connections: list[ConnectionOut]
    trees: list[TreeOut]


@router.get("/workspaces/{workspace_id}/notion")
async def notion_status(ws: CurrentWorkspace) -> NotionStatusOut:
    me = ws.principal.user_id
    conns = await ws.db.execute(
        text(
            "SELECT nc.id::text, nc.notion_workspace_name, u.name AS owner_name,"
            " nc.user_id = :me AS is_mine, nc.created_at FROM notion_connection nc"
            ' LEFT JOIN "user" u ON u.id = nc.user_id'
            " WHERE nc.revoked_at IS NULL ORDER BY nc.created_at"
        ),
        {"me": me},
    )
    # `documents` counts only what this user can see (RLS applies to the count too).
    trees = await ws.db.execute(
        text(
            "SELECT c.id::text, t.connection_id::text, t.title, c.url, t.workspace_visible,"
            " nc.user_id = :me AS is_mine, app.can_read_container(c.id) AS can_read,"
            " (SELECT count(*) FROM source_document d"
            "   WHERE d.container_id = c.id AND d.deleted_at IS NULL) AS documents"
            " FROM notion_tree t JOIN source_container c ON c.id = t.container_id"
            " JOIN notion_connection nc ON nc.id = t.connection_id"
            " WHERE c.is_active AND nc.revoked_at IS NULL ORDER BY t.title"
        ),
        {"me": me},
    )
    return NotionStatusOut(
        configured=get_settings().notion_enabled,
        is_admin=ws.role in ADMIN_ROLES,
        connections=[ConnectionOut(**r._asdict()) for r in conns],
        trees=[TreeOut(**r._asdict()) for r in trees],
    )


class VisibilityIn(BaseModel):
    workspace_visible: bool


@router.put("/workspaces/{workspace_id}/notion/trees/{tree_id}/visibility")
async def set_visibility(ws: CurrentWorkspace, tree_id: str, body: VisibilityIn) -> dict:
    """Widening (visible to the whole workspace) needs an admin who is ALSO the
    connection's owner: only they can read the pages and attest for them. Narrowing is
    allowed for the owner or any admin."""
    row = (
        await ws.db.execute(
            text(
                "SELECT nc.user_id FROM notion_tree t"
                " JOIN notion_connection nc ON nc.id = t.connection_id"
                " WHERE t.container_id::text = :id AND nc.revoked_at IS NULL"
            ),
            {"id": tree_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such Notion page tree")
    is_owner = row.user_id == ws.principal.user_id
    is_admin = ws.role in ADMIN_ROLES
    allowed = (is_admin and is_owner) if body.workspace_visible else (is_admin or is_owner)
    if not allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a workspace admin who connected these pages can share them with everyone",
        )
    await sync.set_tree_visibility(
        ws.workspace_id, tree_id, body.workspace_visible, ws.principal.user_id
    )
    return {"workspace_visible": body.workspace_visible}


@router.delete("/workspaces/{workspace_id}/notion/connections/{connection_id}")
async def disconnect(ws: CurrentWorkspace, connection_id: str) -> dict:
    owner = await ws.db.scalar(
        text("SELECT user_id FROM notion_connection WHERE id::text = :id AND revoked_at IS NULL"),
        {"id": connection_id},
    )
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such Notion connection")
    if owner != ws.principal.user_id and ws.role not in ADMIN_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner or an admin can do this")
    await sync.revoke_connection(ws.workspace_id, connection_id)
    return {"disconnected": True}
