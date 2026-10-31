import copy
import hashlib
import io
import json
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import event, func, select
from test_foundation import headers, rpc

from wardrobe.catalog_import import ImportBatch, import_catalog, load_batch
from wardrobe.cli import grant_client
from wardrobe.config import Settings
from wardrobe.errors import BusinessError
from wardrobe.labels import LabelValues
from wardrobe.models import (
    Asset,
    CatalogImport,
    CatalogSource,
    Garment,
    GarmentProductEvidence,
    OwnerAsset,
    ProductSnapshot,
    ServiceState,
)
from wardrobe.writes import digest

OWNER = str(UUID(int=3))
TOKEN = "synthetic-first-catalog-read-" + "c" * 40


def sealed(value):
    value = copy.deepcopy(value)
    value.pop("digest", None)
    return {**value, "digest": digest(value)}


def fixture_batch(db, count=3):
    root = db.path.parent
    grant_client(
        db,
        Settings(data_root=root),
        owner_id=OWNER,
        client_id="first-import-read",
        scopes=["wardrobe.read"],
        token=TOKEN,
    )
    entries = []
    with db.write() as session:
        for i in range(1, count + 1):
            out = io.BytesIO()
            Image.new("RGB", (8, 8), (i, 0, 0)).save(out, format="PNG")
            raw = out.getvalue()
            sha = hashlib.sha256(raw).hexdigest()
            asset_id = str(UUID(int=1000 + i))
            source_id = str(UUID(int=2000 + i))
            original = root / "assets" / "original" / sha
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(raw)
            session.add(
                Asset(
                    id=asset_id,
                    sha256=sha,
                    size=len(raw),
                    mime="image/png",
                    width=8,
                    height=8,
                )
            )
            session.flush()
            session.add(OwnerAsset(owner_id=OWNER, asset_id=asset_id))
            fields = {
                k: {
                    "value": None,
                    "source_kind": "unknown",
                    "verification_state": "unknown",
                    "evidence": [],
                }
                for k in LabelValues.model_fields
            }
            fields["primary_color"] = {
                "value": "red",
                "source_kind": "observed",
                "verification_state": "unverified",
                "evidence": [{"asset_id": asset_id, "analysis_sha256": sha}],
            }
            fields["brand"] = {
                "value": "Synthetic Brand",
                "source_kind": "source_filename",
                "verification_state": "unverified",
                "evidence": [{"kind": "source_filename", "original_sha256": sha}],
            }
            candidates = ["10001"] if i <= 2 else []
            review = {
                "source_id": source_id,
                "index": i,
                "filename": f"Synthetic image {i}",
                "filename_brand": "Synthetic Brand",
                "status": "correct" if i == 1 else "unreviewed",
                "correct_product_id": "10001" if i == 1 else None,
                "candidate_checks": {"10001": "correct"} if i == 1 else {},
                "note": "",
                "entered_article_number": "TEST-1" if i == 2 else "",
                "updated_at": None,
            }
            evidence = {
                "link_state": "human_image_matched"
                if i == 1
                else "candidates_unreviewed"
                if i == 2
                else "photo_only",
                "confirmed_global_spu_id": "10001" if i == 1 else None,
                "candidate_global_spu_ids": candidates,
                "rejected_global_spu_ids": [],
                "style_confirmed_article_number": None,
                "owned_sku_id": None,
                "owned_size": None,
                "entered_article_number": review["entered_article_number"],
                "full_api_response_ids": candidates,
                "web_sources": [],
                "human_review": review,
                "issues": [],
            }
            entries.append(
                {
                    "index": i,
                    "source_id": source_id,
                    "proposed_garment_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            "wardrobe-first-catalog:" + OWNER + ":" + source_id,
                        )
                    ),
                    "original_asset_id": asset_id,
                    "original_sha256": sha,
                    "filename": review["filename"],
                    "filename_brand": "Synthetic Brand",
                    "name": review["filename"],
                    "category": "shirt",
                    "roles": ["upper"],
                    "availability": "unknown",
                    "annotation_fields": fields,
                    "annotation_review_state": "initial",
                    "product_evidence": evidence,
                    "source_metadata": {"private_path_hint": "PRIVATE-NOT-PUBLIC"},
                    "analysis_sha256": sha,
                }
            )
    snapshot = {
        "code": 200,
        "success": True,
        "trace_id": "synthetic",
        "data": [
            {
                "globalSpuId": 10001,
                "region": "US",
                "spuInfo": {
                    "globalSpuId": 10001,
                    "title": "Synthetic shirt",
                    "brandName": "Synthetic Brand",
                    "articleNumber": "TEST-1",
                    "optional_extra": {"preserved": True},
                },
                "skuInfoList": [
                    {"globalSkuId": 40001, "size": "M", "barCode": "synthetic-barcode"}
                ],
            }
        ],
    }
    value = sealed(
        {
            "schema_version": "wardrobe-enriched-import-preview/1",
            "prepared_at": "2026-10-14T00:00:00+00:00",
            "owner_id": OWNER,
            "published": False,
            "production_write": False,
            "parent_batch_id": str(UUID(int=3000)),
            "parent_batch_digest": "a" * 64,
            "human_review_dataset_id": "synthetic-dataset",
            "summary": {},
            "policy": {},
            "entries": entries,
            "api_snapshots": {"10001": snapshot},
        }
    )
    return root, value


