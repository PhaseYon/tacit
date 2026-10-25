"""Leak tests for the Slack tables (migration 0007) and bot-token encryption."""

import psycopg
import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tacit_api.connectors.slack.tokens import decrypt_token, encrypt_token
from tacit_api.db import dispose_engine, rls_session, worker_session

from .conftest import MIGRATOR_URL, Seed

TOKEN = "xoxb-test-secret-token"
CH_ACME = "00000000-0000-0000-0000-0000000c0001"
CH_GLOBEX = "00000000-0000-0000-0000-0000000c0002"


@pytest.fixture(scope="module")
def slack_rows(seed: Seed) -> Seed:
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for ws, team, cid in [(seed.acme, "TRLS1", CH_ACME), (seed.globex, "TRLS2", CH_GLOBEX)]:
            conn.execute(
                "INSERT INTO slack_installation (workspace_id, team_id, team_name, bot_user_id,"
                " bot_token_encrypted) VALUES (%s, %s, %s, 'UBOT', %s)",
                (ws, team, team, encrypt_token(TOKEN, team)),
            )
            conn.execute(
                "INSERT INTO source_container (id, workspace_id, source, external_id, name,"
                " visibility) VALUES (%s, %s, 'slack', %s, '#general', 'internal')",
                (cid, ws, f"{team}:C1"),
            )
            conn.execute(
                "INSERT INTO slack_channel (container_id, workspace_id, team_id, channel_id,"
                " name, is_private) VALUES (%s, %s, %s, 'C1', '#general', false)",
                (cid, ws, team),
            )
            conn.execute(
                "INSERT INTO slack_sync_cursor (container_id, workspace_id, resource, oldest_ts)"
                " VALUES (%s, %s, 'history', '1.0')",
                (cid, ws),
            )
        conn.execute(
            "INSERT INTO slack_installation (workspace_id, team_id, team_name, bot_user_id,"
            " revoked_at) VALUES (%s, 'TREVOKED', 'old', 'UBOT', now())",
            (seed.acme,),
        )
    return seed


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


async def test_api_reads_installation_metadata_in_own_workspace_only(slack_rows):
    s = slack_rows
    async with rls_session(s.alice, s.acme) as sess:
        teams = set((await sess.execute(text("SELECT team_id FROM slack_installation"))).scalars())
        chans = set((await sess.execute(text("SELECT name FROM slack_channel"))).scalars())
    # Other modules add Acme teams too; what matters is that Globex's never shows.
    assert {"TRLS1", "TREVOKED"} <= teams and "TRLS2" not in teams and "#general" in chans
    async with rls_session(s.carol, s.acme) as sess:  # forged workspace
        assert (await sess.execute(text("SELECT 1 FROM slack_installation"))).all() == []
        assert (await sess.execute(text("SELECT 1 FROM slack_channel"))).all() == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT bot_token_encrypted FROM slack_installation",
        "SELECT * FROM slack_installation",
        "SELECT 1 FROM slack_sync_cursor",
        "UPDATE slack_channel SET name = 'x'",
        "UPDATE slack_installation SET team_name = 'x'",
    ],
    ids=["token-column", "star", "cursor", "write-channel", "write-installation"],
)
async def test_api_role_cannot_read_token_or_write(slack_rows, sql):
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with rls_session(slack_rows.alice, slack_rows.acme) as sess:
            await sess.execute(text(sql))


async def test_worker_scoped_to_selected_workspace(slack_rows):
    s = slack_rows
    async with worker_session(s.acme) as sess:
        teams = set((await sess.execute(text("SELECT team_id FROM slack_installation"))).scalars())
        cursors = (
            await sess.execute(text("SELECT container_id::text FROM slack_sync_cursor"))
        ).all()
        blob = await sess.scalar(
            text("SELECT bot_token_encrypted FROM slack_installation WHERE team_id = 'TRLS1'")
        )
    assert {"TRLS1", "TREVOKED"} <= teams and "TRLS2" not in teams
    assert CH_ACME in {c[0] for c in cursors} and CH_GLOBEX not in {c[0] for c in cursors}
    assert decrypt_token(blob, "TRLS1") == TOKEN
    async with worker_session() as sess:
        assert (await sess.execute(text("SELECT 1 FROM slack_installation"))).all() == []
        assert (await sess.execute(text("SELECT 1 FROM slack_channel"))).all() == []


async def test_worker_cannot_write_other_workspace(slack_rows):
    with pytest.raises(DBAPIError, match="row-level security"):
        async with worker_session(slack_rows.acme) as sess:
            await sess.execute(
                text(
                    "INSERT INTO slack_installation (workspace_id, team_id, team_name,"
                    " bot_user_id, bot_token_encrypted) VALUES (:ws, 'TX', 'x', 'U', '\\x00')"
                ),
                {"ws": slack_rows.globex},
            )


async def test_team_lookup_skips_revoked(slack_rows):
    async with worker_session() as sess:
        got = await sess.scalar(text("SELECT app.workspace_for_slack_team('TRLS1')"))
        revoked = await sess.scalar(text("SELECT app.workspace_for_slack_team('TREVOKED')"))
        listed = set(
            (await sess.execute(text("SELECT * FROM app.list_slack_installations()"))).all()
        )
    assert got == slack_rows.acme and revoked is None
    assert {("org_acme", "TRLS1"), ("org_globex", "TRLS2")} <= {tuple(r) for r in listed}
    assert not any(r[1] == "TREVOKED" for r in listed)


async def test_api_role_cannot_call_worker_helpers(slack_rows):
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with rls_session(slack_rows.alice, slack_rows.acme) as sess:
            await sess.execute(text("SELECT app.workspace_for_slack_team('TRLS1')"))


def test_token_encryption_binds_team_and_hides_plaintext():
    blob = encrypt_token(TOKEN, "T1")
    assert TOKEN.encode() not in blob
    assert blob != encrypt_token(TOKEN, "T1")  # random nonce
    assert decrypt_token(blob, "T1") == TOKEN
    with pytest.raises(InvalidTag):
        decrypt_token(blob, "T2")  # copied onto another team's row
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(InvalidTag):
        decrypt_token(tampered, "T1")
