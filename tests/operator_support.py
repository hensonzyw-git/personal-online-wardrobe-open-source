"""Synthetic, offline operator fixtures; no production files or network."""

import hashlib
import io
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from wardrobe.app import create_app
from wardrobe.auth import Authenticator
from wardrobe.cli import grant_client
from wardrobe.config import Settings
from wardrobe.db import Database
from wardrobe.models import (
    Asset,
    AuditEvent,
    ChangeProposal,
    Garment,
    GarmentAsset,
    HumanDecision,
    Idempotency,
    OwnerAsset,
    ProductDraft,
    ServiceState,
)

T = 1800000000
OWNER = str(UUID(int=1))
OTHER_OWNER = str(UUID(int=2))
TOKEN = "synthetic-operator-token-" + "o" * 40
READ_TOKEN = "synthetic-operator-reader-" + "r" * 40
OTHER_TOKEN = "synthetic-other-reader-" + "b" * 40
KEY = bytes(range(32))


def uid(n):
    return str(UUID(int=n))


def headers(token=TOKEN, key="synthetic-action"):
    value = {"Authorization": "Bearer " + token}
    if key is not None:
        value["Idempotency-Key"] = key
    return value


def row(
    n, *, owner=OWNER, created_at=T - 100, category="shirt", state="active", until=None
):
    return Garment(
        id=uid(n),
        owner_id=owner,
        name=f"Synthetic {n}",
        category=category,
        roles=["upper"],
        availability="unknown",
        state=state,
        revision=1,
        annotation_fields={
            "material": {
                "value": "cotton",
                "source_kind": "user_input",
                "verification_state": "user_verified",
                "evidence": [{"kind": "synthetic"}],
            }
        },
        annotation_review_state="initial",
        identity_fields={"article_number": "SYNTHETIC-ONLY"},
        product_reference={"provider": "synthetic", "global_spu_id": "123"},
        created_at=created_at,
        deleted_at=T - 50 if state == "deleted" else None,
        restore_until=until,
    )


def snapshot(db, garment_id="00000000-0000-0000-0000-00000000000a"):
    with db.sessions.begin() as s:
        g = s.get(Garment, garment_id)
        return {
            "garment": {c.name: getattr(g, c.name) for c in Garment.__table__.columns},
            "catalog": s.get(ServiceState, OWNER).catalog_revision,
            **{
                model.__tablename__: s.scalar(select(func.count()).select_from(model))
                for model in (AuditEvent, Idempotency, ChangeProposal, HumanDecision)
            },
        }


@pytest.fixture
def operator_env(tmp_path, monkeypatch):
    from wardrobe.cli import grant_operator

    clock = [T]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    settings = Settings(data_root=tmp_path / "synthetic-operator")
    assert settings.agent_enabled is False
    db = Database(settings.data_root / "wardrobe.sqlite")
    db.migrate()  # only the pre-existing 0010 chain, in a temporary synthetic DB
    for name, data in (("cursor.key", KEY), ("upload.key", bytes(range(32, 64)))):
        p = settings.data_root / name
        p.write_bytes(data)
        p.chmod(0o600)
    grant_client(
        db,
        settings,
        owner_id=OWNER,
        client_id="synthetic-reader",
        scopes=["wardrobe.read"],
        token=READ_TOKEN,
    )
    grant_client(
        db,
        settings,
        owner_id=OTHER_OWNER,
        client_id="other-reader",
        scopes=["wardrobe.read"],
        token=OTHER_TOKEN,
    )
    grant_operator(db, settings, owner_id=OWNER, token=TOKEN)
    content = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(content, format="PNG")
    image_bytes = content.getvalue()
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    originals = settings.data_root / "assets" / "original"
    originals.mkdir(parents=True)
    image_path = originals / image_hash
    image_path.write_bytes(image_bytes)
    with db.write() as s:
        s.add(
            Asset(
                id=uid(500),
                sha256=image_hash,
                size=len(image_bytes),
                mime="image/png",
                width=4,
                height=4,
            )
        )
        s.flush()
        s.add(OwnerAsset(owner_id=OWNER, asset_id=uid(500)))
        for n in range(10, 16):
            s.add(row(n, created_at=T + n))
        s.add(row(20, state="deleted", until=T + 100))
        s.add(row(21, state="deleted", until=T))
        s.add(row(22, state="deleted", until=T - 1))
        s.add(row(23, state="deleted"))
        s.add(row(30, owner=OTHER_OWNER))
        s.flush()
        s.add_all(
            [GarmentAsset(garment_id=uid(n), asset_id=uid(500)) for n in (10, 11, 20)]
        )
        s.add(
            ProductDraft(
                id=uid(40),
                owner_id=OWNER,
                client_id="synthetic",
                revision=1,
                state="draft",
                payload={},
                created_at=T,
                expires_at=T + 100,
            )
        )
    app = create_app(settings, database=db)
    for middleware in app.user_middleware:
        auth = middleware.kwargs.get("authenticator")
        if auth:
            auth.now = lambda: clock[0]
    auth = Authenticator(db.sessions, settings.audience, now=lambda: clock[0])
    with TestClient(app, base_url="http://localhost") as client:
        yield SimpleNamespace(
            db=db,
            settings=settings,
            app=app,
            client=client,
            clock=clock,
            principal=auth.authenticate(TOKEN),
            auth=auth,
            image_path=image_path,
            image_bytes=image_bytes,
            key=KEY,
        )
    db.close()
