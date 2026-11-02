"""W-LIST-1..7/W-READ-1/W-OPS-1: public v1 compatibility is a hard gate."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from operator_support import (
    KEY,
    OTHER_OWNER,
    OTHER_TOKEN,
    OWNER,
    READ_TOKEN,
    T,
    headers,
    row,
    uid,
)
from operator_support import (
    operator_env as operator_env,  # noqa: PLC0414 - pytest fixture re-export
)
from sqlalchemy import event, text
from test_foundation import rpc

from wardrobe.contracts import ListQuery
from wardrobe.models import Garment, ServiceState


def listing(e, **query):
    return e.client.get("/v1/operator/garments", params=query, headers=headers())


def categories(e, **query):
    return e.client.get("/v1/operator/categories", params=query, headers=headers())


def assert_error(response, status=422, code="INVALID_CURSOR"):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"


def test_w_list_1_visibility_fields_initial_no_image_and_other_owner(operator_env):
    e = operator_env
    r = listing(e)
    assert r.status_code == 200
    data = r.json()["data"]
    assert {x["id"] for x in data["items"]} == {uid(n) for n in range(10, 16)}
    assert data["server_now"] == T
    for g in data["items"]:
        assert type(g["created_at"]) is int
        assert g["deleted_at"] is g["restore_until"] is None
        assert g["annotation_review_state"] == "initial"
        assert g["annotation_fields"]["windproof"]["value"] is None
    no_image = next(x for x in data["items"] if x["id"] == uid(12))
    assert (
        no_image["image_references"] == []
        and no_image["image_access_status"] == "missing"
    )
    for target in (30, 40, 999):
        assert (
            e.client.get(
                f"/v1/operator/garments/{uid(target)}", headers=headers()
            ).status_code
            == 404
        )


@pytest.mark.parametrize("limit", [7, 100])
@pytest.mark.parametrize("order", [None, "asc"])
def test_w_list_2_large_keyset_time_order_and_same_second(operator_env, limit, order):
    e = operator_env
    expected = [(T + n, uid(n)) for n in range(10, 16)]
    with e.db.write() as s:
        for n in range(1000, 1257):
            created = T - (n % 13)
            s.add(row(n, created_at=created))
            expected.append((created, uid(n)))
    query = {
        "limit": limit,
        **({"order": order, "sort": "created_at"} if order else {}),
    }
    got, cursor = [], None
    while True:
        response = listing(e, **query, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        got.extend((x["created_at"], x["id"]) for x in data["items"])
        assert data["has_more"] == (data["next_cursor"] is not None)
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert got == sorted(expected, reverse=order != "asc")
    assert len(set(got)) == len(expected)
    empty = listing(e, category="absent").json()["data"]
    assert (
        empty["items"] == []
        and empty["next_cursor"] is None
        and empty["has_more"] is False
    )


def test_w_list_3_exact_filter_query_shape_and_cursor_binding(operator_env):
    e = operator_env
    with e.db.write() as s:
        s.get(Garment, uid(10)).category = "Shirt"
        s.get(Garment, uid(11)).category = "shirt-extra"
    assert [x["id"] for x in listing(e, category="Shirt").json()["data"]["items"]] == [
        uid(10)
    ]
    assert len(listing(e, category="shirt").json()["data"]["items"]) == 4
    assert len(listing(e).json()["data"]["items"]) == 6
    for query in [
        "sort=id",
        "order=random",
        "state=all",
        "unknown=1",
        "limit=0",
        "limit=101",
        "limit=2&limit=2",
    ]:
        assert_error(
            e.client.get("/v1/operator/garments?" + query, headers=headers()),
            code="INVALID_ARGUMENT",
        )
    first = listing(e, limit=2).json()["data"]["next_cursor"]
    parsed = e.app.state.operator.catalog._decode(first)
    assert parsed["v"] == 2 and parsed["kind"] == "operator-garments"
    assert set(parsed) == {
        "v",
        "kind",
        "owner",
        "client",
        "channel",
        "state",
        "category",
        "limit",
        "sort",
        "order",
        "catalog_revision",
        "last",
    }
    changed = [
        {"v": 1},
        {"v": True},
        {"kind": "operator-categories"},
        {"owner": OTHER_OWNER},
        {"client": "wrong"},
        {"channel": "agent"},
        {"state": "deleted"},
        {"category": "Shirt"},
        {"limit": 3},
        {"sort": "id"},
        {"order": "asc"},
        {"catalog_revision": True},
        {"last": {"created_at": "1800000000", "id": uid(11)}},
        {"last": {"created_at": T, "id": "invalid"}},
        {"last": {"created_at": True, "id": uid(11)}},
        {"last": {"created_at": 2**80, "id": uid(11)}},
        {"catalog_revision": 2**80},
        {"last": {"created_at": T, "id": uid(11), "extra": 1}},
        {"extra": 1},
    ]
    for patch in changed:
        token = e.app.state.operator.catalog._encode({**parsed, **patch})
        assert_error(listing(e, limit=2, cursor=token))
    for drop in parsed:
        token = e.app.state.operator.catalog._encode(
            {k: v for k, v in parsed.items() if k != drop}
        )
        assert_error(listing(e, limit=2, cursor=token))
    assert_error(listing(e, limit=2, cursor="x" + first[1:]))
    for cursor in ("", "x" * 4097):
        assert_error(listing(e, cursor=cursor))
        assert_error(categories(e, cursor=cursor))
    for query in (
        {"limit": 3},
        {"limit": 2, "category": "shirt"},
        {"limit": 2, "order": "asc"},
        {"limit": 2, "state": "deleted"},
    ):
        assert_error(listing(e, cursor=first, **query))
    # A cryptographically valid binding is checked before catalog staleness.
    with e.db.write() as s:
        s.get(ServiceState, OWNER).catalog_revision += 1
    assert_error(listing(e, limit=2, cursor=first), 409, "CURSOR_STALE")
    forged = e.app.state.operator.catalog._encode(
        {**parsed, "kind": "operator-categories"}
    )
    assert_error(listing(e, limit=2, cursor=forged))


def test_w_list_4_trash_pages_follow_current_clock_without_revision_change(
    operator_env,
):
    e = operator_env
    with e.db.write() as s:
        s.get(Garment, uid(20)).created_at = T
        s.add(row(24, state="deleted", until=T + 20, created_at=T - 1))
        s.add(row(25, state="deleted", until=T + 200, created_at=T - 2))
    first = listing(e, state="deleted", limit=1).json()["data"]
    assert first["items"][0]["id"] == uid(20)
    assert first["items"][0]["image_references"] == []
    assert first["items"][0]["image_access_status"] == "deleted"
    e.clock[0] = T + 101
    second = listing(e, state="deleted", limit=1, cursor=first["next_cursor"]).json()[
        "data"
    ]
    assert [x["id"] for x in second["items"]] == [uid(25)]
    assert second["catalog_revision"] == first["catalog_revision"]
    assert second["has_more"] is False
    assert (
        e.client.get(
            f"/v1/operator/garments/{uid(20)}?state=deleted", headers=headers()
        ).status_code
        == 404
    )
    assert (
        e.client.post(
            f"/v1/operator/garments/{uid(20)}/restore",
            headers=headers(),
            json={"base_revision": 1},
        ).json()["error"]["code"]
        == "RESTORE_UNAVAILABLE"
    )


def test_w_list_5_categories_distinct_and_sql_materialization_bound(operator_env):
    e = operator_env
    expected = {"shirt", "Alpha", "alpha", "合成分类"}
    with e.db.write() as s:
        for n in range(1000, 2000):
            category = ["Alpha", "alpha", "合成分类", None][n % 4]
            s.add(row(n, created_at=T - n % 17, category=category))
    seen, token = [], None
    while True:
        data = categories(e, limit=2, **({"cursor": token} if token else {})).json()[
            "data"
        ]
        seen.extend(data["items"])
        token = data["next_cursor"]
        if token is None:
            break
    assert seen == sorted(expected)
    assert "None" not in seen and len(set(seen)) == len(seen)
    statements, loaded = [], []

    def sql(_conn, _cursor, statement, params, *_):
        if "FROM garments" in statement:
            statements.append((statement, params))

    def load(_session, instance):
        if isinstance(instance, Garment):
            loaded.append(instance.id)

    event.listen(e.db.engine, "before_cursor_execute", sql)
    event.listen(e.db.sessions, "loaded_as_persistent", load)
    try:
        data = listing(e, limit=7).json()["data"]
    finally:
        event.remove(e.db.engine, "before_cursor_execute", sql)
        event.remove(e.db.sessions, "loaded_as_persistent", load)
    assert len(data["items"]) == 7 and len(loaded) <= 8
    catalog_sql = [
        statement
        for statement, _params in statements
        if "ORDER BY garments.created_at DESC, garments.id DESC" in statement
    ]
    assert catalog_sql and all("LIMIT" in statement for statement in catalog_sql)
    assert categories(e, state="deleted").json()["data"]["items"] == ["shirt"]


def test_w_list_6_public_id_default_and_real_before_upgrade_v1_fixture(operator_env):
    """Fixture was generated by executing Base's AST-extracted _encode, not new code."""
    e = operator_env
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/operator/public-v1-before.json").read_text()
    )
    assert fixture["source_base"] == "2a45d7ea0ddbf1d8f708c241535680f927f14493"
    assert bytes.fromhex(fixture["synthetic_key_hex"]) == KEY
    base_source = subprocess.check_output(
        ["git", "show", fixture["source_base"] + ":" + fixture["source_path"]]
    )
    assert hashlib.sha256(base_source).hexdigest() == fixture["source_sha256"]
    with e.db.sessions.begin() as s:
        before = s.get(ServiceState, OWNER).catalog_revision
    token = fixture["cursor"]
    page = e.client.get(
        "/v1/garments",
        params={"limit": 2, "cursor": token},
        headers=headers(READ_TOKEN),
    )
    assert page.status_code == 200, page.text
    assert [g["id"] for g in page.json()["data"]["items"]] == [uid(12), uid(13)]
    assert set(e.app.state.catalog._decode(token)) == {
        "v",
        "owner",
        "category",
        "limit",
        "revision",
        "last",
    }
    # The same owner's other existing client can continue old v1: no client/channel binding added.
    other = "synthetic-second-client-" + "c" * 40
    from wardrobe.cli import grant_client

    grant_client(
        e.db,
        e.settings,
        owner_id=OWNER,
        client_id="other-existing-reader",
        scopes=["wardrobe.read"],
        token=other,
    )
    assert (
        e.client.get(
            "/v1/garments", params={"limit": 2, "cursor": token}, headers=headers(other)
        ).status_code
        == 200
    )
    all_api, all_mcp = [], []
    cursor = None
    while True:
        query = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        api = e.client.get(
            "/v1/garments", params=query, headers=headers(READ_TOKEN)
        ).json()["data"]
        mcp = rpc(
            e.client,
            "tools/call",
            {"name": "wardrobe.list_garments", "arguments": query},
            token=READ_TOKEN,
        ).json()["result"]
        assert not mcp["isError"]
        assert api == mcp["structuredContent"]["data"]
        all_api.extend(g["id"] for g in api["items"])
        all_mcp.extend(g["id"] for g in mcp["structuredContent"]["data"]["items"])
        assert all(type(g["created_at"]) is int for g in api["items"])
        cursor = api["next_cursor"]
        if cursor is None:
            break
    assert all_api == all_mcp == [uid(n) for n in range(10, 16)]
    assert ListQuery().limit == 50
    assert (
        e.client.get("/v1/garments?limit=100", headers=headers(READ_TOKEN)).status_code
        == 200
    )
    for query, h in [
        ({"limit": 3}, headers(READ_TOKEN)),
        ({"limit": 2, "category": "shirt"}, headers(READ_TOKEN)),
        ({"limit": 2}, headers(OTHER_TOKEN)),
    ]:
        assert_error(
            e.client.get("/v1/garments", params={**query, "cursor": token}, headers=h)
        )
    for query in ("sort=created_at", "order=asc", "state=deleted"):
        assert_error(
            e.client.get("/v1/garments?" + query, headers=headers(READ_TOKEN)),
            code="INVALID_ARGUMENT",
        )
    with e.db.sessions.begin() as s:
        assert s.get(ServiceState, OWNER).catalog_revision == before
    e.client.patch(
        f"/v1/operator/garments/{uid(10)}",
        headers=headers(),
        json={"base_revision": 1, "patch": {"name": "changed"}},
    )
    assert_error(
        e.client.get(
            "/v1/garments",
            params={"limit": 2, "cursor": token},
            headers=headers(READ_TOKEN),
        ),
        409,
        "CURSOR_STALE",
    )


