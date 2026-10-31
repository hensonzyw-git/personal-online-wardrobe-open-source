"""Assets, staged annotations and trusted confirmation records."""

import sqlalchemy as sa

from alembic import op

revision = "0002_catalog_writes"
down_revision = "0001_foundation"
branch_labels = depends_on = None


def upgrade():
    op.add_column("garments", sa.Column("deleted_at", sa.Integer(), nullable=True))
    op.add_column("garments", sa.Column("restore_until", sa.Integer(), nullable=True))
    # Explicit revision table definitions are frozen here, not imported from live metadata.
    specs = {
        "assets": [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("sha256", sa.String(64), unique=True, nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("mime", sa.String(50), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.CheckConstraint("size>0 AND width>0 AND height>0"),
        ],
        "owner_assets": [
            sa.Column(
                "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
            ),
            sa.Column(
                "asset_id", sa.String(36), sa.ForeignKey("assets.id"), primary_key=True
            ),
        ],
        "garment_assets": [
            sa.Column(
                "garment_id",
                sa.String(36),
                sa.ForeignKey("garments.id"),
                primary_key=True,
            ),
            sa.Column(
                "asset_id", sa.String(36), sa.ForeignKey("assets.id"), primary_key=True
            ),
        ],
        "asset_uploads": [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
            ),
            sa.Column("client_id", sa.String(100), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column(
                "asset_id", sa.String(36), sa.ForeignKey("assets.id"), nullable=True
            ),
        ],
        "idempotency": [
            sa.Column(
                "owner_id", sa.String(36), sa.ForeignKey("owners.id"), primary_key=True
            ),
            sa.Column("client_id", sa.String(100), primary_key=True),
            sa.Column("action", sa.String(50), primary_key=True),
            sa.Column("key", sa.String(128), primary_key=True),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False),
        ],
        "annotation_versions": [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "garment_id",
                sa.String(36),
                sa.ForeignKey("garments.id"),
                nullable=False,
            ),
            sa.Column("base_revision", sa.Integer(), nullable=False),
            sa.Column("fields", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        ],
        "change_proposals": [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
            ),
            sa.Column("created_by_client", sa.String(100), nullable=False),
            sa.Column("changes", sa.JSON(), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("nonce", sa.String(100), nullable=False),
            sa.Column("expires_at", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("receipt", sa.JSON(), nullable=True),
        ],
        "trusted_issuers": [
            sa.Column("issuer", sa.String(100), primary_key=True),
            sa.Column("kid", sa.String(100), primary_key=True),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("bindings", sa.JSON(), nullable=False),
            sa.Column("purposes", sa.JSON(), nullable=False),
            sa.Column("audience", sa.String(100), nullable=False),
            sa.Column("expires_at", sa.Integer(), nullable=False),
            sa.Column("revoked_at", sa.Integer(), nullable=True),
        ],
        "human_decisions": [
            sa.Column("issuer", sa.String(100), primary_key=True),
            sa.Column("jti", sa.String(100), primary_key=True),
            sa.Column(
                "proposal_id",
                sa.String(36),
                sa.ForeignKey("change_proposals.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("assertion_digest", sa.String(64), nullable=False),
            sa.Column("receipt", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        ],
        "audit_events": [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "owner_id", sa.String(36), sa.ForeignKey("owners.id"), nullable=False
            ),
            sa.Column("client_id", sa.String(100), nullable=False),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("target_id", sa.String(36), nullable=False),
            sa.Column("receipt_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        ],
    }
    for name, columns in specs.items():
        op.create_table(name, *columns)


def downgrade():
    raise RuntimeError(
        "Restore a compatible verified backup; destructive downgrade is disabled"
    )
