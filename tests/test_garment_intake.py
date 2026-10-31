"""Synthetic HTTP intake, trust boundaries and crash/replay contracts."""

import base64
import hashlib
import io
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import OWNER_A, OWNER_B
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.cli import grant_client, register_issuer
from wardrobe.db import load_private_key
from wardrobe.intakes import IntakeService
from wardrobe.models import (
    Garment,
    GarmentIntake,
    IntakeCommitAttempt,
    ServiceState,
)
from wardrobe.preview_worker import transform
from wardrobe.writes import canonical

REAL_REMOTE_PERMIT = IntakeService._remote_permit


@pytest.fixture
def intake(store, monkeypatch):
    db, settings, _ = store
    settings = settings.model_copy(update={"intake_enabled": True})

    def child(args, **kwargs):
        transform(Path(args[3]), args[4], Path(args[5]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("wardrobe.image_previews.subprocess.run", child)
    tokens = {
        k: ("synthetic-intake-" + k + "-") * 4
        for k in ("draft", "upload", "confirm", "foreign")
    }
    for key, scopes, channel, owner in [
        ("draft", ["wardrobe.intake", "wardrobe.read"], "agent", OWNER_A),
        (
            "upload",
            ["wardrobe.intake_upload", "wardrobe.intake_observe"],
            "agent",
            OWNER_A,
        ),
        ("confirm", ["human.intake_create"], "human_adapter", OWNER_A),
        ("foreign", ["wardrobe.intake"], "agent", OWNER_B),
    ]:
        grant_client(
            db,
            settings,
            owner_id=owner,
            client_id="personal-agent",
            scopes=scopes,
            token=tokens[key],
            channel=channel,
        )
    private = Ed25519PrivateKey.generate()
    register_issuer(
        db,
        settings,
        issuer="synthetic-pa",
        kid="intake-key",
        public_key=private.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode(),
        subject="pa-user",
        owner_id=OWNER_A,
        client_id="personal-agent",
        ttl_seconds=3600,
        intake_only=True,
    )
    monkeypatch.setattr(
        IntakeService,
        "_remote_permit",
        lambda self, b: {**b, "generation": 1, "expires_at": int(time.time()) + 60},
    )
    with TestClient(
        create_app(settings, database=db), base_url="http://localhost"
    ) as client:
        yield client, tokens, private, db, settings


def headers(tokens, k="draft", **kw):
    return {"Authorization": "Bearer " + tokens[k], **kw}


def call(client, tokens, method, path, body=None, role="draft", key=None, status=200):
    h = headers(tokens, role, **({"Idempotency-Key": key} if key else {}))
    r = client.request(method, path, json=body, headers=h)
    assert r.status_code == status, r.text
    return r.json().get("data", r.json())


def new(client, tokens):
    return call(
        client,
        tokens,
        "POST",
        "/v1/intakes",
        {"origin_scope": str(uuid4())},
        key=str(uuid4()),
    )


def image_bytes(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (100, 80), color).save(output, "JPEG")
    return output.getvalue()


def add_image(client, tokens, task, role="garment", ordinal=1, source=None, view=None):
    source = source or image_bytes()
    sha = hashlib.sha256(source).hexdigest()
    image_id = str(uuid4())
    uploads = []
    for kind in ("source", "view"):
        data = source if kind == "source" else (view or source)
        body = {
            "expected_revision": task["revision"],
            "image_id": image_id,
            "ordinal": ordinal,
            "role": role,
            "source_kind": "owned_photo",
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mime": "image/jpeg",
            "width": 100,
            "height": 80,
            "source_sha256": sha if kind == "view" else None,
        }
        u = call(
            client,
            tokens,
            "POST",
            f"/v1/intakes/{task['task_id']}/uploads",
            body,
            "upload",
            str(uuid4()),
        )
        r = client.put(
            u["upload_url"],
            content=data,
            headers=headers(
                tokens, "upload", **{"X-Wardrobe-Upload-Token": u["upload_token"]}
            ),
        )
        assert r.status_code == 200, r.text
        uploads.append(u["upload_id"])
    return call(
        client,
        tokens,
        "POST",
        f"/v1/intakes/{task['task_id']}/complete",
        {
            "expected_revision": task["revision"],
            "source_upload_id": uploads[0],
            "view_upload_id": uploads[1],
        },
        "upload",
    )


def freeze(client, tokens, task):
    task = call(
        client,
        tokens,
        "PATCH",
        f"/v1/intakes/{task['task_id']}",
        {
            "expected_revision": task["revision"],
            "fields": {
                "name": "Synthetic blue shirt",
                "category": "shirt",
                "roles": ["upper"],
                "thickness": "thin",
            },
        },
    )
    return call(
        client,
        tokens,
        "POST",
        f"/v1/intakes/{task['task_id']}/freeze",
        {"expected_revision": task["revision"]},
    )


def assertion(task, private, **changes):
    now = int(time.time())
    fields = task["field_digests"]
    c = {
        "iss": "synthetic-pa",
        "sub": "pa-user",
        "owner_id": OWNER_A,
        "client_id": "personal-agent",
        "actor_subject": "synthetic-device",
        "aud": "wardrobe:intake-create",
        "purpose": "intake_create",
        "task_id": task["task_id"],
        "revision": task["revision"],
        "payload_digest": task["payload_digest"],
        "duplicate_set_digest": task["duplicate_set_digest"],
        "duplicate_overrides": task["duplicates"],
        "attested_fields": [
            {
                "field_path": "thickness",
                **fields["thickness"],
                "user_action_id": "synthetic-user-edit",
                "input_revision": task["image_revision"],
            }
        ],
        "nonce": task["nonce"],
        "jti": str(uuid4()),
        "decision": "approve",
        "iat": now,
        "exp": now + 60,
        **changes,
    }
    enc = lambda v: base64.urlsafe_b64encode(v).decode().rstrip("=")
    h = enc(
        canonical(
            {"alg": "EdDSA", "typ": "wardrobe-intake-human/1", "kid": "intake-key"}
        )
    )
    b = enc(canonical(c))
    return h + "." + b + "." + enc(private.sign((h + "." + b).encode()))


def test_http_complete_original_preservation_ocr_commit_replay(intake):
    c, t, key, db, _settings = intake
    task = new(c, t)
    raw = image_bytes()
    task = add_image(c, t, task, source=raw)
    task = add_image(c, t, task, "label", 2, image_bytes("white"))
    label = task["images"][1]
    ocr = {
        "image_id": label["image_id"],
        "source_hash": label["source_sha256"],
        "view_hash": label["view_sha256"],
        "raw_text": "Brand: Test Brand\nStyle No: TB-10\nSize: L",
    }
    task = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/observations",
        {
            "expected_revision": task["revision"],
            "image_revision": task["image_revision"],
            "input_fingerprint": task["input_fingerprint"],
            "producer": "apple_vision",
            "schema_version": "wardrobe-label-ocr/1",
            "fields": {"brand": "Test Brand", "article_number": "TB-10", "size": "L"},
            "ocr": ocr,
        },
        "upload",
    )
    task = freeze(c, t, task)
    signed = assertion(task, key)
    path = f"/v1/intakes/{task['task_id']}/decisions"
    a = call(c, t, "POST", path, {"assertion": signed}, "confirm")
    b = call(c, t, "POST", path, {"assertion": signed}, "confirm")
    assert a == b
    gid = a["garment_id"]
    g = call(c, t, "GET", f"/v1/garments/{gid}")["garment"]
    assert g["annotation_fields"]["thickness"]["verification_state"] == "user_verified"
    assert g["identity_fields"]["article_number"]["verification_state"] == "unverified"
    assert len(g["image_references"]) == 1
    asset = g["image_references"][0]["asset_id"]
    r = c.get(f"/v1/assets/{asset}/content", headers=headers(t))
    assert r.content == raw
    r = c.get(f"/v1/assets/{asset}/preview", headers=headers(t))
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    for im in task["images"]:
        r = c.get(
            f"/v1/garments/{gid}/sources/{im['source_object_id']}/content",
            headers=headers(t),
        )
        assert r.status_code == 200
    with db.sessions() as s:
        assert s.get(ServiceState, OWNER_A).catalog_revision == 1
        assert (
            len(
                list(
                    s.scalars(
                        __import__("sqlalchemy")
                        .select(Garment)
                        .where(Garment.id == gid)
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified_fields", ["thickness"]),
        ("field_sources", {}),
        ("owner_id", OWNER_B),
        ("revision", 200),
        ("expires_at", 99999999),
    ],
)
def test_model_cannot_patch_authority(intake, field, value):
    c, t, *_ = intake
    task = new(c, t)
    call(
        c,
        t,
        "PATCH",
        f"/v1/intakes/{task['task_id']}",
        {"expected_revision": 1, "fields": {field: value}},
        status=422,
    )


def test_permissions_terminal_cas_and_hidden_owner(intake):
    c, t, _key, *_ = intake
    task = new(c, t)
    path = f"/v1/intakes/{task['task_id']}"
    call(c, t, "GET", path, role="foreign", status=404)
    call(
        c,
        t,
        "PATCH",
        path,
        {"expected_revision": 2, "fields": {"name": "bad"}},
        status=409,
    )
    call(
        c,
        t,
        "POST",
        path + "/freeze",
        {"expected_revision": 1},
        role="upload",
        status=403,
    )
    call(c, t, "POST", path + "/close")
    call(
        c,
        t,
        "PATCH",
        path,
        {"expected_revision": 2, "fields": {"name": "bad"}},
        status=409,
    )


def test_no_model_can_sign_or_supply_warmth(intake):
    c, t, key, *_ = intake
    task = add_image(c, t, new(c, t))
    task = freeze(c, t, task)
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": assertion(task, key)},
        status=403,
    )
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/observations",
        {
            "expected_revision": task["revision"],
            "image_revision": task["image_revision"],
            "input_fingerprint": task["input_fingerprint"],
            "producer": "intake_visual",
            "schema_version": "wardrobe-intake-vision/1",
            "fields": {"thickness": "thick"},
        },
        role="upload",
        status=422,
    )


def test_close_wins_race_after_permit(intake, monkeypatch):
    c, t, key, db, settings = intake
    task = freeze(c, t, add_image(c, t, new(c, t)))
    service = IntakeService(
        db, settings.data_root, load_private_key(settings.data_root, "upload.key")
    )
    from wardrobe.auth import Authenticator

    principal = Authenticator(db.sessions, settings.audience).authenticate(t["draft"])

    def callback(self, b):
        service.close(principal, task["task_id"])
        return {**b, "generation": 1, "expires_at": int(time.time()) + 60}

    monkeypatch.setattr(IntakeService, "_remote_permit", callback)
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": assertion(task, key)},
        "confirm",
        status=409,
    )
    with db.sessions() as s:
        assert s.get(GarmentIntake, task["task_id"]).published_garment_id is None


