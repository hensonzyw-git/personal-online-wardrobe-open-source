"""First catalog source bindings and complete, separate product evidence."""

import sqlalchemy as sa

from alembic import op

revision = "0005_catalog_import"
down_revision = "0004_product_drafts"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "catalog_imports",
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
        ),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("parent_batch_id", sa.String(36), nullable=False),
        sa.Column("receipt", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.UniqueConstraint("owner_id", "parent_batch_id"),
    )
    op.create_table(
        "catalog_sources",
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
        ),
        sa.Column("source_id", sa.String(36), primary_key=True),
        sa.Column(
            "garment_id",
            sa.String(36),
            sa.ForeignKey("garments.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "asset_id", sa.String(36), sa.ForeignKey("assets.id"), nullable=False
        ),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("batch_digest", sa.String(64), nullable=False),
        sa.Column("source_metadata", sa.JSON, nullable=False),
    )
    op.create_table(
        "product_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("region", sa.String(2), nullable=False),
        sa.Column("global_spu_id", sa.String(20), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON, nullable=False),
        sa.Column("recorded_at", sa.String(60), nullable=False),
        sa.UniqueConstraint(
            "owner_id", "provider", "region", "global_spu_id", "digest"
        ),
    )
    op.create_index("ix_product_snapshots_owner_id", "product_snapshots", ["owner_id"])
    op.create_table(
        "garment_product_evidence",
        sa.Column(
            "garment_id", sa.String(36), sa.ForeignKey("garments.id"), primary_key=True
        ),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("snapshot_ids", sa.JSON, nullable=False),
    )
    op.create_index(
        "ix_garment_product_evidence_owner_id", "garment_product_evidence", ["owner_id"]
    )


def downgrade():
    raise RuntimeError(
        "Restore a compatible backup instead of discarding private catalog data"
    )
