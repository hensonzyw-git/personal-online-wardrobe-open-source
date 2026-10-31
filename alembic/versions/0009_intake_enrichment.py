"""Widen intake image roles and add product enrichment storage.

Unreleased single-head candidate; no historical channel tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_intake_enrichment"
down_revision = "0008_garment_intake"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("intake_images", recreate="always") as batch:
        batch.alter_column("role", existing_type=sa.String(10), type_=sa.String(20))
    op.execute(
        """CREATE TABLE intake_product_enrichments (
        task_id VARCHAR(36) NOT NULL PRIMARY KEY REFERENCES garment_intakes(id),
        schema_version VARCHAR(60) NOT NULL,
        input_fingerprint VARCHAR(64) NOT NULL,
        state VARCHAR(20) NOT NULL,
        attempts_reserved INTEGER NOT NULL CHECK (attempts_reserved BETWEEN 0 AND 2),
        generation INTEGER NOT NULL,
        payload JSON NOT NULL,
        evidence_digest VARCHAR(64),
        updated_at INTEGER NOT NULL,
        reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes>=0)
    )"""
    )


def downgrade():
    raise RuntimeError("Restore a verified backup; do not discard intake enrichments")
