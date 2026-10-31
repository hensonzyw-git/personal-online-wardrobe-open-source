import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from conftest import OWNER_A, TOKEN_A, TOKEN_B
from sqlalchemy import create_engine, func, select, text
from test_catalog_writes import (
    assertion,
    decide,
    enable_write,
    human_setup,
    png,
    upload,
)
from test_foundation import headers, rpc

from alembic import command
from wardrobe.auth import token_hash
from wardrobe.db import Database
from wardrobe.models import ClientGrant, Garment, ProductDraft, ServiceState


def synthetic_product():
    return {
        "provider": "synthetic",
        "region": "US",
        "global_spu_id": "10001",
        "ids": {"globalSpuId": "10001", "regionSpuId": "20001", "dwSpuId": "30001"},
        "article_number": "TEST-001",
        "brand": "Synthetic Brand",
        "title": "Synthetic hoodie 黄色",
        "category": "hoodie",
        "image_url": None,
        "skus": [
            {
                "global_sku_id": "40001",
                "ids": {"globalSkuId": "40001", "regionSkuId": "50001"},
                "attributes": {"颜色": "黄色", "尺码": "M"},
            },
            {
                "global_sku_id": "40002",
                "ids": {"globalSkuId": "40002", "regionSkuId": "50002"},
                "attributes": {"颜色": "黄色", "尺码": "L"},
            },
        ],
    }


class FakeProvider:
    def __init__(self):
        self.product = synthetic_product()
        self.detail_calls = 0
        self.on_detail = None

    def lookup(self, article, page, limit):
        return {"items": [{"global_spu_id": "10001"}], "page": page, "has_more": False}

    def detail(self, _identifier):
        self.detail_calls += 1
        if self.on_detail:
            self.on_detail()
        return copy.deepcopy(self.product)


@pytest.fixture
def providers():
    return {"product_provider": FakeProvider()}


def enable_product(db):
    enable_write(db)
    with db.sessions.begin() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_A))
        grant.scopes = [*grant.scopes, "wardrobe.annotate"]


def draft_input(client):
    return {
        "article_number": "TEST-001",
        "global_spu_id": "10001",
        "owned_size": "M",
        "roles": ["upper"],
        "asset_ids": [upload(client, png("green"), "physical")],
        "screenshots": [
            {
                "asset_id": upload(client, png("blue"), "detail"),
                "kind": "product_detail",
            },
            {
                "asset_id": upload(client, png("red"), "parameters"),
                "kind": "product_parameters",
                "article_number": "TEST-001",
                "values": {
                    "material": "cotton",
                    "composition": "100% cotton",
                    "thickness": "medium",
                },
                "parameters": {"release_price": "1575", "fleece": "no"},
            },
        ],
    }


def prepare(client, payload, key="draft-1"):
    return client.post(
        "/v1/product-drafts",
        headers={**headers(), "Idempotency-Key": key},
        json=payload,
    )


def proposal(client, draft_id, key="publication-1"):
    return client.post(
        f"/v1/product-drafts/{draft_id}/proposal",
        headers={**headers(), "Idempotency-Key": key},
    )


