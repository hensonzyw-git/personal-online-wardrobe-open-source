"""Owner-selected storage season, separate from garment labels."""

import sqlalchemy as sa

from alembic import op

revision = "0006_season_selection"
down_revision = "0005_catalog_import"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "wardrobe_preferences",
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
        ),
        sa.Column("active_season", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("rules_version", sa.String(80), nullable=False),
        sa.Column("rules_digest", sa.String(64), nullable=False),
        sa.Column("initialized_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.CheckConstraint("revision>=1"),
        sa.CheckConstraint("active_season IN ('spring_summer','autumn_winter')"),
    )
    op.add_column(
        "recommendations", sa.Column("wardrobe_context", sa.JSON, nullable=True)
    )


def downgrade():
    raise RuntimeError("Preserve recommendation evidence; use a forward repair")
