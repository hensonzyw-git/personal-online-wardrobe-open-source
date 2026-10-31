"""Persist provider-owned agent operations and immutable confirmations."""

import sqlalchemy as sa

from alembic import op

revision = "0010_agent_facade"
down_revision = "0009_intake_enrichment"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column(
            "grant_id",
            sa.String(64),
            sa.ForeignKey("client_grants.token_hash"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("garment_intakes.id")),
        sa.Column(
            "recommendation_id", sa.String(36), sa.ForeignKey("recommendations.id")
        ),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("owner_id", "client_id", "kind", "request_key"),
        sa.CheckConstraint("kind IN ('intake','recommendation')"),
        sa.CheckConstraint(
            "state IN ('receiving','processing','needs_input','needs_confirmation','succeeded','failed','cancelled','unknown')"
        ),
    )
    op.create_index(
        "ix_agent_operations_dispatch", "agent_operations", ["state", "lease_until"]
    )
    op.create_table(
        "agent_confirmations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey("agent_operations.id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("operation_id", "revision"),
        sa.CheckConstraint("revision>=1"),
    )


def downgrade():
    raise RuntimeError("Restore a verified backup; preserve agent operation receipts")
