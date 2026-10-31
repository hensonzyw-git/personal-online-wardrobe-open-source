import base64
import time
from uuid import UUID, uuid4

import pytest
from conftest import OWNER_A, TOKEN_A
from pydantic import ValidationError
from sqlalchemy import func, select
from test_catalog_writes import (
    HUMAN_TOKEN,
    assertion,
    create,
    decide,
    enable_write,
    human_setup,
    png,
    propose,
    upload,
)
from test_foundation import headers, rpc

from wardrobe.annotations import VisionOutput
from wardrobe.auth import token_hash
from wardrobe.models import (
    AnnotationVersion,
    Asset,
    ClientGrant,
    HistoryAmendment,
    HistoryEvent,
    Recommendation,
    TrustedIssuer,
    Upload,
)
from wardrobe.writes import canonical


def test_draft_publication_and_specific_factual_verification(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    suggestion = {
        "garment_id": garment,
        "base_revision": 1,
        "values": {"primary_color": "red", "waterproof": True},
    }
    saved = client.post(
        "/v1/annotation-suggestions",
        headers={**headers(), "Idempotency-Key": "suggestion"},
        json=suggestion,
    )
    assert saved.status_code == 200, saved.text
    assert not saved.json()["data"]["effective"]
    version = saved.json()["data"]["annotation_version_id"]
    details = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]
    assert details["annotation_fields"]["primary_color"]["value"] is None
    review = client.get("/v1/annotation-review", headers=headers()).json()["data"]
    assert review["items"][0]["annotation_version_id"] == version
    publish = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "publish"},
        json={
            "changes": [
                {
                    "target_id": garment,
                    "base_revision": 1,
                    "operation": "publish_annotation",
                    "patch": {"annotation_version_id": version},
                }
            ]
        },
    ).json()["data"]
    assert (
        publish["changes"][0]["before"]["annotation_draft"]["waterproof"][
            "verification_state"
        ]
        == "unverified"
    )
    assert decide(client, assertion(key, publish)).status_code == 200
    details = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]
    assert (
        details["annotation_fields"]["waterproof"]["verification_state"] == "unverified"
    )
    verify = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "verify"},
        json={
            "changes": [
                {
                    "target_id": garment,
                    "base_revision": 2,
                    "operation": "verify_fields",
                    "patch": {"verified_fields": ["waterproof"]},
                }
            ]
        },
    ).json()["data"]
    assert decide(client, assertion(key, verify)).status_code == 200
    details = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]
    assert (
        details["annotation_fields"]["waterproof"]["verification_state"]
        == "user_verified"
    )
    assert (
        details["annotation_fields"]["primary_color"]["verification_state"]
        == "unverified"
    )
    with db.sessions.begin() as session:
        assert session.get(AnnotationVersion, version).status == "published"


def test_strict_labels_duplicate_json_and_large_control_request(live_service):
    client, db = live_service
    enable_write(db)
    asset = upload(client, png())
    for labels in [
        {"waterproof": {"value": True, "verification_state": "user_verified"}},
        {"brand": 123},
        {"unknown_field": "made up"},
    ]:
        result = client.post(
            "/v1/garments",
            headers={**headers(), "Idempotency-Key": "invalid"},
            json={
                "name": "Synthetic",
                "asset_ids": [asset],
                "annotation_fields": labels,
            },
        )
        assert result.status_code == 422
    duplicate = client.post(
        "/v1/garments",
        headers={**headers(), "Content-Type": "application/json"},
        content=b'{"name":"a","name":"b","asset_ids":[]}',
    )
    assert duplicate.status_code == 422
    oversized = client.post(
        "/v1/garments", headers=headers(), content=b"x" * (1024 * 1024 + 1)
    )
    assert oversized.status_code == 413
    base = {
        "schema_version": "wardrobe-labels/1",
        "model_version": "fixture",
        "prompt_version": "fixture-v1",
        "values": {"primary_color": "red"},
        "observed_fields": ["primary_color"],
        "evidence_asset_ids": [asset],
        "multi_subject": False,
    }
    assert VisionOutput.model_validate(base).values.primary_color == "red"
    for values in [
        {"composition": "100% nylon"},
        {"brand": "Guessed Brand"},
        {"waterproof": True},
    ]:
        with pytest.raises(ValidationError):
            VisionOutput.model_validate(
                {**base, "values": values, "observed_fields": []}
            )