def test_idempotency_different_body_rejected(intake):
    c, t, *_ = intake
    k = str(uuid4())
    a = call(c, t, "POST", "/v1/intakes", {"origin_scope": "one"}, key=k)
    b = call(c, t, "POST", "/v1/intakes", {"origin_scope": "one"}, key=k)
    assert a == b
    call(c, t, "POST", "/v1/intakes", {"origin_scope": "two"}, key=k, status=409)


def test_no_ocr_label_binding_and_no_opaque_source_for_analysis(intake):
    c, t, _key, *_ = intake
    task = add_image(c, t, new(c, t))
    im = task["images"][0]
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/observations",
        {
            "expected_revision": task["revision"],
            "image_revision": task["image_revision"],
            "input_fingerprint": task["input_fingerprint"],
            "producer": "apple_vision",
            "schema_version": "wardrobe-label-ocr/1",
            "fields": {"brand": "Fake"},
            "ocr": {
                "image_id": im["image_id"],
                "source_hash": im["source_sha256"],
                "view_hash": im["view_sha256"],
                "raw_text": "Brand: Fake",
            },
        },
        "upload",
        status=422,
    )
    task = add_image(c, t, task, "label", 2, image_bytes("white"))
    label = task["images"][1]
    r = c.get(
        f"/v1/intakes/{task['task_id']}/images/{label['image_id']}/analysis",
        headers=headers(t, "upload"),
    )
    assert r.status_code == 403
    r = c.get(
        f"/v1/intakes/{task['task_id']}/images/{im['image_id']}/analysis",
        headers=headers(t, "upload"),
    )
    assert r.status_code == 200 and len(r.content) <= 512 * 1024


