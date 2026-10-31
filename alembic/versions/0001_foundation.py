"""Owner, grants and read-only catalog foundation.

Revision ID: 0001_foundation
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("owners", sa.Column("id", sa.String(36), primary_key=True))
    op.create_table(
        "client_grants",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("audience", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.Integer(), nullable=True),
        sa.CheckConstraint("length(token_hash)=64"),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + 604800"
        ),
        sa.CheckConstraint("channel IN ('agent','human_adapter')"),
    )
    op.create_index("ix_client_grants_owner_id", "client_grants", ["owner_id"])
    op.create_table(
        "service_state",
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
        ),
        sa.Column("catalog_revision", sa.Integer(), nullable=False),
        sa.Column("history_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("catalog_revision>=0 AND history_revision>=0"),
    )
    op.create_table(
        "garments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("availability", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("annotation_fields", sa.JSON(), nullable=False),
        sa.Column("annotation_review_state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision>=1"),
        sa.CheckConstraint("availability IN ('available','unavailable','unknown')"),
        sa.CheckConstraint("state IN ('active','deleted')"),
        sa.CheckConstraint(
            "annotation_review_state IN ('initial','needs_review','published')"
        ),
    )
    op.create_index(
        "ix_garments_owner_state_id", "garments", ["owner_id", "state", "id"]
    )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade is not supported; restore a verified compatible backup"
    )
