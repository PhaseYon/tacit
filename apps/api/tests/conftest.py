"""Test fixtures: a fresh `tacit_test` database with the real roles, grants and RLS.

Needs the dev Postgres from docker-compose running (it provides the roles).
"""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jwt
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

SUPERUSER_URL = os.environ.get(
    "TEST_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
# Override to run several checkouts in parallel against one Postgres.
TEST_DB = os.environ.get("TACIT_TEST_DB", "tacit_test")
API_DIR = Path(__file__).resolve().parents[1]

ISSUER = "http://test-issuer"
AUDIENCE = "tacit-api"

# Point the app at the test DB before anything reads settings.
os.environ["API_DATABASE_URL"] = f"postgresql+asyncpg://tacit_api:api@localhost:5432/{TEST_DB}"
os.environ["WORKER_DATABASE_URL"] = (
    f"postgresql+asyncpg://tacit_worker:worker@localhost:5432/{TEST_DB}"
)
# Deterministic hashing embedder: no model download, same vectors everywhere.
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["AUTH_JWT_ISSUER"] = ISSUER
os.environ["AUTH_JWT_AUDIENCE"] = AUDIENCE

MIGRATOR_URL = f"postgresql://tacit_migrator:migrator@localhost:5432/{TEST_DB}"


def _create_test_db() -> None:
    with psycopg.connect(SUPERUSER_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
        conn.execute(f"REVOKE ALL ON DATABASE {TEST_DB} FROM PUBLIC")
        conn.execute(
            f"GRANT CONNECT ON DATABASE {TEST_DB}"
            " TO tacit_migrator, tacit_auth, tacit_api, tacit_worker"
        )
        conn.execute(f"GRANT CREATE ON DATABASE {TEST_DB} TO tacit_migrator")
    # Same setup as infra/postgres/init/01-roles.sh, minus role creation.
    with psycopg.connect(SUPERUSER_URL.rsplit("/", 1)[0] + f"/{TEST_DB}", autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("ALTER SCHEMA public OWNER TO tacit_migrator")
        conn.execute("GRANT USAGE ON SCHEMA public TO tacit_auth, tacit_api, tacit_worker")


def _migrate() -> None:
    os.environ["ALEMBIC_DATABASE_URL"] = MIGRATOR_URL.replace(
        "postgresql://", "postgresql+psycopg://"
    )
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    command.upgrade(cfg, "head")


@dataclass(frozen=True)
class Seed:
    alice: str = "u_alice"
    bob: str = "u_bob"
    carol: str = "u_carol"
    acme: str = "org_acme"  # alice (owner), bob (member)
    globex: str = "org_globex"  # carol (owner)


def _seed(seed: Seed) -> None:
    now = datetime.now(UTC)
    # The table owner bypasses RLS, which is what we want for seeding.
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for uid, name in [(seed.alice, "Alice"), (seed.bob, "Bob"), (seed.carol, "Carol")]:
            conn.execute(
                'INSERT INTO "user" (id, name, email, "emailVerified") VALUES (%s, %s, %s, true)',
                (uid, name, f"{name.lower()}@example.com"),
            )
        for oid, name in [(seed.acme, "Acme"), (seed.globex, "Globex")]:
            conn.execute(
                'INSERT INTO organization (id, name, slug, "createdAt") VALUES (%s, %s, %s, %s)',
                (oid, name, name.lower(), now),
            )
        for mid, oid, uid, role in [
            ("m1", seed.acme, seed.alice, "owner"),
            ("m2", seed.acme, seed.bob, "member"),
            ("m3", seed.globex, seed.carol, "owner"),
        ]:
            conn.execute(
                'INSERT INTO member (id, "organizationId", "userId", role, "createdAt") '
                "VALUES (%s, %s, %s, %s, %s)",
                (mid, oid, uid, role, now),
            )
        conn.execute(
            'INSERT INTO invitation (id, "organizationId", email, role, status, "expiresAt", '
            '"inviterId") VALUES (%s, %s, %s, %s, %s, %s, %s)',
            ("inv1", seed.globex, "dave@example.com", "member", "pending", now, seed.carol),
        )


@pytest.fixture(scope="session")
def seed() -> Seed:
    _create_test_db()
    _migrate()
    s = Seed()
    _seed(s)
    return s


@pytest.fixture(scope="session")
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture(scope="session", autouse=True)
def fake_jwks(signing_key: Ed25519PrivateKey):
    """Swap the JWKS fetch for our test key."""
    from tacit_api import auth
    from tacit_api.config import get_settings

    get_settings.cache_clear()

    class _Key:
        key = signing_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, _token: str) -> _Key:
            return _Key()

    original = auth.get_jwk_client
    auth.get_jwk_client = lambda: _Client()  # type: ignore[assignment]
    yield
    auth.get_jwk_client = original


@pytest.fixture(scope="session")
def make_token(signing_key: Ed25519PrivateKey):
    def _make(sub: str, *, aud: str = AUDIENCE, iss: str = ISSUER, ttl: int = 900) -> str:
        now = int(time.time())
        claims = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + ttl}
        return jwt.encode(claims, signing_key, algorithm="EdDSA")

    return _make


@pytest.fixture
async def client(seed: Seed):
    from tacit_api.db import dispose_engine
    from tacit_api.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()
