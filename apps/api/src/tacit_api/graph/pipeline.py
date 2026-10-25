"""Scan + process graph episodes (design §7). Runs as tacit_worker, one workspace per
transaction. Graphiti needs episodes within a group added one at a time, in order, so
processing is serial per container (callers hold a per-group lock)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text

from ..db import worker_session
from ..llm import LLMProvider
from ..search.embeddings import get_embedder
from .bundles import Bundle, eligible_bundles, render
from .client import group_for
from .costs import BudgetExceeded, CostContext, current_cost
from .ontology import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, EXTRACTION_INSTRUCTIONS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    enabled: bool
    include_private: bool
    monthly_budget_usd: Decimal


async def workspace_settings(workspace_id: str) -> Settings:
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT enabled, include_private, monthly_budget_usd FROM workspace_graph_settings"
                    " WHERE workspace_id = :ws"
                ),
                {"ws": workspace_id},
            )
        ).one_or_none()
    if row is None:
        return Settings(False, False, Decimal(0))
    return Settings(row.enabled, row.include_private, Decimal(row.monthly_budget_usd))


async def month_spend(workspace_id: str) -> Decimal:
    async with worker_session(workspace_id) as s:
        v = await s.scalar(
            text(
                "SELECT COALESCE(sum(cost_usd), 0) FROM llm_usage WHERE workspace_id = :ws"
                " AND created_at >= date_trunc('month', now())"
            ),
            {"ws": workspace_id},
        )
    return Decimal(v)


def _model_ids(provider: LLMProvider) -> str:
    return f"{provider.name}:{provider.model_for('default')}:{provider.model_for('small')}"


async def scan(workspace_id: str, provider: LLMProvider) -> dict[str, int]:
    """Diff eligible bundles against graph_episode: new/changed -> pending; gone -> stale."""
    settings = await workspace_settings(workspace_id)
    bundles = await eligible_bundles(workspace_id, include_private=settings.include_private)
    model_ids, emb = _model_ids(provider), get_embedder().model
    by_root = {b.root_id: b for b in bundles}
    counts = {"pending": 0, "stale": 0}
    async with worker_session(workspace_id) as s:
        existing = {
            r[0]: (r[1], r[2])
            for r in await s.execute(
                text("SELECT root_document_id::text, input_hash, status FROM graph_episode")
            )
        }
        for root_id, b in by_root.items():
            h = b.input_hash(model_ids, emb)
            prev = existing.get(root_id)
            if prev and prev[0] is not None and bytes(prev[0]) == h and prev[1] == "done":
                continue
            if prev and prev[1] in ("pending", "processing"):
                continue
            await s.execute(
                text(
                    "INSERT INTO graph_episode (workspace_id, container_id, root_document_id,"
                    " group_id, status, reference_time, token_estimate, embedding_model)"
                    " VALUES (:ws, :cid, :rid, :g, 'pending', :ref, :tok, :emb)"
                    " ON CONFLICT (root_document_id, part) DO UPDATE SET status = 'pending',"
                    " reference_time = excluded.reference_time,"
                    " token_estimate = excluded.token_estimate, updated_at = now()"
                ),
                {
                    "ws": workspace_id,
                    "cid": b.container_id,
                    "rid": root_id,
                    "g": group_for(b.container_id),
                    "ref": b.reference_time,
                    "tok": sum(len(m.body) for m in b.members) // 4,
                    "emb": emb,
                },
            )
            counts["pending"] += 1
        # Roots that are no longer eligible (tombstoned, repo hidden, private opted out):
        # their facts disappear at once because post-checks require status='done'.
        gone = [rid for rid, (_, st) in existing.items() if rid not in by_root and st != "stale"]
        if gone:
            await s.execute(
                text(
                    "UPDATE graph_episode SET status = 'stale', updated_at = now()"
                    " WHERE root_document_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": gone},
            )
            counts["stale"] = len(gone)
    return counts


async def _record_usage(ctx: CostContext) -> None:
    if not ctx.records:
        return
    async with worker_session(ctx.workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO llm_usage (workspace_id, container_id, graph_episode_id, purpose,"
                " provider, model, prompt_name, input_tokens, cached_input_tokens,"
                " output_tokens, cost_usd) VALUES (:ws, :cid, :eid, :purpose, :prov, :model,"
                " :prompt, :inp, :cached, :out, :cost)"
            ),
            [
                {
                    "ws": ctx.workspace_id,
                    "cid": ctx.container_id,
                    "eid": ctx.graph_episode_id,
                    "purpose": ctx.purpose,
                    "prov": u.provider,
                    "model": u.model,
                    "prompt": prompt,
                    "inp": u.input_tokens,
                    "cached": u.cached_input_tokens,
                    "out": u.output_tokens,
                    "cost": u.cost_usd,
                }
                for prompt, u in ctx.records
            ],
        )


async def _participants(workspace_id: str, logins: set[str]) -> dict[str, tuple[str, str]]:
    """login -> (external_identity_id, person_id); creates person rows on first sight."""
    if not logins:
        return {}
    async with worker_session(workspace_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text, lower(login), person_id::text FROM external_identity"
                    " WHERE source = 'github' AND lower(login) = ANY(:logins)"
                ),
                {"logins": sorted(logins)},
            )
        ).all()
        out: dict[str, tuple[str, str]] = {}
        for ext_id, login, person_id in rows:
            if person_id is None:
                person_id = await s.scalar(
                    text(
                        "INSERT INTO person (workspace_id, display_name) VALUES (:ws, :n)"
                        " RETURNING id::text"
                    ),
                    {"ws": workspace_id, "n": login},
                )
                await s.execute(
                    text(
                        "UPDATE external_identity SET person_id = CAST(:p AS uuid) WHERE id = CAST(:e AS uuid)"
                    ),
                    {"p": person_id, "e": ext_id},
                )
            out[login] = (ext_id, person_id)
    return out


async def process_container(
    workspace_id: str, container_id: str, graphiti, provider: LLMProvider, limit: int = 20
) -> dict[str, int]:
    """Process pending episodes of one container in reference_time order."""
    settings = await workspace_settings(workspace_id)
    stats = {"done": 0, "failed": 0, "skipped_budget": 0}
    if not settings.enabled:
        return stats
    bundles = {
        b.root_id: b
        for b in await eligible_bundles(workspace_id, include_private=settings.include_private)
        if b.container_id == container_id
    }
    async with worker_session(workspace_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text, root_document_id::text, episode_uuid FROM graph_episode"
                    " WHERE container_id = :cid AND status IN ('pending', 'stale')"
                    " ORDER BY reference_time NULLS FIRST LIMIT :n"
                ),
                {"cid": container_id, "n": limit},
            )
        ).all()

    model_ids, emb = _model_ids(provider), get_embedder().model
    group = group_for(container_id)
    for episode_id, root_id, old_uuid in rows:
        bundle = bundles.get(root_id)
        remaining = settings.monthly_budget_usd - await month_spend(workspace_id)
        ctx = CostContext(workspace_id, container_id, episode_id, "graph_extract", remaining)
        token = current_cost.set(ctx)
        try:
            if bundle is None:
                # Stale and no longer eligible: remove its episode, keep the row hidden.
                if old_uuid:
                    await graphiti.remove_episode(old_uuid)
                await _set(workspace_id, episode_id, "stale", episode_uuid=None)
                continue
            ctx.check()
            await _set(workspace_id, episode_id, "processing", bump=True)
            if old_uuid:
                await graphiti.remove_episode(old_uuid)
            result = await graphiti.add_episode(
                name=bundle.name[:250],
                episode_body=render(bundle),
                source_description=f"github {bundle.kind} {bundle.url} tacit:{episode_id}",
                reference_time=bundle.reference_time or datetime.now(UTC),
                group_id=group,
                entity_types=ENTITY_TYPES,
                edge_types=EDGE_TYPES,
                edge_type_map=EDGE_TYPE_MAP,
                custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
            )
            await _link_people(workspace_id, container_id, group, bundle, result.nodes)
            await _finish(workspace_id, episode_id, bundle, result.episode.uuid, model_ids, emb)
            stats["done"] += 1
        except BudgetExceeded as exc:
            log.warning("Graph budget stop: %s", exc)
            await _set(workspace_id, episode_id, "skipped_budget", error=str(exc))
            stats["skipped_budget"] += 1
            await _record_usage(ctx)
            break
        except Exception as exc:
            log.exception("Graph episode %s failed", episode_id)
            await _set(workspace_id, episode_id, "failed", error=repr(exc)[:2000])
            stats["failed"] += 1
        finally:
            current_cost.reset(token)
        await _record_usage(ctx)
    return stats


async def _set(
    workspace_id: str,
    episode_id: str,
    status: str,
    *,
    bump: bool = False,
    error: str | None = None,
    episode_uuid: str | None | object = ...,
) -> None:
    sets = ["status = :st", "error = :err", "updated_at = now()"]
    if bump:
        sets.append("attempts = attempts + 1")
    params = {"st": status, "err": error, "id": episode_id}
    if episode_uuid is not ...:
        sets.append("episode_uuid = :uuid")
        params["uuid"] = episode_uuid
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(f"UPDATE graph_episode SET {', '.join(sets)} WHERE id = CAST(:id AS uuid)"), params
        )


async def _finish(
    workspace_id: str, episode_id: str, bundle: Bundle, episode_uuid: str, model_ids: str, emb: str
) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE graph_episode SET status = 'done', episode_uuid = :uuid, error = NULL,"
                " input_hash = :h, processed_at = now(), updated_at = now()"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"uuid": episode_uuid, "h": bundle.input_hash(model_ids, emb), "id": episode_id},
        )
        await s.execute(
            text("DELETE FROM graph_episode_document WHERE graph_episode_id = CAST(:id AS uuid)"),
            {"id": episode_id},
        )
        await s.execute(
            text(
                "INSERT INTO graph_episode_document (graph_episode_id, document_id, workspace_id,"
                " container_id) VALUES (CAST(:e AS uuid), CAST(:d AS uuid), :ws, CAST(:c AS uuid))"
            ),
            [
                {"e": episode_id, "d": m.document_id, "ws": workspace_id, "c": bundle.container_id}
                for m in bundle.members
            ],
        )


async def _link_people(
    workspace_id: str, container_id: str, group: str, bundle: Bundle, nodes
) -> None:
    """Exact @login match only; never fuzzy name matching (design §6)."""
    people = await _participants(workspace_id, bundle.participants)
    links = []
    for node in nodes:
        if "Person" not in (node.labels or []):
            continue
        login = node.name.strip().lstrip("@").lower()
        if login in people:
            ext_id, person_id = people[login]
            links.append(
                {
                    "ws": workspace_id,
                    "c": container_id,
                    "g": group,
                    "n": node.uuid,
                    "e": ext_id,
                    "p": person_id,
                }
            )
    if not links:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO graph_entity_link (workspace_id, container_id, group_id, node_uuid,"
                " entity_type, external_identity_id, person_id, method) VALUES (:ws,"
                " CAST(:c AS uuid), :g, :n, 'Person', CAST(:e AS uuid), CAST(:p AS uuid),"
                " 'login_exact') ON CONFLICT (group_id, node_uuid) DO NOTHING"
            ),
            links,
        )
