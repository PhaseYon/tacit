"""Deterministic stand-in for the LLM Graphiti calls during extraction (no network, no $).

It answers every response schema with a minimal valid object, and does real (if naive)
work for entity and edge extraction: @logins become Person entities, capitalised words
from a small vocabulary become Technology/Component entities, and a sentence like
"switch from X to Y" yields facts. Good enough to exercise the pipeline, group isolation
and supersession against a real Neo4j.
"""

import re
from typing import Any

from pydantic import BaseModel

TECH = ["Elasticsearch", "Postgres", "Stripe", "Braintree", "Redis", "Kafka", "MongoDB", "React"]
COMPONENTS = ["search", "billing", "api", "web"]


def _episode_text(messages) -> str:
    text = "\n".join(m.content for m in messages)
    m = re.search(r"<CURRENT MESSAGE>(.*?)</CURRENT MESSAGE>", text, re.S) or re.search(
        r"<EPISODE_CONTENT>(.*?)</EPISODE_CONTENT>", text, re.S
    )
    return m.group(1) if m else text


def _type_ids(messages) -> dict[str, int]:
    """Graphiti lists entity types with integer ids in the prompt."""
    text = "\n".join(m.content for m in messages)
    ids: dict[str, int] = {}
    pattern = r"""['"]entity_type_id['"]:\s*(\d+),\s*['"]entity_type_name['"]:\s*['"](\w+)['"]"""
    for tid, name in re.findall(pattern, text):
        ids[name] = int(tid)
    return ids


def _entities(messages) -> list[dict[str, Any]]:
    body = _episode_text(messages)
    ids = _type_ids(messages)
    out: list[dict[str, Any]] = []
    for login in dict.fromkeys(re.findall(r"@([A-Za-z0-9-]+)", body)):
        out.append({"name": login, "entity_type_id": ids.get("Person", 0), "episode_indices": [0]})
    for tech in TECH:
        if tech.lower() in body.lower():
            out.append(
                {"name": tech, "entity_type_id": ids.get("Technology", 0), "episode_indices": [0]}
            )
    for comp in COMPONENTS:
        if re.search(rf"\b{comp}\b", body, re.I):
            out.append(
                {"name": comp, "entity_type_id": ids.get("Component", 0), "episode_indices": [0]}
            )
    return out


def _edges(messages) -> list[dict[str, Any]]:
    body = _episode_text(messages)
    edges = []
    comps = [c for c in COMPONENTS if re.search(rf"\b{c}\b", body, re.I)] or ["api"]
    for m in re.finditer(r"from (\w+) to (\w+)", body, re.I):
        old, new = m.group(1), m.group(2)
        edges.append(
            {
                "source_entity_name": comps[0],
                "target_entity_name": new,
                "relation_type": "USES",
                "fact": f"{comps[0]} uses {new} instead of {old}",
                "valid_at": None,
                "invalid_at": None,
            }
        )
    for m in re.finditer(r"adopt (\w+)", body, re.I):
        edges.append(
            {
                "source_entity_name": comps[0],
                "target_entity_name": m.group(1),
                "relation_type": "USES",
                "fact": f"{comps[0]} uses {m.group(1)}",
                "valid_at": None,
                "invalid_at": None,
            }
        )
    return edges


def _minimal(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return _minimal(defs[schema["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        return None if len(options) < len(schema["anyOf"]) else _minimal(options[0], defs)
    if "default" in schema:
        return schema["default"]
    t = schema.get("type")
    if t == "object":
        return {
            k: _minimal(v, defs)
            for k, v in schema.get("properties", {}).items()
            if k in schema.get("required", [])
        }
    if t == "array":
        return []
    if t == "string":
        return schema.get("enum", [""])[0]
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None


def respond(messages, schema: type[BaseModel] | None) -> dict[str, Any]:
    if schema is None:
        return {}
    name = schema.__name__
    json_schema = schema.model_json_schema()
    data = _minimal(json_schema, json_schema.get("$defs", {}))
    if name == "ExtractedEntities":
        data = {"extracted_entities": _entities(messages)}
    elif name == "ExtractedEdges":
        data = {"edges": _edges(messages)}
    elif name == "NodeResolutions":
        data = {"entity_resolutions": []}
    return schema.model_validate(data).model_dump(mode="json")