def test_w_list_7_all_cursor_kinds_are_strictly_isolated(operator_env):
    e = operator_env
    with e.db.write() as s:
        s.get(Garment, uid(10)).category = "Alpha"
    public = e.client.get("/v1/garments?limit=1", headers=headers(READ_TOKEN)).json()[
        "data"
    ]["next_cursor"]
    garments = listing(e, limit=1).json()["data"]["next_cursor"]
    cats = categories(e, limit=1).json()["data"]["next_cursor"]
    assert public and garments and cats
    for token in (public, cats):
        assert_error(listing(e, limit=1, cursor=token))
    for token in (public, garments):
        assert_error(categories(e, limit=1, cursor=token))
    for token in (garments, cats):
        assert_error(
            e.client.get(
                "/v1/garments",
                params={"limit": 1, "cursor": token},
                headers=headers(READ_TOKEN),
            )
        )
        result = rpc(
            e.client,
            "tools/call",
            {
                "name": "wardrobe.list_garments",
                "arguments": {"limit": 1, "cursor": token},
            },
            token=READ_TOKEN,
        ).json()["result"]
        assert result["isError"]
        assert result["structuredContent"]["error"]["code"] == "INVALID_CURSOR"


def test_w_ops_1_schema_revision_files_and_agent_flag_unchanged(operator_env):
    e = operator_env

    def schema():
        with e.db.engine.connect() as c:
            return (
                list(
                    c.execute(
                        text(
                            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                        )
                    )
                ),
                c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
            )

    before = schema()
    assert before[1] == "0010_agent_facade"
    assert e.settings.agent_enabled is False
    assert listing(e).status_code == 200
    assert (
        e.client.patch(
            f"/v1/operator/garments/{uid(10)}",
            headers=headers(),
            json={"base_revision": 1, "patch": {"name": "schema unchanged"}},
        ).status_code
        == 200
    )
    assert schema() == before
    root = Path(__file__).resolve().parents[1]
    baseline = "2a45d7ea0ddbf1d8f708c241535680f927f14493"
    listed = (
        subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", baseline, "alembic/versions"],
            cwd=root,
        )
        .decode()
        .splitlines()
    )
    current = sorted(
        str(p.relative_to(root)) for p in (root / "alembic/versions").glob("*.py")
    )
    assert current == sorted(listed)
    assert len(current) == 10
    for file in listed:
        assert (root / file).read_bytes() == subprocess.check_output(
            ["git", "show", baseline + ":" + file], cwd=root
        )


