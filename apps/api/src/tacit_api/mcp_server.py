"""Tacit as an MCP server (streamable HTTP at /mcp), for Claude, Cursor and friends.

Auth: OAuth 2.1. The web app (Better Auth `mcp()` plugin) is the authorization server;
this is the protected resource. Access tokens are JWTs with aud = MCP_RESOURCE_URL,
verified against the web app's JWKS. Every tool runs as the token's user through the
same RLS as the web app, so an MCP client sees exactly what that user can see.
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import Field
from sqlalchemy import or_, select, text

from .config import get_settings
from .db import rls_session, set_rls_context
from .models import Member, Workspace
from .search.hybrid import hybrid_search


def make_verifier(public_key: str | None = None) -> JWTVerifier:
    s = get_settings()
    return JWTVerifier(
        jwks_uri=None if public_key else s.auth_jwks_url,
        public_key=public_key,
        issuer=s.auth_jwt_issuer,
        audience=s.mcp_resource_url,
        algorithm="EdDSA",  # Better Auth's JWT plugin default (Ed25519)
    )


def _auth() -> RemoteAuthProvider:
    s = get_settings()
    return RemoteAuthProvider(
        token_verifier=make_verifier(),
        authorization_servers=[s.auth_jwt_issuer],
        base_url=s.api_public_url,
        resource_name="Tacit",
    )


mcp = FastMCP(
    "Tacit",
    instructions=(
        "Tacit is the team's memory: PRs, issues, reviews and design docs. Use `search` to "
        "answer 'why did we do X, who decided, and where is the evidence?'. Always cite the "
        "`url` of each source you rely on. Results only include what the user may access."
    ),
    auth=_auth(),
)


def _user_id() -> str:
    token = get_access_token()
    if token is None or not token.claims.get("sub"):
        raise ToolError("Not authenticated")
    return str(token.claims["sub"])


async def _resolve_workspace(user_id: str, workspace: str | None) -> tuple[str, str]:
    """Pick the workspace by id or slug, or the only one the user has. Returns (id, name)."""
    async with rls_session(user_id) as s:
        q = (
            select(Workspace.id, Workspace.name)
            .join(Member, Member.workspace_id == Workspace.id)
            .where(Member.user_id == user_id)
        )
        if workspace:
            q = q.where(or_(Workspace.id == workspace, Workspace.slug == workspace))
        rows = (await s.execute(q)).all()
    if not rows:
        raise ToolError("Workspace not found" if workspace else "You're not in any workspace")
    if len(rows) > 1:
        names = ", ".join(f"{r.name}" for r in rows)
        raise ToolError(f"You're in several workspaces ({names}); pass `workspace` (slug or id).")
    return rows[0].id, rows[0].name


@mcp.tool
async def list_workspaces() -> list[dict[str, str]]:
    """List the workspaces you belong to (use the slug as `workspace` in other tools)."""
    user_id = _user_id()
    async with rls_session(user_id) as s:
        rows = await s.execute(
            select(Workspace.id, Workspace.name, Workspace.slug, Member.role)
            .join(Member, Member.workspace_id == Workspace.id)
            .where(Member.user_id == user_id)
            .order_by(Workspace.name)
        )
        return [{"id": r.id, "name": r.name, "slug": r.slug, "role": r.role} for r in rows]


@mcp.tool
async def search(
    query: Annotated[str, Field(description="Natural-language question or keywords", min_length=1)],
    workspace: Annotated[str | None, Field(description="Workspace slug or id")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
) -> dict[str, Any]:
    """Hybrid (semantic + keyword) search over the team's PRs, issues, reviews and docs.

    Returns the best passage per source with a citation URL. Cite `url` in answers.
    """
    user_id = _user_id()
    ws_id, ws_name = await _resolve_workspace(user_id, workspace)
    async with rls_session(user_id, ws_id) as s:
        hits = await hybrid_search(s, query, limit)
    return {
        "workspace": ws_name,
        "results": [
            {
                "document_id": h.document_id,
                "title": h.title,
                "kind": h.kind,
                "repo": h.container,
                "author": h.author,
                "date": h.created_at.date().isoformat() if h.created_at else None,
                "state": h.state,
                "url": h.url,
                "passage": h.snippet.replace("**", ""),
            }
            for h in hits
        ],
    }


@mcp.tool
async def get_document(
    document_id: Annotated[str, Field(description="`document_id` from search results")],
    workspace: Annotated[str | None, Field(description="Workspace slug or id")] = None,
) -> dict[str, Any]:
    """Full text of a source plus its discussion thread (e.g. a PR with its reviews and
    comments), each with a citation URL. Use after `search` to read the evidence."""
    user_id = _user_id()
    ws_id, _ = await _resolve_workspace(user_id, workspace)
    async with rls_session(user_id, ws_id) as s:
        await set_rls_context(s, user_id=user_id, workspace_id=ws_id)
        doc = (
            await s.execute(
                text(
                    "SELECT id::text, parent_id::text, kind, title, body, url, author_login,"
                    " source_created_at, metadata FROM source_document"
                    " WHERE id = CAST(:id AS uuid) AND deleted_at IS NULL"
                ),
                {"id": document_id},
            )
        ).one_or_none()
        if doc is None:
            # Same answer for "doesn't exist" and "no access": nothing to probe.
            raise ToolError("Document not found")
        root_id = doc.parent_id or doc.id
        thread = (
            await s.execute(
                text(
                    "SELECT id::text, kind, title, body, url, author_login, source_created_at"
                    " FROM source_document WHERE (id = CAST(:root AS uuid)"
                    " OR parent_id = CAST(:root AS uuid)) AND deleted_at IS NULL"
                    " ORDER BY source_created_at NULLS FIRST LIMIT 100"
                ),
                {"root": root_id},
            )
        ).all()
    return {
        "document": {
            "id": doc.id,
            "kind": doc.kind,
            "title": doc.title,
            "url": doc.url,
            "author": doc.author_login,
            "date": doc.source_created_at.isoformat() if doc.source_created_at else None,
            "metadata": doc.metadata,
            "body": doc.body,
        },
        "thread": [
            {
                "id": t.id,
                "kind": t.kind,
                "title": t.title,
                "url": t.url,
                "author": t.author_login,
                "date": t.source_created_at.isoformat() if t.source_created_at else None,
                "body": t.body[:4000],
            }
            for t in thread
            if t.id != doc.id
        ],
    }


mcp_app = mcp.http_app(path="/mcp")