def review_assertion(key, proposal, garment, asset, **overrides):
    now = int(time.time())

    def encode(value):
        return base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")

    head = encode({"alg": "EdDSA", "typ": "wardrobe-human-v1", "kid": "key-1"})
    body = encode(
        {
            "version": "wardrobe-review/1",
            "iss": "synthetic-ui",
            "sub": "wardrobe-owner-a",
            "owner_id": OWNER_A,
            "client_id": "human-ui",
            "actor_subject": "device:synthetic",
            "aud": "open-wardrobe:human-decisions",
            "purpose": "review_preview",
            "proposal_id": proposal["proposal_id"],
            "digest": proposal["digest"],
            "nonce": proposal["nonce"],
            "garment_id": garment,
            "target_revision": 2,
            "asset_id": asset,
            "iat": now,
            "exp": now + 120,
            **overrides,
        }
    )
    signed = key.sign((head + "." + body).encode())
    return (
        head + "." + body + "." + base64.urlsafe_b64encode(signed).decode().rstrip("=")
    )


def test_removed_image_preview_is_bound_to_active_restore_review(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    with db.write() as session:
        grant = session.get(ClientGrant, token_hash(HUMAN_TOKEN))
        grant.scopes = [*grant.scopes, "human.preview"]
        session.get(TrustedIssuer, ("synthetic-ui", "key-1")).purposes = [
            "decision",
            "review_preview",
        ]
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    remove = propose(client, garment, "delete")
    assert decide(client, assertion(key, remove)).status_code == 200
    restore = propose(client, garment, "restore", revision=2, key="restore")
    signed = review_assertion(key, restore, garment, asset)
    endpoint = f"/v1/change-proposals/{restore['proposal_id']}/preview-grants"
    assert (
        client.post(endpoint, headers=headers(), json={"assertion": signed}).status_code
        == 403
    )
    granted = client.post(
        endpoint, headers=headers(HUMAN_TOKEN), json={"assertion": signed}
    )
    assert granted.status_code == 200, granted.text
    path = granted.json()["data"]["content_url"]
    assert (
        client.get(
            path, headers={**headers(), "X-Wardrobe-Review-Assertion": signed}
        ).status_code
        == 403
    )
    assert (
        client.get(
            path,
            headers={**headers(HUMAN_TOKEN), "X-Wardrobe-Review-Assertion": signed},
        ).content
        == png()
    )
    expired = review_assertion(
        key,
        restore,
        garment,
        asset,
        iat=int(time.time()) - 200,
        exp=int(time.time()) - 1,
    )
    assert (
        client.get(
            path,
            headers={**headers(HUMAN_TOKEN), "X-Wardrobe-Review-Assertion": expired},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/change-proposals/{restore['proposal_id']}/cancel", headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.get(
            path,
            headers={**headers(HUMAN_TOKEN), "X-Wardrobe-Review-Assertion": signed},
        ).status_code
        == 409
    )
    assert decide(client, assertion(key, restore)).status_code == 409
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 404
    )


def test_history_revocation_requires_human_and_preserves_amendment(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    now = int(time.time())
    event_id, rec_id = str(uuid4()), str(uuid4())
    with db.write() as session:
        session.add(
            Recommendation(
                id=rec_id,
                owner_id=OWNER_A,
                client_id="client-a",
                grant_id=token_hash(TOKEN_A),
                payload={},
                state="succeeded",
                created_at=now,
                deadline=now + 120,
                attempts=1,
                result={"outfits": []},
            )
        )
        session.flush()
        session.add(
            HistoryEvent(
                id=event_id,
                owner_id=OWNER_A,
                recommendation_id=rec_id,
                outfit_id=str(uuid4()),
                kind="worn",
                occurred_at=now,
                payload={"signature": "synthetic-signature", "garment_ids": []},
                revision=1,
                active=True,
            )
        )
    proposal = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "revoke-event"},
        json={
            "changes": [
                {
                    "target_type": "history",
                    "target_id": event_id,
                    "base_revision": 1,
                    "operation": "revoke_history",
                    "patch": {},
                }
            ]
        },
    ).json()["data"]
    assert decide(client, assertion(key, proposal), token=TOKEN_A).status_code == 403
    assert (
        client.get("/v1/history", headers=headers()).json()["data"]["items"][0]["kind"]
        == "worn"
    )
    assert decide(client, assertion(key, proposal)).status_code == 200
    assert not client.get("/v1/history", headers=headers()).json()["data"]["items"]
    with db.sessions.begin() as session:
        assert not session.get(HistoryEvent, event_id).active
        assert session.get(HistoryEvent, event_id).revision == 2
        assert session.scalar(select(func.count()).select_from(HistoryAmendment)) == 1


