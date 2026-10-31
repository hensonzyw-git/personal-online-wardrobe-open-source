"""Keep initial grant identity while bounding each managed renewal to seven days."""

import sqlalchemy as sa

from alembic import op

revision = "0007_managed_renewal"
down_revision = "0006_season_selection"
branch_labels = None
depends_on = None

FIELDS = "token_hash,owner_id,client_id,scopes,audience,channel,created_at,expires_at,revoked_at"


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        raise RuntimeError("Run migrations through wardrobectl init-db")
    count = connection.exec_driver_sql("SELECT count(*) FROM client_grants").scalar()
    op.create_table(
        "client_grants_next",
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
        sa.Column("renewed_at", sa.Integer(), nullable=True),
        sa.CheckConstraint("length(token_hash)=64"),
        sa.CheckConstraint("channel IN ('agent','human_adapter')"),
        sa.CheckConstraint(
            "(renewed_at IS NULL OR renewed_at >= created_at) AND "
            "expires_at > COALESCE(renewed_at,created_at) AND "
            "expires_at <= COALESCE(renewed_at,created_at) + 604800"
        ),
    )
    connection.exec_driver_sql(
        f"INSERT INTO client_grants_next ({FIELDS}) SELECT {FIELDS} FROM client_grants"
    )
    copied = connection.exec_driver_sql(
        "SELECT count(*) FROM client_grants_next"
    ).scalar()
    difference = connection.exec_driver_sql(
        f"SELECT {FIELDS} FROM client_grants EXCEPT SELECT {FIELDS} FROM client_grants_next"
    ).first()
    if copied != count or difference:
        raise RuntimeError("Grant migration preservation check failed")
    op.drop_table("client_grants")
    op.rename_table("client_grants_next", "client_grants")
    op.create_index("ix_client_grants_owner_id", "client_grants", ["owner_id"])


def downgrade():
    raise RuntimeError(
        "Restore the pre-migration backup with services and renewal timer stopped"
    )
