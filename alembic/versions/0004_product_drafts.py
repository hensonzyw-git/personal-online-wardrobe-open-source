"""Private product evidence drafts and confirmed garment product references."""

import sqlalchemy as sa

from alembic import op

revision = "0004_product_drafts"
down_revision = "0003_recommendations"
branch_labels = depends_on = None


def upgrade():
    op.add_column("garments", sa.Column("product_reference", sa.JSON(), nullable=True))
    op.create_table(
        "product_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column(
            "published_garment_id",
            sa.String(36),
            sa.ForeignKey("garments.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.Integer, nullable=False),
        sa.CheckConstraint("revision>=1"),
        sa.CheckConstraint("state IN ('draft','published')"),
        sa.CheckConstraint("expires_at>created_at"),
    )
    op.create_index("ix_product_drafts_owner_id", "product_drafts", ["owner_id"])


def downgrade():
    raise RuntimeError(
        "Restore a verified compatible backup instead of dropping private product evidence"
    )
