"""Exercise schema 0009→0010 migration and backup restoration on synthetic rows."""

import sqlite3
import time
from uuid import uuid4

from sqlalchemy import text

from alembic import command
from wardrobe.db import Database
from wardrobe.models import GarmentIntake, IntakeProductEnrichment, Owner


def snapshot(db):
    with db.sessions() as session:
        return {
            table: [
                tuple(r) for r in session.execute(text('SELECT * FROM "' + table + '"'))
            ]
            for table in [
                "owners",
                "garment_intakes",
                "intake_product_enrichments",
            ]
        }


def test_schema0009_enrichment_survives_migration_and_backup(tmp_path, monkeypatch):
    db = Database(tmp_path / "old.sqlite")
    real = command.upgrade
    monkeypatch.setattr(
        command, "upgrade", lambda config, _: real(config, "0009_intake_enrichment")
    )
    db.migrate()
    owner, task = str(uuid4()), str(uuid4())
    now = int(time.time())
    with db.write() as s:
        s.add(Owner(id=owner))
        s.flush()
        s.add(
            GarmentIntake(
                id=task,
                owner_id=owner,
                client_id="synthetic-old",
                origin_scope="old-intake",
                created_at=now,
                last_user_edit_at=now,
                expires_at=now + 86400,
            )
        )
        s.flush()
        s.add(
            IntakeProductEnrichment(
                task_id=task,
                schema_version="test/1",
                input_fingerprint="a" * 64,
                state="idle",
                attempts_reserved=0,
                generation=1,
                payload={"rounds": []},
                evidence_digest=None,
                updated_at=now,
                reserved_bytes=0,
            )
        )
    before = snapshot(db)
    backup = tmp_path / "backup.sqlite"
    with sqlite3.connect(db.path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    monkeypatch.setattr(command, "upgrade", real)
    db.migrate()
    db.assert_initialized()
    assert snapshot(db) == before
    with db.sessions() as s:
        assert s.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert s.execute(text("PRAGMA foreign_key_check")).first() is None
        assert s.execute(text("SELECT count(*) FROM agent_operations")).scalar() == 0
        tables = {
            row[0]
            for row in s.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "intake_product_enrichments" in tables
        assert not any(name.startswith("legacy_") for name in tables)
    db.close()
    restored = Database(backup)
    assert snapshot(restored) == before
    with restored.sessions() as s:
        assert (
            s.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0009_intake_enrichment"
        )
    restored.close()