@pytest.mark.parametrize("version", ["legacy", "unsupported-version"])
def test_w_read_1_existing_evidence_versions_are_allowlisted_and_read_only(
    operator_env, version
):
    from wardrobe.models import GarmentProductEvidence, ProductSnapshot
    from wardrobe.writes import digest

    e = operator_env
    secret_marker = "synthetic-raw-provider-secret-do-not-project"
    if version == "legacy":
        evidence = {
            "link_state": "candidates_unreviewed",
            "confirmed_global_spu_id": None,
            "candidate_global_spu_ids": ["123"],
            "rejected_global_spu_ids": [],
            "style_confirmed_article_number": None,
            "owned_sku_id": None,
            "owned_size": None,
            "entered_article_number": "SYNTHETIC-ONLY",
            "issues": [],
            "full_api_responses": {"secret": secret_marker},
            "local_import_path": "/synthetic/private/source",
        }
        snapshot_value = {
            "raw_response": {
                "secret": secret_marker,
                "Authorization": "synthetic credential",
            }
        }
    else:
        snapshot_value = {"raw_response": secret_marker}
        evidence = {
            "schema_version": "unsupported-provider-product/1",
            "provider": "synthetic",
            "full_api_responses": secret_marker,
        }
    with e.db.write() as s:
        s.add(
            ProductSnapshot(
                id=uid(901),
                owner_id=OWNER,
                provider="synthetic",
                region="HK",
                global_spu_id="123",
                digest=digest(snapshot_value),
                response=snapshot_value,
                recorded_at="2026-11-02T00:00:00Z",
            )
        )
        s.add(
            GarmentProductEvidence(
                garment_id=uid(10),
                owner_id=OWNER,
                evidence=evidence,
                snapshot_ids=[uid(901)],
            )
        )
    before = json.dumps(evidence, sort_keys=True)
    response = e.client.get(f"/v1/operator/garments/{uid(10)}", headers=headers())
    if version == "legacy":
        assert response.status_code == 200, response.text
        data = response.json()["data"]["garment"]
        assert (
            data["product_evidence"]["schema_version"] == "wardrobe-catalog-product/1"
        )
        assert data["product_reference"]["global_spu_id"] == "123"
        assert data["identity_fields"]["article_number"] == "SYNTHETIC-ONLY"
    else:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"
    for forbidden in (
        secret_marker,
        "full_api_responses",
        "local_import_path",
        "Authorization",
        "/synthetic/private/source",
    ):
        assert forbidden not in response.text
    assert (
        e.client.patch(
            f"/v1/operator/garments/{uid(10)}",
            headers=headers(),
            json={"base_revision": 1, "patch": {"name": "local edit"}},
        ).status_code
        == 200
    )
    with e.db.sessions.begin() as s:
        assert (
            json.dumps(s.get(GarmentProductEvidence, uid(10)).evidence, sort_keys=True)
            == before
        )
        assert s.get(ProductSnapshot, uid(901)).response == snapshot_value
