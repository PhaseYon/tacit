"""Search index: document chunks with embeddings (pgvector) and full-text (tsvector).

Chunks carry workspace_id and container_id so the same RLS applies as on documents:
the workspace policy plus the RESTRICTIVE source-permission policy.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from migrations.rls import enable_workspace_rls

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both supported backends produce 384-dim vectors (bge-small natively, OpenAI
# text-embedding-3-small via its `dimensions` parameter). Changing this needs a
# migration and a re-embed.
EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE document_chunk (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          workspace_id text NOT NULL,
          container_id uuid NOT NULL,
          document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
          chunk_index integer NOT NULL,
          title text,
          content text NOT NULL,
          token_count integer NOT NULL,
          embedding vector({EMBEDDING_DIM}) NOT NULL,
          embedding_model text NOT NULL,
          tsv tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A')
            || setweight(to_tsvector('english', content), 'B')
          ) STORED,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (document_id, chunk_index),
          FOREIGN KEY (workspace_id, container_id)
            REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
        );
        CREATE INDEX document_chunk_embedding_idx ON document_chunk
          USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX document_chunk_tsv_idx ON document_chunk USING gin (tsv);
        CREATE INDEX document_chunk_document_idx ON document_chunk (document_id);
        CREATE INDEX document_chunk_workspace_idx ON document_chunk (workspace_id, container_id);
        """
    )
    enable_workspace_rls("document_chunk")
    op.execute(
        """
        CREATE POLICY document_chunk_container_acl ON document_chunk
        AS RESTRICTIVE FOR SELECT TO tacit_api
        USING (app.can_read_container(container_id))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON document_chunk TO tacit_worker")
    op.execute("GRANT SELECT ON document_chunk TO tacit_api")
    # The scheduled indexer needs to know which workspaces have work, nothing more.
    op.execute(
        """
        CREATE FUNCTION app.workspaces_pending_index() RETURNS SETOF text
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
          SELECT DISTINCT workspace_id FROM source_document
          WHERE (processed_hash IS DISTINCT FROM content_hash AND deleted_at IS NULL)
          UNION
          SELECT DISTINCT c.workspace_id FROM document_chunk c
          JOIN source_document d ON d.id = c.document_id WHERE d.deleted_at IS NOT NULL
        $$;
        REVOKE ALL ON FUNCTION app.workspaces_pending_index() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.workspaces_pending_index() TO tacit_worker;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.workspaces_pending_index()")
    op.execute("DROP TABLE document_chunk")
