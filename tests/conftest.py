import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from wardrobe.app import create_app
from wardrobe.cli import grant_client
from wardrobe.config import Settings
from wardrobe.db import Database, load_cursor_key, load_private_key
from wardrobe.models import Garment

OWNER_A = str(UUID(int=1))
OWNER_B = str(UUID(int=2))
TOKEN_A = "synthetic-owner-a-read-token-" + "a" * 40
TOKEN_B = "synthetic-owner-b-read-token-" + "b" * 40
TOKEN_NO_READ = "synthetic-without-read-token-" + "c" * 40


@pytest.fixture
def store(tmp_path):
    settings = Settings(data_root=tmp_path / "wardrobe")
    db = Database(settings.data_root / "wardrobe.sqlite")
    db.migrate()
    key = load_cursor_key(settings.data_root, create=True)
    load_private_key(settings.data_root, "upload.key", create=True)
    grant_client(
        db,
        settings,
        owner_id=OWNER_A,
        client_id="client-a",
        scopes=["wardrobe.read"],
        token=TOKEN_A,
    )
    grant_client(
        db,
        settings,
        owner_id=OWNER_B,
        client_id="client-b",
        scopes=["wardrobe.read"],
        token=TOKEN_B,
    )
    grant_client(
        db,
        settings,
        owner_id=OWNER_A,
        client_id="no-read",
        scopes=["wardrobe.propose_change"],
        token=TOKEN_NO_READ,
    )
    with db.sessions.begin() as session:
        for number in range(10, 16):
            session.add(
                Garment(
                    id=str(UUID(int=number)),
                    owner_id=OWNER_A,
                    name=f"Synthetic {number}",
                    category="shirt",
                    roles=["upper"],
                    availability="unknown",
                    state="active",
                    revision=1,
                    annotation_fields={
                        "material": {
                            "value": None,
                            "source_kind": "unknown",
                            "verification_state": "unknown",
                        }
                    },
                    annotation_review_state="initial",
                    created_at=int(time.time()),
                )
            )
        session.add(
            Garment(
                id=str(UUID(int=20)),
                owner_id=OWNER_A,
                name="Deleted",
                category="shirt",
                roles=["upper"],
                availability="unknown",
                state="deleted",
                revision=2,
                annotation_fields={},
                annotation_review_state="initial",
                created_at=int(time.time()),
            )
        )
        session.add(
            Garment(
                id=str(UUID(int=30)),
                owner_id=OWNER_B,
                name="Other owner",
                category=None,
                roles=[],
                availability="unknown",
                state="active",
                revision=1,
                annotation_fields={},
                annotation_review_state="needs_review",
                created_at=int(time.time()),
            )
        )
    yield db, settings, key
    db.close()


@pytest.fixture
def providers():
    return {}


@pytest.fixture
def live_service(store, providers):
    db, settings, _key = store
    app = create_app(settings, database=db, **providers)
    # Exercise HTTP/MCP and the real lifespan worker without binding a socket.
    with TestClient(app, base_url="http://localhost") as client:
        yield client, db