def test_duplicate_set_changed_between_freeze_and_commit(intake):
    c, t, key, _db, _settings = intake
    one = freeze(c, t, add_image(c, t, new(c, t)))
    two = freeze(c, t, add_image(c, t, new(c, t)))
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{one['task_id']}/decisions",
        {"assertion": assertion(one, key)},
        "confirm",
    )
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{two['task_id']}/decisions",
        {"assertion": assertion(two, key)},
        "confirm",
        status=409,
    )
    two = call(c, t, "GET", f"/v1/intakes/{two['task_id']}")
    two = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{two['task_id']}/freeze",
        {"expected_revision": two["revision"]},
    )
    assert len(two["duplicates"]) == 1
    result = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{two['task_id']}/decisions",
        {"assertion": assertion(two, key)},
        "confirm",
    )
    assert result["state"] == "submitted"


def test_gc_only_isolated_closed_sources_not_committed_original(intake):
    c, t, key, db, settings = intake
    draft = add_image(c, t, new(c, t))
    committed = freeze(c, t, add_image(c, t, new(c, t), source=image_bytes("red")))
    result = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{committed['task_id']}/decisions",
        {"assertion": assertion(committed, key)},
        "confirm",
    )
    call(c, t, "POST", f"/v1/intakes/{draft['task_id']}/close")
    service = IntakeService(
        db, settings.data_root, load_private_key(settings.data_root, "upload.key")
    )
    service.collect(int(time.time()) + 2 * 86400)
    rawsha = draft["images"][0]["source_sha256"]
    assert not (service.files / rawsha).exists()
    r = c.get(f"/v1/garments/{result['garment_id']}", headers=headers(t))
    assert r.status_code == 200
    sha = committed["images"][0]["source_sha256"]
    assert (
        settings.data_root / "assets" / "original" / sha
    ).read_bytes() == image_bytes("red")


