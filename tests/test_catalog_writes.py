import base64
import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from conftest import OWNER_A, OWNER_B, TOKEN_A, TOKEN_B
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from PIL import Image
from sqlalchemy import func, select
from test_foundation import headers, rpc

from wardrobe.auth import token_hash
from wardrobe.cli import grant_client
from wardrobe.models import (
    Asset,
    ClientGrant,
    Garment,
    HumanDecision,
    TrustedIssuer,
)
from wardrobe.writes import canonical

HUMAN_TOKEN = "synthetic-human-adapter-token-" + "h" * 40


def enable_write(db):
    # Acquire the write lock before reading; idle workers may otherwise turn
    # this setup transaction into a failed SQLite snapshot upgrade.
    with db.write() as session:
        session.get(ClientGrant, token_hash(TOKEN_A)).scopes = [
            "wardrobe.read",
            "wardrobe.create",
            "wardrobe.propose_change",
        ]


def png(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, content, key="upload-1"):
    prepared = client.post(
        "/v1/asset-uploads",
        headers={**headers(), "Idempotency-Key": key},
        json={"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
    )
    assert prepared.status_code == 200, prepared.text
    info = prepared.json()["data"]
    accepted = client.put(
        info["upload_url"],
        headers={**headers(), "X-Wardrobe-Upload-Token": info["upload_token"]},
        content=content,
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["data"]["asset_id"]


def create(client, asset_id, key="create-1", roles=None):
    response = client.post(
        "/v1/garments",
        headers={**headers(), "Idempotency-Key": key},
        json={
            "name": "Synthetic new garment",
            "category": "shirt",
            "roles": roles if roles is not None else ["upper"],
            "asset_ids": [asset_id],
            "annotation_fields": {"material": None},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def human_setup(db):
    from wardrobe.config import Settings

    settings = Settings(data_root=db.path.parent)
    grant_client(
        db,
        settings,
        owner_id=OWNER_A,
        client_id="human-ui",
        scopes=["human.decision"],
        token=HUMAN_TOKEN,
        channel="human_adapter",
    )
    key = Ed25519PrivateKey.generate()
    with db.sessions.begin() as session:
        session.add(
            TrustedIssuer(
                issuer="synthetic-ui",
                kid="key-1",
                public_key=key.public_key()
                .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
                .decode(),
                bindings=[
                    {
                        "sub": "wardrobe-owner-a",
                        "owner_id": OWNER_A,
                        "client_id": "human-ui",
                    }
                ],
                purposes=["decision"],
                audience="open-wardrobe:human-decisions",
                expires_at=int(time.time()) + 3600,
                revoked_at=None,
            )
        )
    return key


def propose(
    client,
    garment_id,
    operation="update",
    patch=None,
    revision=1,
    key="proposal-1",
    extra_changes=None,
):
    change = {
        "target_id": garment_id,
        "base_revision": revision,
        "operation": operation,
        "patch": patch or {},
    }
    response = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": key},
        json={"changes": [change] + (extra_changes or [])},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def assertion(key, proposal, **overrides):
    now = int(time.time())
    payload = {
        "version": "wardrobe-human/1",
        "iss": "synthetic-ui",
        "sub": "wardrobe-owner-a",
        "owner_id": OWNER_A,
        "client_id": "human-ui",
        "actor_subject": "device:synthetic",
        "aud": "open-wardrobe:human-decisions",
        "purpose": "decision",
        "proposal_id": proposal["proposal_id"],
        "digest": proposal["digest"],
        "revisions": {c["target_id"]: c["base_revision"] for c in proposal["changes"]},
        "nonce": proposal["nonce"],
        "jti": str(uuid4()),
        "decision": "approve",
        "iat": now,
        "exp": now + 120,
        **overrides,
    }

    def encode(value):
        return base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")

    head, body = (
        encode({"alg": "EdDSA", "typ": "wardrobe-human-v1", "kid": "key-1"}),
        encode(payload),
    )
    signed = key.sign((head + "." + body).encode())
    return (
        head + "." + body + "." + base64.urlsafe_b64encode(signed).decode().rstrip("=")
    )


def decide(client, signed, token=HUMAN_TOKEN):
    return client.post(
        "/v1/human-decisions", headers=headers(token), json={"assertion": signed}
    )


def test_upload_create_idempotency_asset_dedup_and_owner(live_service):
    client, db = live_service
    enable_write(db)
    original = png()
    asset = upload(client, original)
    first = create(client, asset)
    again = create(client, asset)
    assert first == again
    second = create(client, asset, key="different-physical-garment")
    assert second["data"]["garment_id"] != first["data"]["garment_id"]
    assert second["data"]["possible_duplicate"]
    assert upload(client, original, key="upload-again") == asset
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
    content = client.get(f"/v1/assets/{asset}/content", headers=headers())
    assert content.content == original
    assert content.headers["cache-control"] == "private, no-store"
    assert client.get(f"/v1/assets/{asset}/content").status_code == 401
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers(TOKEN_B)).status_code
        == 404
    )
    details = client.get(
        f"/v1/garments/{first['data']['garment_id']}", headers=headers()
    ).json()["data"]["garment"]
    assert details["recommendation_eligible"]
    without_role = create(client, asset, key="no-role", roles=[])
    details = client.get(
        f"/v1/garments/{without_role['data']['garment_id']}", headers=headers()
    ).json()["data"]["garment"]
    assert (
        not details["recommendation_eligible"]
        and "roles" in details["missing_requirements"]
    )
    conflict = client.post(
        "/v1/garments",
        headers={**headers(), "Idempotency-Key": "create-1"},
        json={"name": "Different", "asset_ids": [asset]},
    )
    assert conflict.status_code == 409


def test_invalid_image_and_wrong_checksum_are_not_ready(live_service):
    client, db = live_service
    enable_write(db)
    corrupt = b"not an image"
    prepared = client.post(
        "/v1/asset-uploads",
        headers={**headers(), "Idempotency-Key": "bad-image"},
        json={"sha256": hashlib.sha256(corrupt).hexdigest(), "size": len(corrupt)},
    ).json()["data"]
    assert (
        client.put(
            prepared["upload_url"],
            headers={**headers(), "X-Wardrobe-Upload-Token": prepared["upload_token"]},
            content=corrupt,
        ).status_code
        == 422
    )
    assert (
        client.put(
            prepared["upload_url"],
            headers={**headers(), "X-Wardrobe-Upload-Token": "forged"},
            content=corrupt,
        ).status_code
        == 403
    )
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 0


def test_human_confirm_replay_reject_and_scope(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    garment = str(UUID(int=10))
    proposal = propose(client, garment, patch={"name": "Confirmed new name"})
    signed = assertion(key, proposal)
    assert decide(client, signed, token=TOKEN_A).status_code == 403
    rejected = decide(client, assertion(key, proposal, sub="unregistered-subject"))
    assert rejected.status_code == 403
    forged = decide(client, signed[:-8] + "AAAAAAAA")
    assert forged.status_code == 403
    approved = decide(client, signed)
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["state"] == "executed"
    assert decide(client, signed).json()["data"] == approved.json()["data"]
    assert (
        client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
            "garment"
        ]["name"]
        == "Confirmed new name"
    )
    second = propose(
        client,
        garment,
        patch={"name": "Should stay unchanged"},
        revision=2,
        key="reject-this",
    )
    assert (
        decide(client, assertion(key, second, decision="reject")).json()["data"][
            "state"
        ]
        == "rejected"
    )
    assert (
        client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
            "garment"
        ]["name"]
        == "Confirmed new name"
    )
    assert rpc(client, "tools/list", token=HUMAN_TOKEN).status_code == 403


def test_batch_revision_conflict_rolls_back_and_concurrent_replay(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    one, two = str(UUID(int=10)), str(UUID(int=11))
    batch = propose(
        client,
        one,
        patch={"name": "Batch name"},
        extra_changes=[
            {
                "target_id": two,
                "base_revision": 1,
                "operation": "update",
                "patch": {"name": "Batch name"},
            }
        ],
    )
    with db.sessions.begin() as session:
        session.get(Garment, two).revision = 2
    assert decide(client, assertion(key, batch)).status_code == 409
    assert (
        client.get(f"/v1/garments/{one}", headers=headers()).json()["data"]["garment"][
            "name"
        ]
        == "Synthetic 10"
    )
    proposal = propose(client, one, patch={"name": "Once"}, key="single")
    signed = assertion(key, proposal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(client, signed), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert results[0].json()["data"] == results[1].json()["data"]
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HumanDecision)) == 1
        assert session.get(Garment, one).revision == 2


def test_logical_delete_shared_asset_and_restore(live_service):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    first = create(client, asset)["data"]["garment_id"]
    second = create(client, asset, key="second")["data"]["garment_id"]
    remove = propose(client, first, "delete", key="delete-first")
    assert decide(client, assertion(key, remove)).status_code == 200
    assert client.get(f"/v1/garments/{first}", headers=headers()).status_code == 404
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 200
    )
    remove = propose(client, second, "delete", key="delete-second")
    assert decide(client, assertion(key, remove)).status_code == 200
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 404
    )
    restore = propose(client, first, "restore", revision=2, key="restore-first")
    assert decide(client, assertion(key, restore)).status_code == 200
    assert (
        client.get(f"/v1/assets/{asset}/content", headers=headers()).status_code == 200
    )


def test_mcp_raw_arguments_are_strict(live_service):
    client, _db = live_service
    for args in [{"limit": "2"}, {"limit": True}, {"owner_id": OWNER_B}]:
        result = rpc(
            client, "tools/call", {"name": "wardrobe.list_garments", "arguments": args}
        ).json()["result"]
        assert (
            result["isError"]
            and result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENT"
        )
