"""Leak tests for the Notion tables (migration 0008) and token encryption."""

import psycopg
import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tacit_api.connectors.crypto import TokenCipher
from tacit_api.connectors.notion.tokens import decrypt_token, encrypt_token
from tacit_api.connectors.slack.tokens import encrypt_token as slack_encrypt
from tacit_api.db import dispose_engine, rls_session, worker_session

from .conftest import MIGRATOR_URL, Seed

TOKEN = "ntn_test_secret_token"
CONN_ACME = "00000000-0000-0000-0000-0000000d0001"
CONN_GLOBEX = "00000000-0000-0000-0000-0000000d0002"
CONT_ACME = "00000000-0000-0000-0000-0000000d1001"


@pytest.fixture(scope="module")
def notion_rows(seed: Seed) -> Seed:
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for ws, user, conn_id, bot in [
            (seed.acme, seed.alice, CONN_ACME, "bot-rls-1"),
            (seed.globex, seed.carol, CONN_GLOBEX, "bot-rls-2"),
        ]:
            conn.execute(
                "INSERT INTO notion_connection (id, workspace_id, user_id, notion_workspace_id,"
                " notion_user_id, bot_id, access_token_encrypted, refresh_token_encrypted)"
                " VALUES (%s, %s, %s, 'NWRLS', 'nu1', %s, %s, %s)",
                (
                    conn_id,
                    ws,
                    user,
                    bot,
                    encrypt_token(TOKEN, bot, "access"),
                    encrypt_token("refresh", bot, "refresh"),
                ),
            )
            conn.execute(
                "INSERT INTO notion_sync_cursor (connection_id, workspace_id) VALUES (%s, %s)",
                (conn_id, ws),
            )
        conn.execute(
            "INSERT INTO source_container (id, workspace_id, source, external_id, name,"
            " visibility) VALUES (%s, %s, 'notion', %s, 'Roadmap', 'private')",
            (CONT_ACME, seed.acme, f"{CONN_ACME}:root"),
        )
        conn.execute(
            "INSERT INTO notion_tree (container_id, workspace_id, connection_id, root_id,"
            " root_type, title) VALUES (%s, %s, %s, 'root', 'page', 'Roadmap')",
            (CONT_ACME, seed.acme, CONN_ACME),
        )
    return seed


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


async def test_api_reads_connection_metadata_in_own_workspace_only(notion_rows):
    s = notion_rows
    async with rls_session(s.alice, s.acme) as sess:
        bots = set((await sess.execute(text("SELECT bot_id FROM notion_connection"))).scalars())
        trees = set((await sess.execute(text("SELECT title FROM notion_tree"))).scalars())
    assert "bot-rls-1" in bots and "bot-rls-2" not in bots and "Roadmap" in trees
    async with rls_session(s.carol, s.acme) as sess:  # forged workspace
        assert (await sess.execute(text("SELECT 1 FROM notion_connection"))).all() == []
        assert (await sess.execute(text("SELECT 1 FROM notion_tree"))).all() == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT access_token_encrypted FROM notion_connection",
        "SELECT refresh_token_encrypted FROM notion_connection",
        "SELECT * FROM notion_connection",
        "SELECT 1 FROM notion_page",
        "SELECT 1 FROM notion_sync_cursor",
        "UPDATE notion_tree SET workspace_visible = true",
        "UPDATE notion_connection SET revoked_at = now()",
        "SELECT app.notion_connections_for('NWRLS')",
    ],
    ids=[
        "access-token",
        "refresh-token",
        "star",
        "pages",
        "cursor",
        "mark-visible",
        "write-connection",
        "worker-helper",
    ],
)
async def test_api_role_cannot_read_tokens_or_write(notion_rows, sql):
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with rls_session(notion_rows.alice, notion_rows.acme) as sess:
            await sess.execute(text(sql))


async def test_worker_scoped_to_selected_workspace(notion_rows):
    s = notion_rows
    async with worker_session(s.acme) as sess:
        bots = set((await sess.execute(text("SELECT bot_id FROM notion_connection"))).scalars())
        blob = await sess.scalar(
            text("SELECT access_token_encrypted FROM notion_connection WHERE id = :id"),
            {"id": CONN_ACME},
        )
    assert "bot-rls-1" in bots and "bot-rls-2" not in bots
    assert decrypt_token(blob, "bot-rls-1", "access") == TOKEN
    async with worker_session() as sess:
        assert (await sess.execute(text("SELECT 1 FROM notion_connection"))).all() == []
        assert (await sess.execute(text("SELECT 1 FROM notion_sync_cursor"))).all() == []


async def test_worker_cannot_write_other_workspace(notion_rows):
    with pytest.raises(DBAPIError, match="row-level security"):
        async with worker_session(notion_rows.acme) as sess:
            await sess.execute(
                text(
                    "INSERT INTO notion_connection (workspace_id, user_id, notion_workspace_id,"
                    " notion_user_id, bot_id, access_token_encrypted)"
                    " VALUES (:ws, :u, 'X', 'x', 'bot-x', '\\x00')"
                ),
                {"ws": notion_rows.globex, "u": notion_rows.carol},
            )


async def test_routing_helpers_list_live_connections(notion_rows):
    async with worker_session() as sess:
        routed = {
            tuple(r)
            for r in await sess.execute(
                text("SELECT workspace_id, bot_id FROM app.notion_connections_for('NWRLS')")
            )
        }
        listed = {
            str(r[1])
            for r in await sess.execute(text("SELECT * FROM app.list_notion_connections()"))
        }
    assert routed >= {("org_acme", "bot-rls-1"), ("org_globex", "bot-rls-2")}
    assert {CONN_ACME, CONN_GLOBEX} <= listed


def test_token_encryption_binds_bot_and_kind():
    blob = encrypt_token(TOKEN, "bot1", "access")
    assert TOKEN.encode() not in blob
    assert decrypt_token(blob, "bot1", "access") == TOKEN
    with pytest.raises(InvalidTag):
        decrypt_token(blob, "bot2", "access")  # copied onto another connection
    with pytest.raises(InvalidTag):
        decrypt_token(blob, "bot1", "refresh")  # swapped into the other column
    # Separate keys per connector: a Slack ciphertext never decrypts as a Notion one.
    with pytest.raises(InvalidTag):
        TokenCipher(b"tacit:notion-token:v1").decrypt(slack_encrypt(TOKEN, "T1"), "T1")