def test_expired_unknown_attempt_must_resolve_before_new_confirm(intake, monkeypatch):
    c, t, key, db, _settings = intake
    task = freeze(c, t, add_image(c, t, new(c, t)))

    def fail_permit(self, b):
        from wardrobe.errors import BusinessError

        raise BusinessError("COMMIT_OUTCOME_UNKNOWN", "Synthetic unavailable", 503)

    monkeypatch.setattr(IntakeService, "_remote_permit", fail_permit)
    signed = assertion(task, key)
    claim = json_claim(signed)
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": signed},
        "confirm",
        status=503,
    )
    with db.write() as s:
        a = s.get(IntakeCommitAttempt, claim["jti"])
        a.permit = {"expires_at": int(time.time()) - 1}
    a = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/attempts/{claim['jti']}/resolve",
        {"origin_deleted": False},
        "confirm",
    )
    assert a["state"] == "expired_aborted"
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": signed},
        "confirm",
        status=403,
    )


def json_claim(signed):
    import json

    body = signed.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_permit_hmac_uses_decoded_hex_secret_and_fixed_origin(intake, monkeypatch):
    import hmac

    from wardrobe.intakes import IntakeService
    from wardrobe.writes import canonical

    _c, _t, _key, db, settings = intake
    secret = bytes(range(32))
    monkeypatch.setenv("WARDROBE_INTAKE_PERMIT_KEY", secret.hex())
    request = {
        "task_id": str(uuid4()),
        "jti": str(uuid4()),
        "revision": 1,
        "payload_digest": "a" * 64,
        "origin_scope": str(uuid4()),
    }
    permit = {**request, "generation": 1, "expires_at": int(time.time()) + 60}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, content, headers):
            assert url == "https://agent.example.com/internal/wardrobe/intake-permits"
            raw = "\n".join(
                [
                    "POST",
                    "/internal/wardrobe/intake-permits",
                    headers["X-Wardrobe-Timestamp"],
                    headers["X-Wardrobe-Nonce"],
                    hashlib.sha256(content).hexdigest(),
                ]
            ).encode()
            assert (
                headers["X-Wardrobe-Signature"]
                == hmac.new(secret, raw, hashlib.sha256).hexdigest()
            )
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "permit": permit,
                    "signature": hmac.new(
                        secret, canonical(permit), hashlib.sha256
                    ).hexdigest(),
                },
            )

    monkeypatch.setattr("wardrobe.intakes.httpx.Client", Client)
    service = IntakeService(
        db, settings.data_root, load_private_key(settings.data_root, "upload.key")
    )
    assert REAL_REMOTE_PERMIT(service, request) == permit