def test_http_mcp_signed_publication_and_idempotency(live_service, providers):
    client, db = live_service
    enable_product(db)
    signing_key = human_setup(db)
    inputs = draft_input(client)
    result = prepare(client, inputs)
    assert result.status_code == 200, result.text
    saved = result.json()
    draft_id = saved["data"]["draft_id"]
    assert saved["data"]["effective"] is False
    assert saved["data"]["draft"]["selected_sku"]["global_sku_id"] == "40001"
    assert prepare(client, inputs).json() == saved
    assert providers["product_provider"].detail_calls == 1
    assert prepare(client, {**inputs, "owned_size": "L"}).status_code == 409
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    names = {x["name"] for x in tools}
    assert {
        "wardrobe.lookup_product",
        "wardrobe.prepare_product_draft",
        "wardrobe.get_product_draft",
        "wardrobe.propose_product_publication",
    } <= names
    assert "wardrobe.human_decision" not in names
    mcp_draft = rpc(
        client,
        "tools/call",
        {"name": "wardrobe.get_product_draft", "arguments": {"draft_id": draft_id}},
    ).json()["result"]
    assert not mcp_draft["isError"]
    assert mcp_draft["structuredContent"]["data"] == saved["data"]
    mcp_proposed = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.propose_product_publication",
            "arguments": {"draft_id": draft_id, "key": "publication-1"},
        },
    ).json()["result"]
    proposed = mcp_proposed["structuredContent"]["data"]
    assert proposed["changes"][0]["before"]["product_draft"] == saved["data"]["draft"]
    with db.sessions.begin() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Garment)
                .where(Garment.owner_id == OWNER_A)
            )
            == 7
        )
    signed = assertion(signing_key, proposed)
    assert decide(client, signed, token=TOKEN_A).status_code == 403
    applied = decide(client, signed)
    assert applied.status_code == 200, applied.text
    assert decide(client, signed).json()["data"] == applied.json()["data"]
    assert proposal(client, draft_id).json()["data"] == proposed
    garment_id = applied.json()["data"]["published_garments"][draft_id]["garment_id"]
    garment = client.get(f"/v1/garments/{garment_id}", headers=headers()).json()[
        "data"
    ]["garment"]
    assert garment["product_reference"]["sku_ids"]["globalSkuId"] == "40001"
    assert len(garment["image_references"]) == 1
    assert (
        garment["annotation_fields"]["material"]["source_kind"]
        == "screenshot_extraction"
    )
    assert garment["annotation_fields"]["brand"]["source_kind"] == "product_api"
    assert garment["annotation_fields"]["size"]["source_kind"] == "caller_supplied"
    assert (
        garment["annotation_fields"]["composition"]["verification_state"]
        == "unverified"
    )
    assert garment["annotation_fields"]["waterproof"]["value"] is None
    assert (
        client.get(f"/v1/product-drafts/{draft_id}", headers=headers()).json()["data"][
            "state"
        ]
        == "published"
    )
    assert proposal(client, draft_id, "other-key").status_code == 409
    second = prepare(client, inputs, "another-physical-item").json()["data"]["draft_id"]
    next_proposal = proposal(client, second, "another-publication").json()["data"]
    next_applied = decide(client, assertion(signing_key, next_proposal))
    assert next_applied.status_code == 200
    assert (
        next_applied.json()["data"]["published_garments"][second]["garment_id"]
        != garment_id
    )


@pytest.mark.parametrize(
    "case,code",
    [
        ("size", "SKU_NOT_FOUND"),
        ("ambiguous", "SKU_AMBIGUOUS"),
        ("article", "PRODUCT_MISMATCH"),
    ],
)
def test_never_picks_first_candidate(live_service, providers, case, code):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    if case == "size":
        payload["owned_size"] = "XL"
    elif case == "ambiguous":
        other = copy.deepcopy(providers["product_provider"].product["skus"][0])
        other["global_sku_id"] = "40003"
        other["ids"] = {"globalSkuId": "40003"}
        other["attributes"]["版本"] = "wide"
        providers["product_provider"].product["skus"].append(other)
    else:
        providers["product_provider"].product["article_number"] = "TEST-OTHER"
    response = prepare(client, payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(ProductDraft)) == 0
    if case == "ambiguous":
        assert len(response.json()["error"]["candidates"]) == 2
        payload["sale_attributes"] = {"版本": "wide"}
        assert prepare(client, payload).status_code == 200


