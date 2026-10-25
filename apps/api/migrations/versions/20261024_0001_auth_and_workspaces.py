"""Better Auth tables (users, sessions, workspaces, invites, JWKS), grants and RLS.

The CREATE TABLE statements come from `auth generate` for better-auth 1.7.12 with the
organization and jwt plugins. If Better Auth or its plugins add columns after an
upgrade, re-run generate and add a new migration.

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTH_TABLES = [
    "user",
    "session",
    "account",
    "verification",
    "organization",
    "member",
    "invitation",
    "jwks",
]
RLS_TABLES = ["user", "organization", "member", "invitation"]

BETTER_AUTH_SCHEMA = """
create table "user" ("id" text not null primary key, "name" text not null, "email" text not null unique, "emailVerified" boolean not null, "image" text, "createdAt" timestamptz default CURRENT_TIMESTAMP not null, "updatedAt" timestamptz default CURRENT_TIMESTAMP not null);
create table "session" ("id" text not null primary key, "expiresAt" timestamptz not null, "token" text not null unique, "createdAt" timestamptz default CURRENT_TIMESTAMP not null, "updatedAt" timestamptz not null, "ipAddress" text, "userAgent" text, "userId" text not null references "user" ("id") on delete cascade, "activeOrganizationId" text);
create table "account" ("id" text not null primary key, "accountId" text not null, "providerId" text not null, "userId" text not null references "user" ("id") on delete cascade, "accessToken" text, "refreshToken" text, "idToken" text, "accessTokenExpiresAt" timestamptz, "refreshTokenExpiresAt" timestamptz, "scope" text, "password" text, "createdAt" timestamptz default CURRENT_TIMESTAMP not null, "updatedAt" timestamptz not null);
create table "verification" ("id" text not null primary key, "identifier" text not null, "value" text not null, "expiresAt" timestamptz not null, "createdAt" timestamptz default CURRENT_TIMESTAMP not null, "updatedAt" timestamptz default CURRENT_TIMESTAMP not null);
create table "organization" ("id" text not null primary key, "name" text not null, "slug" text not null unique, "logo" text, "createdAt" timestamptz not null, "metadata" text);
create table "member" ("id" text not null primary key, "organizationId" text not null references "organization" ("id") on delete cascade, "userId" text not null references "user" ("id") on delete cascade, "role" text not null, "createdAt" timestamptz not null);
create table "invitation" ("id" text not null primary key, "organizationId" text not null references "organization" ("id") on delete cascade, "email" text not null, "role" text, "status" text not null, "expiresAt" timestamptz not null, "createdAt" timestamptz default CURRENT_TIMESTAMP not null, "inviterId" text not null references "user" ("id") on delete cascade);
create table "jwks" ("id" text not null primary key, "publicKey" text not null, "privateKey" text not null, "createdAt" timestamptz not null, "expiresAt" timestamptz, "alg" text, "crv" text);
create index "session_userId_idx" on "session" ("userId");
create index "account_userId_idx" on "account" ("userId");
create index "verification_identifier_idx" on "verification" ("identifier");
create index "member_organizationId_idx" on "member" ("organizationId");
create index "member_userId_idx" on "member" ("userId");
create index "invitation_organizationId_idx" on "invitation" ("organizationId");
create index "invitation_email_idx" on "invitation" ("email");
"""

# Helpers used by RLS policies. SECURITY DEFINER functions run as tacit_migrator
# (the owner), so they can read `member` without recursing into its own policy.
RLS_FUNCTIONS = """
CREATE SCHEMA app;
GRANT USAGE ON SCHEMA app TO tacit_api;

CREATE FUNCTION app.current_user_id() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '') $$;

CREATE FUNCTION app.current_workspace_id() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.workspace_id', true), '') $$;

CREATE FUNCTION app.is_member(ws text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM member WHERE "organizationId" = ws AND "userId" = app.current_user_id()
  )
$$;

CREATE FUNCTION app.shares_workspace(other_user text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM member mine JOIN member theirs ON theirs."organizationId" = mine."organizationId"
    WHERE mine."userId" = app.current_user_id() AND theirs."userId" = other_user
  )
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO tacit_api;
"""


def upgrade() -> None:
    op.execute(BETTER_AUTH_SCHEMA)
    op.execute(RLS_FUNCTIONS)

    # Better Auth (tacit_auth) needs full access to its own tables and nothing else.
    tables = ", ".join(f'"{t}"' for t in AUTH_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO tacit_auth")

    # The API only reads identity/workspace data, and never sessions, accounts or keys.
    op.execute('GRANT SELECT ON "organization", "member", "invitation" TO tacit_api')
    op.execute('GRANT SELECT ("id", "name", "email", "image", "createdAt") ON "user" TO tacit_api')

    for t in RLS_TABLES:
        op.execute(f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY')
        # Better Auth must look up any user (e.g. by email on sign-in); grants already
        # limit it to auth tables.
        op.execute(
            f'CREATE POLICY "{t}_auth_service" ON "{t}" FOR ALL TO tacit_auth '
            "USING (true) WITH CHECK (true)"
        )

    op.execute(
        'CREATE POLICY "organization_members_read" ON "organization" FOR SELECT TO tacit_api '
        'USING (app.is_member("id"))'
    )
    op.execute(
        'CREATE POLICY "member_members_read" ON "member" FOR SELECT TO tacit_api '
        'USING (app.is_member("organizationId"))'
    )
    op.execute(
        'CREATE POLICY "invitation_members_read" ON "invitation" FOR SELECT TO tacit_api '
        'USING (app.is_member("organizationId"))'
    )
    op.execute(
        'CREATE POLICY "user_self_or_teammate_read" ON "user" FOR SELECT TO tacit_api '
        'USING ("id" = app.current_user_id() OR app.shares_workspace("id"))'
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA app CASCADE")
    for t in reversed(AUTH_TABLES):
        op.execute(f'DROP TABLE "{t}" CASCADE')