def test_explicit_attested_storage_season_works_unknown_category_without_guessing():
    from wardrobe.seasons import classification

    garment = SimpleNamespace(
        category="unmapped-new-category",
        annotation_fields={
            "seasons": {
                "value": ["autumn_winter"],
                "source_kind": "user_supplied",
                "verification_state": "unverified",
            }
        },
    )
    assert classification(garment) == "unassigned"
    garment.annotation_fields["seasons"]["verification_state"] = "user_verified"
    assert classification(garment) == "autumn_winter"
    garment.annotation_fields["seasons"]["source_kind"] = "visual_observation"
    assert classification(garment) == "unassigned"


def test_default_closed_same_schema_preserves_submitted_reads(intake):
    c, t, key, db, settings = intake
    task = freeze(c, t, add_image(c, t, new(c, t)))
    signed = assertion(task, key)
    receipt = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": signed},
        "confirm",
    )
    disabled = settings.model_copy(update={"intake_enabled": False})
    with TestClient(
        create_app(disabled, database=db), base_url="http://localhost"
    ) as closed:
        call(
            closed,
            t,
            "POST",
            "/v1/intakes",
            {"origin_scope": str(uuid4())},
            key=str(uuid4()),
            status=503,
        )
        replay = call(
            closed,
            t,
            "POST",
            f"/v1/intakes/{task['task_id']}/decisions",
            {"assertion": signed},
            "confirm",
        )
        assert replay == receipt
        g = call(closed, t, "GET", f"/v1/garments/{receipt['garment_id']}")["garment"]
        r = closed.get(g["image_references"][0]["content_url"], headers=headers(t))
        assert r.content == image_bytes()


