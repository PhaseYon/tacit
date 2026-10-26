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
from .client import BOT_SCOPES, bot_client, oauth_client, verify_signature

log = logging.getLogger(__name__)
router = APIRouter(tags=["slack"])

STATE_AUDIENCE = "tacit:slack-install"
STATE_TTL = 600


# --- Events API -----------------------------------------------------------------------


@router.post("/webhooks/slack")
async def slack_events(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> JSONResponse:
    """Verify, answer url_verification, store once, enqueue. No Slack calls here:
    Slack expects a response within 3 seconds."""
    body = await request.body()
    # Also rejects timestamps more than 5 minutes off (replays of captured requests).
    if not verify_signature(body, x_slack_request_timestamp, x_slack_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from exc

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})
    if payload.get("type") != "event_callback" or not payload.get("event_id"):
        return JSONResponse({"ignored": True})
    # Slack retries (and replays inside the 5-minute window) reuse event_id.
    did = await handlers.record_delivery(payload)
    if did is None:
        return JSONResponse({"duplicate": True})
    handlers.enqueue_delivery(did)
    return JSONResponse({"queued": True})


# --- Install flow (OAuth v2) ----------------------------------------------------------


def _state_secret() -> bytes:
    secret = get_settings().better_auth_secret.get_secret_value().encode()
    return hmac.new(secret, b"tacit:slack-install-state", hashlib.sha256).digest()


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
    return f"{get_settings().api_public_url}/slack/oauth/callback"


class ConnectIn(BaseModel):
    return_to: str = "/dashboard"


class ConnectOut(BaseModel):
    url: str


@router.post("/workspaces/{workspace_id}/slack/connect")
async def slack_connect(ws: CurrentWorkspace, body: ConnectIn) -> ConnectOut:
    s = get_settings()
    if not s.slack_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Slack app is not configured")
    if ws.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace admins can connect Slack")
    if not await sync.linked_slack_accounts(ws.workspace_id, ws.principal.user_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "slack_not_linked", "message": "Link your Slack account first"},
        )
    return_to = body.return_to if body.return_to.startswith("/") else "/dashboard"
    params = {
        "client_id": s.slack_client_id,
        "scope": ",".join(BOT_SCOPES),
        "redirect_uri": redirect_uri(),
        "state": make_state(ws.workspace_id, ws.principal.user_id, return_to),
    }
    return ConnectOut(url=f"https://slack.com/oauth/v2/authorize?{urlencode(params)}")


def _back(return_to: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(f"{get_settings().web_origin}{return_to}{sep}{urlencode(params)}")


@router.get("/slack/oauth/callback")
async def slack_oauth_callback(
    state: str, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Slack redirects the installer here. `state` is signed by us; the install is
    accepted only if the approving Slack user is the Tacit user's verified Slack link."""
    try:
        st = read_state(state)
    except jwt.PyJWTError:
        return _back("/dashboard", slack_error="expired")
    return_to, workspace_id, user_id = st["rt"], st["ws"], st["sub"]
    if error or not code:
        return _back(return_to, slack_error="cancelled")

    s = get_settings()
    try:
        resp = await oauth_client().oauth_v2_access(
            client_id=s.slack_client_id,
            client_secret=s.slack_client_secret.get_secret_value(),
            code=code,
            redirect_uri=redirect_uri(),
        )
        oauth = resp.data
        sync.verify_installer(oauth, await sync.linked_slack_accounts(workspace_id, user_id))
        team_info = (await bot_client(oauth["access_token"]).team_info()).data.get("team") or {}
        await sync.upsert_installation(workspace_id, oauth, team_info, user_id)
    except sync.InstallVerificationError:
        return _back(return_to, slack_error="not_allowed")
    except DBAPIError:
        # RLS refused the upsert: this Slack team belongs to another workspace.
        return _back(return_to, slack_error="taken")
    except Exception:
        log.exception("Slack install failed")
        return _back(return_to, slack_error="failed")

    from .tasks import sync_team

    sync_team.delay(workspace_id, oauth["team"]["id"])
    return _back(return_to, slack="connected")


# --- Status for the UI ----------------------------------------------------------------


class ChannelOut(BaseModel):
    id: str
    name: str
    url: str | None
    is_private: bool
    backfill_status: str
    acl_synced_at: datetime | None
    can_read: bool
    documents: int


class SlackStatusOut(BaseModel):
    configured: bool
    teams: list[str]
    channels: list[ChannelOut]


@router.get("/workspaces/{workspace_id}/slack")
async def slack_status(ws: CurrentWorkspace) -> SlackStatusOut:
    teams = (
        await ws.db.execute(
            text(
                "SELECT team_name FROM slack_installation WHERE revoked_at IS NULL"
                " ORDER BY created_at"
            )
        )
    ).scalars()
    # `documents` counts only what this user can see (RLS applies to the count too).
    rows = await ws.db.execute(
        text(
            "SELECT c.id::text, sc.name, c.url, sc.is_private, sc.backfill_status,"
            " c.acl_synced_at, app.can_read_container(c.id) AS can_read,"
            " (SELECT count(*) FROM source_document d"
            "   WHERE d.container_id = c.id AND d.deleted_at IS NULL) AS documents"
            " FROM source_container c JOIN slack_channel sc ON sc.container_id = c.id"
            " WHERE c.is_active ORDER BY sc.name"
        )
    )
    return SlackStatusOut(
        configured=get_settings().slack_enabled,
        teams=list(teams),
        channels=[ChannelOut(**r._asdict()) for r in rows],
    )
