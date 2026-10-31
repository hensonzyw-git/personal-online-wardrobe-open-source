from alembic import context
from wardrobe.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "Use wardrobectl init-db to run migrations with an explicit database"
    )
context.configure(
    connection=connection, target_metadata=Base.metadata, render_as_batch=True
)
with context.begin_transaction():
    context.run_migrations()