def test_migration_preserves_old_rows_and_defaults_identity_empty(
    tmp_path, monkeypatch
):
    from sqlalchemy import text

    from alembic import command
    from wardrobe.db import Database

    db = Database(tmp_path / "migration.sqlite")
    real = command.upgrade
    monkeypatch.setattr(
        command, "upgrade", lambda config, _target: real(config, "0007_managed_renewal")
    )
    db.migrate()
    with db.write() as s:
        s.execute(text("INSERT INTO owners(id) VALUES (:owner)"), {"owner": OWNER_A})
        s.execute(
            text(
                "INSERT INTO garments(id,owner_id,name,category,roles,availability,state,revision,annotation_fields,annotation_review_state,created_at) VALUES (:id,:owner,:name,:category,:roles,:availability,:state,1,:fields,:review,:created)"
            ),
            {
                "id": str(uuid4()),
                "owner": OWNER_A,
                "name": "Old synthetic garment",
                "category": "长裤",
                "roles": '["lower"]',
                "availability": "unknown",
                "state": "active",
                "fields": '{"thickness":{"value":"thick","source_kind":"user_supplied","verification_state":"user_verified"}}',
                "review": "published",
                "created": int(time.time()),
            },
        )
        before = s.execute(text("SELECT * FROM garments")).first()
    monkeypatch.setattr(command, "upgrade", real)
    db.migrate()
    db.assert_initialized()
    with db.sessions() as s:
        after = s.execute(text("SELECT * FROM garments")).first()
        assert tuple(after[:-1]) == tuple(before)
        assert after[-1] == "{}"
        assert s.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert s.execute(text("PRAGMA foreign_key_check")).first() is None
    db.close()


@pytest.mark.parametrize(
    "evidence",
    [
        {"lines": [{}] * 201},
        {"coordinate_system": "normalized-bottom-left-oriented"},
        {
            "lines": [
                {"text": "synthetic", "bbox": [0.9, 0.1, 0.5, 0.5], "candidates": []}
            ]
        },
        {
            "lines": [
                {
                    "text": "synthetic",
                    "bbox": [0, 0, 1, 1],
                    "candidates": [{"text": "x", "confidence": 2}],
                }
            ]
        },
        {
            "lines": [
                {
                    "text": "synthetic",
                    "bbox": [0, 0, 1, 1],
                    "candidates": [{"text": "x", "confidence": 1}] * 4,
                }
            ]
        },
    ],
)
def test_ocr_bounds_fail_closed(evidence):
    from wardrobe.errors import BusinessError
    from wardrobe.intake_sources import validate_ocr_geometry

    with pytest.raises(BusinessError) as error:
        validate_ocr_geometry(evidence)
    assert error.value.code == "OCR_UNCERTAIN"


def test_duplicate_old_asset_with_different_client_view_keeps_old_preview(intake):
    from wardrobe.image_previews import PreviewStore, warm_previews
    from wardrobe.models import Asset, GarmentAsset, OwnerAsset

    c, t, key, db, settings = intake
    raw = image_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    aid = str(uuid4())
    gid = str(uuid4())
    path = settings.data_root / "assets" / "original" / sha
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(raw)
    with db.write() as s:
        s.add(
            Asset(
                id=aid,
                sha256=sha,
                size=len(raw),
                mime="image/jpeg",
                width=100,
                height=80,
            )
        )
        s.flush()
        s.add(OwnerAsset(owner_id=OWNER_A, asset_id=aid))
        s.add(
            Garment(
                id=gid,
                owner_id=OWNER_A,
                name="Existing legacy garment",
                category="shirt",
                roles=["upper"],
                availability="unknown",
                state="active",
                revision=1,
                annotation_fields={},
                annotation_review_state="initial",
                created_at=int(time.time()),
            )
        )
        s.flush()
        s.add(GarmentAsset(garment_id=gid, asset_id=aid))
    expected = PreviewStore(settings.data_root).get(sha).read_bytes()
    task = freeze(c, t, add_image(c, t, new(c, t), source=raw, view=image_bytes("red")))
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": assertion(task, key)},
        "confirm",
    )
    preview = c.get(f"/v1/assets/{aid}/preview", headers=headers(t))
    assert preview.status_code == 200 and preview.content == expected
    assert path.read_bytes() == raw
    report = warm_previews(db, settings.data_root, OWNER_A)
    assert report["failed"] == 0 and report["existing"] >= 1
    g = call(c, t, "GET", f"/v1/garments/{gid}")["garment"]
    assert "source_reference" not in g["image_references"][0]


