"""Durable recommendations and append-only history amendment evidence."""

import sqlalchemy as sa

from alembic import op

revision = "0003_recommendations"
down_revision = "0002_catalog_writes"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "recommendations",
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
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("deadline", sa.Integer, nullable=False),
        sa.Column("lease_id", sa.String(36)),
        sa.Column("lease_until", sa.Integer),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("error", sa.JSON),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','partial','failed','cancelled')"
        ),
        sa.CheckConstraint("attempts>=0 AND deadline>created_at"),
    )
    op.create_index("ix_recommendations_owner_id", "recommendations", ["owner_id"])
    op.create_table(
        "history_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
        ),
        sa.Column(
            "recommendation_id",
            sa.String(36),
            sa.ForeignKey("recommendations.id"),
            nullable=False,
        ),
        sa.Column("outfit_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.CheckConstraint("kind IN ('generated','shown','adopted','worn','rejected')"),
        sa.CheckConstraint("revision>=1"),
    )
    op.create_index("ix_history_events_owner_id", "history_events", ["owner_id"])
    op.create_table(
        "history_amendments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("history_events.id"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.String(36),
            sa.ForeignKey("change_proposals.id"),
            nullable=False,
        ),
        sa.Column("before", sa.JSON, nullable=False),
        sa.Column("after", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )


def downgrade():
    raise RuntimeError(
        "Restore a verified compatible backup instead of dropping private history"
    )