def parsed(value):
    return ImportBatch.model_validate(value)


def test_first_import_dry_run_atomic_replay_and_changed_batch(store):
    db, _, _ = store
    root, value = fixture_batch(db)
    batch = parsed(value)
    check = import_catalog(db, root, batch)
    assert check["data"]["effective"] is False
    with db.sessions.begin() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(Garment)
                .where(Garment.owner_id == OWNER)
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    receipt = import_catalog(db, root, batch, apply=True)
    assert import_catalog(db, root, batch, apply=True) == receipt
    with db.sessions.begin() as s:
        assert s.scalar(select(func.count()).select_from(CatalogSource)) == 3
        assert s.scalar(select(func.count()).select_from(GarmentProductEvidence)) == 3
        assert s.scalar(select(func.count()).select_from(ProductSnapshot)) == 1
        assert s.get(ServiceState, OWNER).catalog_revision == 1
        g = s.get(Garment, value["entries"][0]["proposed_garment_id"])
        assert (
            g.product_reference is None and g.annotation_fields["size"]["value"] is None
        )
        assert (
            g.annotation_fields["primary_color"]["verification_state"] == "unverified"
        )
    changed = copy.deepcopy(value)
    changed["entries"][0]["name"] = "Changed"
    with pytest.raises(BusinessError, match="cannot overwrite"):
        import_catalog(db, root, parsed(sealed(changed)), apply=True)
    # Replaying after an existing garment is removed never resurrects it.
    with db.write() as s:
        s.get(Garment, value["entries"][0]["proposed_garment_id"]).state = "deleted"
    assert import_catalog(db, root, batch, apply=True) == receipt
    with db.sessions.begin() as s:
        assert (
            s.get(Garment, value["entries"][0]["proposed_garment_id"]).state
            == "deleted"
        )