def test_remove_first_then_fill_available_slot_and_reject_old_confirmation(intake):
    c, t, key, *_ = intake
    task = add_image(c, t, new(c, t), ordinal=1)
    task = add_image(c, t, task, ordinal=2, source=image_bytes("white"))
    task = freeze(c, t, task)
    old_assertion = assertion(task, key)
    removed = task["images"][0]["image_id"]
    task = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/images/{removed}/remove",
        {"expected_revision": task["revision"]},
        "upload",
    )
    assert [im["ordinal"] for im in task["images"]] == [2]
    assert task["state"] == "review" and "payload_digest" not in task
    # Reproduce the old client's count+1 collision before exercising the fix.
    with pytest.raises(AssertionError, match="REVISION_CONFLICT"):
        add_image(
            c, t, task, ordinal=len(task["images"]) + 1, source=image_bytes("red")
        )
    # Slot allocation must fill the hole, not derive 2 from remaining count.
    task = add_image(c, t, task, ordinal=1, source=image_bytes("red"))
    assert [im["ordinal"] for im in task["images"]] == [1, 2]
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": old_assertion},
        "confirm",
        status=409,
    )


def test_ocr_prefix_is_not_a_label_and_repeat_result_does_not_invalidate_freeze(intake):
    from sqlalchemy import func, select

    from wardrobe.models import IntakeObservation

    c, t, *_rest = intake
    db = _rest[1]
    task = add_image(c, t, new(c, t))
    task = add_image(c, t, task, "label", 2, image_bytes("white"))
    label = task["images"][1]
    payload = {
        "expected_revision": task["revision"],
        "image_revision": task["image_revision"],
        "input_fingerprint": task["input_fingerprint"],
        "producer": "apple_vision",
        "schema_version": "wardrobe-label-ocr/1",
        "fields": {"brand": "Test Brand"},
        "ocr": {
            "image_id": label["image_id"],
            "source_hash": label["source_sha256"],
            "view_hash": label["view_sha256"],
            "raw_text": "NotBrand: Test Brand",
        },
    }
    path = f"/v1/intakes/{task['task_id']}/observations"
    call(c, t, "POST", path, payload, "upload", status=422)
    payload["ocr"]["raw_text"] = "Brand: Test Brand"
    task = call(c, t, "POST", path, payload, "upload")
    task = freeze(c, t, task)
    # Simulate a lost response: either the original revision or a refreshed one
    # can request the exact same observation without adding rows or revisions.
    assert call(c, t, "POST", path, payload, "upload") == task
    payload["expected_revision"] = task["revision"]
    assert call(c, t, "POST", path, payload, "upload") == task
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(IntakeObservation)) == 1
    removed = call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/images/{label['image_id']}/remove",
        {"expected_revision": task["revision"]},
        "upload",
    )
    assert "brand" not in removed["fields"] and "brand" not in removed["field_sources"]
    assert removed["fields"]["thickness"] == "thin"  # Actual user facts survive.
    call(c, t, "POST", path, payload, "upload", status=409)