def test_color_conflict_blocks_until_explicit_selection(live_service, providers):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    providers["product_provider"].product["title"] = "Synthetic hoodie 卡其色"
    payload["screenshots"][1]["values"]["primary_color"] = "卡其色"
    blocked = prepare(client, payload).json()["data"]
    assert "color_conflict" in blocked["draft"]["blockers"]
    assert (
        proposal(client, blocked["draft_id"]).json()["error"]["code"]
        == "PRODUCT_DRAFT_NEEDS_REVIEW"
    )
    fixed = prepare(
        client,
        {
            **payload,
            "selected_color": "黄色",
            "color_note": "Platform yellow corresponds to my khaki item",
        },
        "resolved",
    ).json()["data"]
    assert not fixed["draft"]["blockers"]
    assert fixed["draft"]["conflicts"][0]["resolution_note"]
    assert proposal(client, fixed["draft_id"]).status_code == 200


def test_owner_evidence_schema_and_revocation(live_service, providers):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    prepared = prepare(client, payload).json()["data"]
    assert (
        client.get(
            f"/v1/product-drafts/{prepared['draft_id']}", headers=headers(TOKEN_B)
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/product-drafts",
            headers={**headers(TOKEN_B), "Idempotency-Key": "other"},
            json=payload,
        ).status_code
        == 403
    )
    bad = copy.deepcopy(payload)
    bad["screenshots"][1]["article_number"] = "DIFFERENT"
    assert prepare(client, bad, "bad").status_code == 422
    bad = {**payload, "app_secret": "should-never-be-a-contract-field"}
    assert prepare(client, bad, "bad").status_code == 422
    with db.sessions.begin() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_B))
        grant.scopes = [*grant.scopes, "wardrobe.annotate"]
    assert (
        client.post(
            "/v1/product-drafts",
            headers={**headers(TOKEN_B), "Idempotency-Key": "other"},
            json=payload,
        ).json()["error"]["code"]
        == "ASSET_NOT_OWNED"
    )

    def revoke():
        with db.sessions.begin() as session:
            session.get(ClientGrant, token_hash(TOKEN_A)).revoked_at = int(time.time())

    providers["product_provider"].on_detail = revoke
    assert prepare(client, payload, "revoked-inflight").status_code == 401
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(ProductDraft)) == 1


def test_existing_verified_fields_and_stale_revision(live_service):
    client, db = live_service
    enable_product(db)
    key = human_setup(db)
    garment_id = str(UUID(int=10))
    with db.sessions.begin() as session:
        row = session.get(Garment, garment_id)
        row.annotation_fields = {
            "material": {
                "value": "wool",
                "source_kind": "user_verified",
                "verification_state": "user_verified",
            },
            "notes": {
                "value": "Keep this note",
                "source_kind": "caller_supplied",
                "verification_state": "unverified",
            },
        }
        row.availability = "unavailable"
    payload = {**draft_input(client), "garment_id": garment_id, "base_revision": 1}
    draft = prepare(client, payload).json()["data"]
    assert draft["draft"]["preserved_verified_fields"]["material"]["value"] == "wool"
    proposed = proposal(client, draft["draft_id"]).json()["data"]
    assert decide(client, assertion(key, proposed)).status_code == 200
    with db.sessions.begin() as session:
        row = session.get(Garment, garment_id)
        assert row.annotation_fields["material"]["value"] == "wool"
        assert row.annotation_fields["notes"]["value"] == "Keep this note"
        assert row.availability == "unavailable"
        assert row.revision == 2
        assert session.get(ServiceState, OWNER_A).catalog_revision == 1
    assert (
        prepare(client, payload, "stale").json()["error"]["code"] == "REVISION_CONFLICT"
    )
    draft2 = prepare(client, {**payload, "base_revision": 2}, "fresh").json()["data"]
    proposed2 = proposal(client, draft2["draft_id"], "fresh-proposal").json()["data"]
    with db.sessions.begin() as session:
        session.get(Garment, garment_id).revision += 1
    assert (
        client.get(
            f"/v1/change-proposals/{proposed2['proposal_id']}", headers=headers()
        ).json()["data"]["state"]
        == "conflicted"
    )
    assert decide(client, assertion(key, proposed2)).status_code == 409


