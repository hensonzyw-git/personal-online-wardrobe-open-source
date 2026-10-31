import os
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from alembic import command


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 2},
        )

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _record):
            # Python sqlite3's legacy transaction control does not BEGIN for SELECT.
            # Own BEGIN explicitly so multi-query reads share one WAL snapshot.
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("PRAGMA journal_mode=WAL")

        @event.listens_for(self.engine, "begin")
        def begin(connection):
            connection.exec_driver_sql(
                "BEGIN IMMEDIATE"
                if connection.get_execution_options().get("sqlite_write")
                else "BEGIN"
            )

        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        # Create the private database before SQLite writes a journal alongside it.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        bundled = Path(__file__).parent / "migrations"
        scripts = (
            bundled
            if bundled.exists()
            else Path(__file__).resolve().parents[2] / "alembic"
        )
        config = Config()
        config.set_main_option("script_location", str(scripts))
        with self.engine.connect() as connection:
            # SQLite cannot rebuild a referenced table with FK enforcement on.
            # Toggle only this connection, before BEGIN; validate before COMMIT.
            raw = connection.connection.driver_connection
            raw.execute("PRAGMA foreign_keys=OFF")
            try:
                with connection.begin():
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
                    if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                        raise RuntimeError("Migration foreign key validation failed")
            finally:
                raw.execute("PRAGMA foreign_keys=ON")
                if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise RuntimeError("Migration foreign key enforcement unavailable")

    def assert_initialized(self) -> None:
        if not self.path.is_file():
            raise RuntimeError("Run wardrobectl init-db before starting the service")
        with self.engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
            if revision != "0010_agent_facade":
                raise RuntimeError(
                    "Database migration does not match this service version"
                )

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def write(self):
        with self.sessions.begin() as session:
            session.connection(execution_options={"sqlite_write": True})
            yield session


def load_cursor_key(root: Path, *, create: bool = False) -> bytes:
    return load_private_key(root, "cursor.key", create=create)


def load_private_key(root: Path, name: str, *, create: bool = False) -> bytes:
    if name not in {"cursor.key", "upload.key"}:
        raise ValueError("Unknown key purpose")
    path = root / name
    if create:
        import secrets

        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as file:
                file.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32 or path.stat().st_mode & 0o077:
        raise RuntimeError("Service signing key must be private and contain 32 bytes")
    return key
