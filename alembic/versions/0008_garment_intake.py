"""Frozen SQLite intake schema; does not import mutable application models."""

import sqlalchemy as sa

from alembic import op

revision = "0008_garment_intake"
down_revision = "0007_managed_renewal"
branch_labels = None
depends_on = None
DDL = (
    """CREATE TABLE garment_intakes (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(36) NOT NULL,
	client_id VARCHAR(100) NOT NULL,
	origin_scope VARCHAR(128) NOT NULL,
	state VARCHAR(30) NOT NULL,
	revision INTEGER NOT NULL,
	image_revision INTEGER NOT NULL,
	payload JSON NOT NULL,
	field_sources JSON NOT NULL,
	frozen JSON,
	created_at INTEGER NOT NULL,
	last_user_edit_at INTEGER NOT NULL,
	expires_at INTEGER NOT NULL,
	published_garment_id VARCHAR(36),
	receipt JSON,
	PRIMARY KEY (id),
	UNIQUE (owner_id, client_id, origin_scope),
	CHECK (revision>=1 AND image_revision>=0),
	CHECK (state IN ('collecting','review','frozen','commit_pending','submitted','closed','expired')),
	FOREIGN KEY(owner_id) REFERENCES owners (id),
	UNIQUE (published_garment_id),
	FOREIGN KEY(published_garment_id) REFERENCES garments (id)
)""",
    """CREATE INDEX ix_garment_intakes_owner_id ON garment_intakes (owner_id)""",
    """CREATE TABLE intake_uploads (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	image_id VARCHAR(36) NOT NULL,
	kind VARCHAR(10) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	size INTEGER NOT NULL,
	metadata_json JSON NOT NULL,
	state VARCHAR(20) NOT NULL,
	expires_at INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id)
)""",
    """CREATE INDEX ix_intake_uploads_task_id ON intake_uploads (task_id)""",
    """CREATE TABLE intake_source_objects (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	asset_id VARCHAR(36) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	view_sha256 VARCHAR(64) NOT NULL,
	report JSON NOT NULL,
	state VARCHAR(20) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owners (id),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)""",
    """CREATE TABLE intake_images (
	task_id VARCHAR(36) NOT NULL,
	image_id VARCHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	role VARCHAR(10) NOT NULL,
	source_kind VARCHAR(20) NOT NULL,
	source_object_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (task_id, image_id),
	UNIQUE (task_id, ordinal),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id),
	FOREIGN KEY(source_object_id) REFERENCES intake_source_objects (id)
)""",
    """CREATE TABLE intake_observations (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	input_fingerprint VARCHAR(64) NOT NULL,
	producer VARCHAR(40) NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id)
)""",
    """CREATE TABLE intake_commit_attempts (
	jti VARCHAR(100) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	signed_request_hash VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(30) NOT NULL,
	permit JSON,
	receipt JSON,
	PRIMARY KEY (jti),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id)
)""",
    """CREATE INDEX ix_intake_commit_attempts_task_id ON intake_commit_attempts (task_id)""",
    """CREATE TABLE intake_decisions (
	issuer VARCHAR(100) NOT NULL,
	jti VARCHAR(100) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	assertion_digest VARCHAR(64) NOT NULL,
	receipt JSON NOT NULL,
	created_at INTEGER NOT NULL,
	PRIMARY KEY (issuer, jti),
	UNIQUE (task_id),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id)
)""",
    """CREATE TABLE intake_gc_jobs (
	source_id VARCHAR(36) NOT NULL,
	grace_until INTEGER NOT NULL,
	state VARCHAR(20) NOT NULL,
	retries INTEGER NOT NULL,
	PRIMARY KEY (source_id),
	FOREIGN KEY(source_id) REFERENCES intake_source_objects (id)
)""",
    """CREATE TABLE garment_intake_origins (
	garment_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	sources JSON NOT NULL,
	receipt JSON NOT NULL,
	PRIMARY KEY (garment_id),
	FOREIGN KEY(garment_id) REFERENCES garments (id),
	UNIQUE (task_id),
	FOREIGN KEY(task_id) REFERENCES garment_intakes (id)
)""",
)


def upgrade():
    op.add_column(
        "garments",
        sa.Column("identity_fields", sa.JSON(), nullable=False, server_default="{}"),
    )
    for statement in DDL:
        op.execute(statement)


def downgrade():
    raise RuntimeError(
        "Forward repair or isolated backup restore; never discard created garments"
    )