def test_reject_expiry_and_payload_tampering(live_service):
    client, db = live_service
    enable_product(db)
    key = human_setup(db)
    payload = draft_input(client)
    draft_id = prepare(client, payload).json()["data"]["draft_id"]
    proposed = proposal(client, draft_id).json()["data"]
    assert (
        decide(client, assertion(key, proposed, decision="reject")).status_code == 200
    )
    assert (
        client.get(f"/v1/product-drafts/{draft_id}", headers=headers()).json()["data"][
            "effective"
        ]
        is False
    )
    proposed = proposal(client, draft_id, "new-proposal").json()["data"]
    with db.sessions.begin() as session:
        draft = session.get(ProductDraft, draft_id)
        draft.payload = {**draft.payload, "name": "Tampered after approval preview"}
    assert decide(client, assertion(key, proposed)).status_code == 409
    with db.sessions.begin() as session:
        expired = session.get(ProductDraft, draft_id)
        expired.created_at = int(time.time()) - 100
        expired.expires_at = int(time.time()) - 1
    assert proposal(client, draft_id, "expired").status_code == 409


def test_product_mcp_input_errors_are_structured(live_service):
    client, _db = live_service
    response = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.lookup_product",
            "arguments": {"article_number": "TEST-001", "page": "1"},
        },
    ).json()["result"]
    assert response["isError"]
    assert response["structuredContent"]["error"]["code"] == "INVALID_ARGUMENT"


def test_draft_images_expire_and_do_not_bypass_garment_removal(live_service):
    client, db = live_service
    enable_product(db)
    key = human_setup(db)
    payload = draft_input(client)
    prepared = prepare(client, payload).json()["data"]
    for ref in prepared["image_references"]:
        response = client.get(ref["content_url"], headers=headers())
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert (
            client.get(ref["content_url"], headers=headers(TOKEN_B)).status_code == 404
        )
        assert client.get(ref["content_url"]).status_code == 401
    prop = proposal(client, prepared["draft_id"]).json()["data"]
    decision = decide(client, assertion(key, prop)).json()["data"]
    garment_id = decision["published_garments"][prepared["draft_id"]]["garment_id"]
    with db.sessions.begin() as session:
        session.get(Garment, garment_id).state = "deleted"
    for ref in prepared["image_references"]:
        assert client.get(ref["content_url"], headers=headers()).status_code == 404
    assert (
        prepare(client, payload, "removed-image-reuse").json()["error"]["code"]
        == "TARGET_REMOVED"
    )


def test_multiple_draft_publications_roll_back_together(live_service):
    client, db = live_service
    enable_product(db)
    key = human_setup(db)
    payload = draft_input(client)
    first = prepare(client, payload, "first").json()["data"]["draft_id"]
    second = prepare(client, payload, "second").json()["data"]["draft_id"]
    prop = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "batch"},
        json={
            "changes": [
                {
                    "target_type": "product_draft",
                    "target_id": x,
                    "base_revision": 1,
                    "operation": "publish_product",
                }
                for x in (first, second)
            ]
        },
    ).json()["data"]
    with db.sessions.begin() as session:
        expired = session.get(ProductDraft, second)
        expired.created_at = int(time.time()) - 100
        expired.expires_at = int(time.time()) - 1
    assert decide(client, assertion(key, prop)).status_code == 409
    with db.sessions.begin() as session:
        assert session.get(ProductDraft, first).state == "draft"
        assert (
            session.scalar(
                select(func.count())
                .select_from(Garment)
                .where(Garment.owner_id == OWNER_A)
            )
            == 7
        )