def test_submitted_backup_inventory_restores_source_and_view_and_detects_corruption(
    intake, tmp_path, monkeypatch
):
    import copy
    import shutil

    from wardrobe.intake_backup import build, verify
    from wardrobe.manifest import reconcile

    c, t, key, db, settings = intake
    task = add_image(
        c, t, new(c, t), source=image_bytes("blue"), view=image_bytes("red")
    )
    task = add_image(c, t, task, "label", 2, image_bytes("white"))
    task = freeze(c, t, task)
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": assertion(task, key)},
        "confirm",
    )
    draft = add_image(c, t, new(c, t), source=image_bytes("green"))
    manifest = build(db, settings.data_root)
    import json
    import stat
    import sys

    from wardrobe.cli import main

    manifest_path = tmp_path / "backup.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wardrobectl",
            "--data-root",
            str(settings.data_root),
            "intake-backup-manifest",
            "--output",
            str(manifest_path),
        ],
    )
    main()
    assert json.loads(manifest_path.read_text()) == manifest
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert len(manifest["entries"]) == 4
    assert not any(
        x["sha256"] == draft["images"][0]["source_sha256"] for x in manifest["entries"]
    )
    restored = tmp_path / "restored"
    for entry in manifest["entries"]:
        target = restored / entry["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings.data_root / entry["relative_path"], target)
    assert verify(restored, manifest)["ok"]
    report_path = tmp_path / "restore-verification.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wardrobectl",
            "--data-root",
            str(restored),
            "verify-intake-backup",
            "--manifest",
            str(manifest_path),
            "--output",
            str(report_path),
        ],
    )
    main()
    assert json.loads(report_path.read_text())["ok"]
    assert not (restored / "wardrobe.sqlite").exists()
    view = next(
        x
        for x in manifest["entries"]
        if x["relative_path"].startswith("intake/objects/")
    )
    (restored / view["relative_path"]).write_bytes(b"corrupt synthetic view")
    assert not verify(restored, manifest)["ok"]
    (settings.data_root / view["relative_path"]).unlink()
    assert not reconcile(db, settings.data_root)["submitted_intake_media"]["ok"]
    with pytest.raises(ValueError, match="integrity"):
        build(db, settings.data_root)
    malicious = copy.deepcopy(manifest)
    malicious["entries"][0]["relative_path"] = "../outside"
    with pytest.raises(ValueError):
        verify(restored, malicious)


def test_reconcile_keeps_legacy_report_when_submitted_view_metadata_is_missing(
    intake, tmp_path, monkeypatch
):
    import json
    import sys

    from sqlalchemy import delete, select

    from wardrobe.cli import main
    from wardrobe.intake_backup import build
    from wardrobe.manifest import reconcile
    from wardrobe.models import Asset, IntakeUpload

    c, t, key, db, settings = intake
    task = freeze(c, t, add_image(c, t, new(c, t)))
    call(
        c,
        t,
        "POST",
        f"/v1/intakes/{task['task_id']}/decisions",
        {"assertion": assertion(task, key)},
        "confirm",
    )
    # An unrelated legacy asset must still have a usable integrity result.
    content = image_bytes("purple")
    sha = hashlib.sha256(content).hexdigest()
    legacy_id = str(uuid4())
    path = settings.data_root / "assets" / "original" / sha
    path.write_bytes(content)
    with db.write() as session:
        session.add(
            Asset(
                id=legacy_id,
                sha256=sha,
                size=len(content),
                mime="image/jpeg",
                width=16,
                height=16,
            )
        )
        session.execute(
            delete(IntakeUpload).where(
                IntakeUpload.task_id == task["task_id"], IntakeUpload.kind == "view"
            )
        )
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for folder in ("assets/original", "intake/objects")
        for p in (settings.data_root / folder).iterdir()
        if p.is_file()
    }
    with pytest.raises(ValueError, match="metadata is incomplete"):
        build(db, settings.data_root)
    report = reconcile(db, settings.data_root)
    assert {"asset_id": legacy_id, "state": "intact"} in report["assets"]
    section = report["submitted_intake_media"]
    assert section["ok"] is False and section["repaired"] is False
    assert section["error"] == "SUBMITTED_INTAKE_METADATA_INVALID"
    assert section["checked"] == 0 and not report["repaired"]
    output = tmp_path / "reconciliation.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wardrobectl",
            "--data-root",
            str(settings.data_root),
            "reconcile",
            "--output",
            str(output),
        ],
    )
    main()
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o077 == 0
    assert all(
        (p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in before.items()
    )
    with db.sessions.begin() as session:
        assert (
            session.scalar(
                select(IntakeUpload).where(
                    IntakeUpload.task_id == task["task_id"], IntakeUpload.kind == "view"
                )
            )
            is None
        )
