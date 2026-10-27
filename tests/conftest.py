"""Test setup: isolated config dir, no keychain, fake embedder, throwaway Neo4j.

Neo4j tests need the test database:  docker compose --profile test up -d --wait neo4j-test
It listens on bolt://localhost:7688 and is WIPED by the tests. To run several checkouts
at once (agent worktrees), give each its own container and port:

    docker run -d --rm --name tacit-test-$PORT -p $PORT:7687 \
      -e NEO4J_AUTH=neo4j/tacit-test-password --tmpfs /data neo4j:5.26
    TACIT_TEST_NEO4J_URI=bolt://localhost:$PORT uv run pytest
"""

import os
import tempfile

import pytest

_cfg = tempfile.mkdtemp(prefix="tacit-test-cfg-")
os.environ["TACIT_CONFIG_DIR"] = _cfg
os.environ["TACIT_NO_KEYRING"] = "1"
os.environ["TACIT_EMBEDDINGS__PROVIDER"] = "fake"
os.environ["TACIT_IDENTITY__EMAIL"] = "tester@example.com"
TEST_URI = os.environ.get("TACIT_TEST_NEO4J_URI", "bolt://localhost:7688")
os.environ["TACIT_GRAPH__URI"] = TEST_URI
os.environ["TACIT_SECRET_NEO4J_PASSWORD"] = os.environ.get(
    "TACIT_TEST_NEO4J_PASSWORD", "tacit-test-password"
)
for var in ("GITHUB_TOKEN", "GH_TOKEN", "SLACK_TOKEN", "NOTION_TOKEN", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)


@pytest.fixture
async def store():
    """A Store on the test database, wiped before each test, schema in place."""
    from tacit.config import get_config
    from tacit.store import Store
    from tacit.store.schema import ensure_schema

    get_config.cache_clear()
    s = Store.from_config()
    try:
        await s.driver.verify_connectivity()
    except Exception as exc:  # pragma: no cover - environment
        await s.close()
        pytest.skip(f"Neo4j test database not reachable at {TEST_URI}: {exc}")
    await s._execute("MATCH (n) DETACH DELETE n")
    await ensure_schema(s, "fake-hash-384")
    yield s
    await s.close()