def test_mcp_prepare_lookup_and_key_replay(live_service):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    looked = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.lookup_product",
            "arguments": {"article_number": "TEST-001"},
        },
    ).json()["result"]
    assert looked["isError"] is False
    made = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.prepare_product_draft",
            "arguments": {"payload": payload, "key": "mcp-first"},
        },
    ).json()["result"]
    assert made["isError"] is False
    assert prepare(client, payload, "mcp-first").json() == made["structuredContent"]


def test_concurrent_draft_retries_commit_one_receipt(live_service, providers):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    barrier = threading.Barrier(2)
    providers["product_provider"].on_detail = lambda: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: prepare(client, payload), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(ProductDraft)) == 1


def test_all_screenshot_color_claims_are_kept(live_service):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    payload["screenshots"][0]["values"] = {"primary_color": "黄色"}
    payload["screenshots"][1]["values"]["primary_color"] = "卡其色"
    result = prepare(client, payload).json()["data"]["draft"]
    assert "color_conflict" in result["blockers"]
    assert len(result["conflicts"][-1]["screenshot_color_claims"]) == 2
    assert (
        prepare(client, {**payload, "color_note": "   "}, "blank-note").status_code
        == 422
    )


def test_conflicting_second_screenshot_size_blocks_publication(live_service):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    payload["screenshots"][0]["values"] = {"size": "M"}
    payload["screenshots"][1]["values"]["size"] = "L"
    draft = prepare(client, payload).json()["data"]
    assert "size_conflict" in draft["draft"]["blockers"]
    assert len(draft["draft"]["conflicts"][-1]["screenshot_size_claims"]) == 2
    assert proposal(client, draft["draft_id"]).status_code == 409


def test_conflicting_screenshot_colors_without_sales_color_are_blocked(
    live_service, providers
):
    client, db = live_service
    enable_product(db)
    for sku in providers["product_provider"].product["skus"]:
        del sku["attributes"]["颜色"]
    payload = draft_input(client)
    payload["screenshots"][0]["values"] = {"primary_color": "黄色"}
    payload["screenshots"][1]["values"]["primary_color"] = "蓝色"
    draft = prepare(client, payload).json()["data"]
    assert "screenshot_conflict:primary_color" in draft["draft"]["blockers"]
    assert proposal(client, draft["draft_id"]).status_code == 409


def test_matching_screenshot_preserves_platform_source_and_blank_lookup_is_rejected(
    live_service,
):
    client, db = live_service
    enable_product(db)
    payload = draft_input(client)
    payload["screenshots"][0]["values"] = {"brand": "Synthetic Brand"}
    fields = prepare(client, payload).json()["data"]["draft"]["annotation_fields"]
    brand = fields["brand"]
    assert brand["source_kind"] == "product_api"
    assert len(brand["evidence"]) == 2
    assert len(fields["model"]["evidence"]) == 1
    assert len(fields["subcategory"]["evidence"]) == 1
    assert (
        client.post(
            "/v1/products/lookup", headers=headers(), json={"article_number": "   "}
        ).status_code
        == 422
    )


def test_upgrade_existing_0003_preserves_catalog_and_null_reference(tmp_path):
    path = tmp_path / "old.sqlite"
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0003_recommendations")
        connection.execute(text("INSERT INTO owners(id) VALUES (:id)"), {"id": OWNER_A})
        connection.execute(
            text(
                "INSERT INTO garments(id, owner_id, name, category, roles, availability, state, revision, annotation_fields, annotation_review_state, created_at) VALUES (:id,:owner,'Preserved','shirt','[\"upper\"]','unknown','active',3,'{}','initial',1)"
            ),
            {"id": str(UUID(int=100)), "owner": OWNER_A},
        )
    engine.dispose()
    database = Database(path)
    try:
        database.migrate()
        database.assert_initialized()
        database.migrate()
        with database.sessions.begin() as session:
            row = session.get(Garment, str(UUID(int=100)))
            assert row.name == "Preserved" and row.revision == 3
            assert row.product_reference is None
    finally:
        database.close()