def test_upload_expiry_and_malformed_confirmation(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    with db.write() as session:
        row = session.scalar(select(Upload).where(Upload.asset_id == asset))
        row.expires_at = int(time.time()) - 1
        upload_id = row.id
    result = client.get(f"/v1/asset-uploads/{upload_id}", headers=headers())
    assert result.status_code == 200 and result.json()["data"]["state"] == "ready"
    assert (
        client.put(
            f"/v1/asset-uploads/{upload_id}/content",
            headers={**headers(), "X-Wardrobe-Upload-Token": "invalid"},
            content=png(),
        ).status_code
        == 403
    )
    garment = str(UUID(int=10))
    proposal = propose(client, garment, patch={"name": "New"})
    signed = assertion(key, proposal)
    malformed_header = (
        base64.urlsafe_b64encode(
            canonical({"alg": "EdDSA", "typ": "wardrobe-human-v1", "kid": []})
        )
        .decode()
        .rstrip("=")
    )
    assert (
        decide(client, malformed_header + "." + signed.split(".", 1)[1]).status_code
        == 403
    )
    result = rpc(client, "tools/list").json()["result"]
    names = {tool["name"] for tool in result["tools"]}
    assert "wardrobe.confirm" not in names and "wardrobe.human_decision" not in names
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1


def test_vision_evidence_stays_unverified_through_publication(live_service):
    from wardrobe.annotations import AnnotationService
    from wardrobe.auth import Authenticator
    from wardrobe.catalog import CatalogService
    from wardrobe.db import load_cursor_key

    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    output = VisionOutput.model_validate(
        {
            "schema_version": "wardrobe-labels/1",
            "model_version": "synthetic-fixture-v1",
            "prompt_version": "synthetic-v1",
            "values": {
                "primary_color": "red",
                "texture": "smooth",
                "style": ["casual"],
            },
            "observed_fields": ["primary_color", "texture"],
            "evidence_asset_ids": [asset],
            "multi_subject": False,
        }
    )
    service = AnnotationService(
        db, CatalogService(db.sessions, load_cursor_key(db.path.parent))
    )
    principal = Authenticator(db.sessions, "open-wardrobe").authenticate(TOKEN_A)
    version = service.save_vision_output(principal, garment, 1, output, "vision-v1")[
        "data"
    ]["annotation_version_id"]
    proposal = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "publish-vision"},
        json={
            "changes": [
                {
                    "target_id": garment,
                    "base_revision": 1,
                    "operation": "publish_annotation",
                    "patch": {"annotation_version_id": version},
                }
            ]
        },
    ).json()["data"]
    assert decide(client, assertion(key, proposal)).status_code == 200
    labels = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]["annotation_fields"]
    assert labels["primary_color"]["source_kind"] == "observed"
    assert labels["style"]["source_kind"] == "inferred"
    assert labels["style"]["verification_state"] == "unverified"
    assert labels["composition"]["value"] is None
    assert labels["texture"]["evidence"][0]["asset_id"] == asset


def test_database_errors_are_redacted_across_api_and_mcp(live_service, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from wardrobe.catalog import CatalogService

    client, _db = live_service

    def unavailable(*_args):
        raise OperationalError(
            "PRIVATE_SQL_CANARY",
            {"param": "PRIVATE_PARAM_CANARY"},
            RuntimeError("private"),
        )

    monkeypatch.setattr(CatalogService, "list_garments", unavailable)
    api = client.get("/v1/garments", headers=headers())
    assert api.status_code == 503 and "PRIVATE_SQL_CANARY" not in api.text
    result = rpc(
        client, "tools/call", {"name": "wardrobe.list_garments", "arguments": {}}
    )
    assert result.json()["result"]["isError"]
    assert (
        "PRIVATE_SQL_CANARY" not in result.text
        and "PRIVATE_PARAM_CANARY" not in result.text
    )


def test_confirmed_image_detach_removes_read_and_recommendation_eligibility(
    live_service,
):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    changing_label_presence = client.post(
        "/v1/garments",
        headers={**headers(), "Idempotency-Key": "create-1"},
        json={
            "name": "Synthetic new garment",
            "category": "shirt",
            "roles": ["upper"],
            "asset_ids": [asset],
            "annotation_fields": {},
        },
    )
    assert changing_label_presence.status_code == 409
    proposal = propose(client, garment, patch={"asset_ids": []}, key="detach-image")
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 200
    )
    assert decide(client, assertion(key, proposal)).status_code == 200
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 404
    )
    details = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]
    assert (
        not details["recommendation_eligible"]
        and "image" in details["missing_requirements"]
    )


def test_consumed_confirmation_returns_receipt_after_assertion_expiry(
    live_service, monkeypatch
):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    proposal = propose(client, str(UUID(int=10)), patch={"name": "Once"})
    signed = assertion(key, proposal)
    other = propose(
        client,
        str(UUID(int=11)),
        patch={"name": "Must stay unchanged"},
        key="unconsumed",
    )
    other_signed = assertion(key, other)
    approved = decide(client, signed)
    assert approved.status_code == 200
    future = time.time() + 180
    monkeypatch.setattr("wardrobe.changes.time.time", lambda: future)
    assert decide(client, signed).json()["data"] == approved.json()["data"]
    assert decide(client, other_signed).status_code == 403