def test_rollback_on_mid_transaction_failure(store):
    db, _, _ = store
    root, value = fixture_batch(db)

    def fail(_session, _flush_context, _instances):
        if any(
            isinstance(x, CatalogSource)
            and x.source_id == value["entries"][1]["source_id"]
            for x in _session.new
        ):
            raise RuntimeError("synthetic interruption")

    event.listen(db.sessions.class_, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            import_catalog(db, root, parsed(value), apply=True)
    finally:
        event.remove(db.sessions.class_, "before_flush", fail)
    with db.sessions.begin() as s:
        assert s.scalar(select(func.count()).select_from(CatalogSource)) == 0
        assert s.scalar(select(func.count()).select_from(CatalogImport)) == 0
        assert s.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
        assert (
            s.scalar(
                select(func.count())
                .select_from(Garment)
                .where(Garment.owner_id == OWNER)
            )
            == 0
        )
        assert s.get(ServiceState, OWNER).catalog_revision == 0


@pytest.mark.parametrize("corruption", ["missing", "hash", "symlink", "foreign"])
def test_asset_preflight_rejects_without_partial_writes(store, corruption):
    db, _, _ = store
    root, value = fixture_batch(db)
    e = value["entries"][1]
    path = root / "assets" / "original" / e["original_sha256"]
    if corruption == "missing":
        path.unlink()
    elif corruption == "hash":
        path.write_bytes(b"corrupt")
    elif corruption == "symlink":
        target = root / "outside.png"
        path.rename(target)
        path.symlink_to(target)
    else:
        with db.write() as s:
            s.delete(s.get(OwnerAsset, (OWNER, e["original_asset_id"])))
    with pytest.raises(BusinessError):
        import_catalog(db, root, parsed(value), apply=True)
    with db.sessions.begin() as s:
        assert s.scalar(select(func.count()).select_from(CatalogSource)) == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "owned_sku",
        "promote_candidate",
        "wrong_snapshot",
        "wrong_photo",
        "false_verification",
        "identity",
        "duplicate",
        "material_claim",
    ],
)
def test_invalid_evidence_cannot_be_promoted(store, corruption):
    db, _, _ = store
    _, value = fixture_batch(db)
    if corruption == "owned_sku":
        value["entries"][1]["product_evidence"]["owned_sku_id"] = "40001"
    elif corruption == "promote_candidate":
        value["entries"][1]["product_evidence"]["confirmed_global_spu_id"] = "10001"
    elif corruption == "wrong_snapshot":
        value["api_snapshots"]["10001"]["data"][0]["globalSpuId"] = 20002
    elif corruption == "wrong_photo":
        value["entries"][1]["annotation_fields"]["primary_color"]["evidence"][0][
            "asset_id"
        ] = value["entries"][0]["original_asset_id"]
    elif corruption == "false_verification":
        value["entries"][1]["annotation_fields"]["primary_color"][
            "verification_state"
        ] = "user_verified"
    elif corruption == "identity":
        value["entries"][1]["proposed_garment_id"] = str(UUID(int=99999))
    elif corruption == "duplicate":
        value["entries"][1] = copy.deepcopy(value["entries"][0])
    else:
        value["entries"][1]["annotation_fields"]["composition"] = {
            **value["entries"][1]["annotation_fields"]["primary_color"],
            "value": "100% cotton",
        }
    with pytest.raises(ValidationError):
        parsed(sealed(value))


def test_private_file_digest_duplicate_json_and_mutation_guards(store, tmp_path):
    db, _, _ = store
    root, value = fixture_batch(db)
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    batch = load_batch(path, value["digest"], OWNER)
    batch.entries[0].name = "changed after loading"
    with pytest.raises(BusinessError, match="Frozen batch"):
        import_catalog(db, root, batch, apply=True)
    with pytest.raises(BusinessError):
        load_batch(path, "0" * 64, OWNER)
    with pytest.raises(BusinessError):
        load_batch(path, value["digest"], str(UUID(int=4)))
    path.chmod(0o644)
    with pytest.raises(BusinessError):
        load_batch(path, value["digest"], OWNER)
    path.chmod(0o600)
    path.write_text('{"digest":"a","digest":"b"}')
    with pytest.raises(BusinessError):
        load_batch(path, value["digest"], OWNER)


def test_api_mcp_complete_evidence_is_private_and_sizes_unknown(live_service):
    client, db = live_service
    root, value = fixture_batch(db)
    receipt = import_catalog(db, root, parsed(value), apply=True)
    identifiers = [m["garment_id"] for m in receipt["data"]["mappings"]]
    path = f"/v1/garments/{identifiers[0]}/product-evidence"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers()).status_code == 404
    result = client.get(path, headers=headers(TOKEN))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    payload = result.json()["data"]
    assert payload["full_api_responses"] == value["api_snapshots"]
    assert payload["product_evidence"]["owned_size"] is None
    assert "PRIVATE-NOT-PUBLIC" not in result.text
    mcp = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.get_product_evidence",
            "arguments": {"garment_id": identifiers[0]},
        },
        token=TOKEN,
    ).json()["result"]
    assert not mcp["isError"] and mcp["structuredContent"]["data"] == payload
    discovery = rpc(client, "tools/list", token=TOKEN).json()["result"]["tools"]
    assert not any("import" in x["name"] for x in discovery)
    all_items = []
    cursor = None
    while True:
        query = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/garments", params=query, headers=headers(TOKEN)).json()[
            "data"
        ]
        equivalent = rpc(
            client,
            "tools/call",
            {"name": "wardrobe.list_garments", "arguments": query},
            token=TOKEN,
        ).json()["result"]["structuredContent"]["data"]
        assert page == equivalent
        all_items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {x["id"] for x in all_items} == set(identifiers)
    assert all(x["annotation_fields"]["size"]["value"] is None for x in all_items)
    assert all("full_api_responses" not in x for x in all_items)
    with db.write() as s:
        s.get(Garment, identifiers[0]).state = "deleted"
    assert client.get(path, headers=headers(TOKEN)).status_code == 404
