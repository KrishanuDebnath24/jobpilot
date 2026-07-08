"""Phase 7: embeddings for semantic search.

Adds ``embedding vector(1024)`` to ``analyses`` and ``suggestions`` (jobs + resumes
already have the column from Phase 1) so all four content kinds can be embedded and
searched. Adds HNSW cosine-distance indexes on the two high-volume searchable tables
(``jobs`` and ``analyses``) so approximate nearest-neighbour search stays fast as the
pool grows; resumes/suggestions are low-volume (sequential scan is fine). The vectors
live on user-owned tables, so ``delete_user_data`` already wipes them via cascade.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.add_column("analyses", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("suggestions", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))

    # HNSW indexes for cosine similarity. `IF NOT EXISTS` keeps re-runs idempotent.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_jobs_embedding_hnsw ON jobs "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analyses_embedding_hnsw ON analyses "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_analyses_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_jobs_embedding_hnsw")
    op.drop_column("suggestions", "embedding")
    op.drop_column("analyses", "embedding")
