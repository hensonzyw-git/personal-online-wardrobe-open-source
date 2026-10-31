import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from wardrobe.db import Database


def old_database(tmp_path, *, invalid_reference=False):
    database = Database(tmp_path / "data" / "wardrobe.sqlite")
    database.path.parent.mkdir(mode=0o700)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "alembic")
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0006_season_selection")
    with sqlite3.connect(database.path) as c:
        c.execute("INSERT INTO owners VALUES ('owner')")
        c.execute(
            "INSERT INTO client_grants VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "a" * 64,
                "owner",
                "client",
                '["wardrobe.read"]',
                "aud",
                "agent",
                100,
                200,
                None,
            ),
        )
        c.execute(
            "INSERT INTO recommendations (id,owner_id,client_id,grant_id,payload,state,created_at,deadline,attempts) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "rec",
                "owner",
                "client",
                "b" * 64 if invalid_reference else "a" * 64,
                "{}",
                "succeeded",
                100,
                200,
                1,
            ),
        )
    return database


def test_upgrade_preserves_grant_and_reference_and_bounds_renewals(tmp_path):
    database = old_database(tmp_path)
    with sqlite3.connect(database.path) as c:
        before = c.execute("SELECT * FROM client_grants").fetchall()
        reference = c.execute("SELECT * FROM recommendations").fetchall()
    database.migrate()
    database.assert_initialized()
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with sqlite3.connect(database.path) as c:
        assert c.execute("SELECT * FROM client_grants").fetchall() == [
            before[0] + (None,)
        ]
        assert c.execute("SELECT * FROM recommendations").fetchall() == reference
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            c.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='ix_client_grants_owner_id'"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE client_grants SET expires_at=604901")
        c.execute("UPDATE client_grants SET renewed_at=150,expires_at=604950")
        for anchor, expiry in [(99, 200), (150, 150), (150, 604951)]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "UPDATE client_grants SET renewed_at=?,expires_at=?",
                    (anchor, expiry),
                )
        c.execute("PRAGMA foreign_keys=ON")
    database.migrate()  # Repeated migrations are safe.
    database.close()


def test_invalid_foreign_key_rolls_back_table_and_revision(tmp_path):
    database = old_database(tmp_path, invalid_reference=True)
    with pytest.raises(RuntimeError, match="foreign key"):
        database.migrate()
    with sqlite3.connect(database.path) as c:
        assert (
            c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0006_season_selection"
        )
        assert "renewed_at" not in [
            r[1] for r in c.execute("PRAGMA table_info(client_grants)")
        ]
        assert c.execute("SELECT count(*) FROM client_grants").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='client_grants_next'"
            ).fetchone()[0]
            == 0
        )
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    database.close()
