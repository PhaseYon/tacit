import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text

from ...config import get_settings
from ...deps import CurrentWorkspace
from . import intake, sync
from .client import app_client, installation_client, verify_signature

router = APIRouter(tags=["github"])

STATE_AUDIENCE = "tacit:github-install"
STATE_TTL = 600


# --- Webhooks -------------------------------------------------------------------------


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(),
    x_github_delivery: str = Header(),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")
    if x_github_event == "ping":
        return JSONResponse({"ok": True}, status_code=status.HTTP_200_OK)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from exc

    if not await intake.record_delivery(x_github_delivery, x_github_event, payload):
        return JSONResponse({"duplicate": True}, status_code=status.HTTP_200_OK)
    intake.enqueue_delivery(x_github_delivery)
    return JSONResponse({"queued": True}, status_code=status.HTTP_202_ACCEPTED)


# --- Install flow ---------------------------------------------------------------------


def _state_secret() -> bytes:
    # Derived per purpose, so the shared secret is never used raw for two things.
    secret = get_settings().better_auth_secret.get_secret_value().encode()
    return hmac.new(secret, b"tacit:github-install-state", hashlib.sha256).digest()


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


class ConnectIn(BaseModel):
    return_to: str = "/dashboard"


class ConnectOut(BaseModel):
    url: str


@router.post("/workspaces/{workspace_id}/github/connect")
async def github_connect(ws: CurrentWorkspace, body: ConnectIn) -> ConnectOut:
    s = get_settings()
    if not s.github_enabled or not s.github_app_slug:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App is not configured")
    if ws.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace admins can connect GitHub")
    if await sync.linked_github_id(ws.workspace_id, ws.principal.user_id) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "github_not_linked", "message": "Link your GitHub account first"},
        )
    return_to = body.return_to if body.return_to.startswith("/") else "/dashboard"
    state = make_state(ws.workspace_id, ws.principal.user_id, return_to)
    return ConnectOut(
        url=f"https://github.com/apps/{s.github_app_slug}/installations/new?state={state}"
    )


def _back(return_to: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(f"{get_settings().web_origin}{return_to}{sep}{urlencode(params)}")


@router.get("/github/setup")
async def github_setup(
    state: str, installation_id: int | None = None, setup_action: str | None = None
) -> RedirectResponse:
    """GitHub redirects the installer here. Nothing in the query string is trusted:
    `state` is signed by us, and the installation is checked against the user's
    OAuth-verified GitHub identity before it's linked."""
    try:
        st = read_state(state)
    except jwt.PyJWTError:
        return _back("/dashboard", github_error="expired")
    return_to, workspace_id, user_id = st["rt"], st["ws"], st["sub"]
    if setup_action == "request":
        return _back(return_to, github="requested")  # an org admin must approve it
    if installation_id is None:
        return _back(return_to, github_error="missing_installation")

    github_user_id = await sync.linked_github_id(workspace_id, user_id)
    if github_user_id is None:
        return _back(return_to, github_error="not_linked")
    try:
        inst = await sync.verify_installer(
            app_client(), installation_client(installation_id), installation_id, github_user_id
        )
        await sync.upsert_installation(workspace_id, inst, user_id)
    except sync.InstallVerificationError:
        return _back(return_to, github_error="not_allowed")
    except Exception:
        return _back(return_to, github_error="failed")

    from .tasks import sync_installation

    sync_installation.delay(workspace_id, installation_id)
    return _back(return_to, github="connected")


# --- Status for the UI ----------------------------------------------------------------


class RepoOut(BaseModel):
    id: str
    full_name: str
    url: str | None
    visibility: str
    backfill_status: str
    acl_synced_at: datetime | None
    can_read: bool
    documents: int


class GitHubStatusOut(BaseModel):
    configured: bool
    installations: list[str]
    repos: list[RepoOut]


@router.get("/workspaces/{workspace_id}/github")
async def github_status(ws: CurrentWorkspace) -> GitHubStatusOut:
    accounts = (
        await ws.db.execute(
            text(
                "SELECT account_login FROM github_installation WHERE deleted_at IS NULL"
                " ORDER BY created_at"
            )
        )
    ).scalars()
    # `documents` counts only what this user can see (RLS applies to the count too).
    rows = await ws.db.execute(
        text(
            "SELECT c.id::text, g.full_name, c.url, c.visibility, g.backfill_status,"
            " c.acl_synced_at, app.can_read_container(c.id) AS can_read,"
            " (SELECT count(*) FROM source_document d"
            "   WHERE d.container_id = c.id AND d.deleted_at IS NULL) AS documents"
            " FROM source_container c JOIN github_repository g ON g.container_id = c.id"
            " WHERE c.is_active ORDER BY g.full_name"
        )
    )
    return GitHubStatusOut(
        configured=get_settings().github_enabled,
        installations=list(accounts),
        repos=[RepoOut(**r._asdict()) for r in rows],
    )
