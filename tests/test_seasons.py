"""Selection boundaries over real SQLite and authenticated HTTP, no providers."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from conftest import OWNER_A, TOKEN_A
from sqlalchemy import select
from test_foundation import headers, rpc

from wardrobe.auth import token_hash
from wardrobe.catalog import CatalogService
from wardrobe.contracts import Principal
from wardrobe.errors import BusinessError
from wardrobe.models import ClientGrant, Garment, WardrobePreference
from wardrobe.seasons import (
    RULES,
    RULES_DIGEST,
    SearchInput,
    SeasonInput,
    SeasonService,
    initialize,
)


def setup_selection(db):
    initialize(db, OWNER_A, "autumn_winter")
    with db.write() as s:
        row = s.get(ClientGrant, token_hash(TOKEN_A))
        row.scopes = [*row.scopes, "wardrobe.season"]
        for index, (category, (_, sleeve)) in enumerate(RULES.items(), 100):
            s.add(
                Garment(
                    id=str(UUID(int=index)),
                    owner_id=OWNER_A,
                    name="Synthetic selection",
                    category=category,
                    roles=["upper"],
                    state="active",
                    revision=1,
                    availability="unknown",
                    created_at=1,
                    annotation_review_state="initial",
                    annotation_fields={"sleeve": {"value": sleeve}},
                )
            )


def test_initialization_scope_and_mcp_equivalence(live_service, store):
    client, _ = live_service
    db, _, _ = store
    assert client.get("/v1/season", headers=headers()).status_code == 503
    setup_selection(db)
    data = client.get("/v1/season", headers=headers()).json()["data"]
    assert data["rules_digest"] == RULES_DIGEST
    assert data["eligible_count"] == 13  # 10 autumn/winter + 3 all-year categories
    assert data["unassigned_count"] == 6  # old synthetic generic shirts
    assert client.get("/v1/season").status_code == 401
    assert (
        client.get(
            "/v1/season",
            headers=headers(token="synthetic-owner-b-read-token-" + "b" * 40),
        ).status_code
        == 503
    )
    assert (
        rpc(
            client, "tools/call", {"name": "wardrobe.get_season", "arguments": {}}
        ).json()["result"]["structuredContent"]["data"]
        == data
    )
    with pytest.raises(BusinessError):
        initialize(db, OWNER_A, "spring_summer")


def test_switch_cas_replay_noop_and_cursor_stale(live_service, store):
    client, _ = live_service
    db, _, _ = store
    setup_selection(db)
    old = client.get("/v1/garment-search?limit=1", headers=headers()).json()["data"]
    body = {"season": "spring_summer", "expected_revision": 1}
    auth = {**headers(), "Idempotency-Key": "synthetic-switch"}
    first = client.put("/v1/season", headers=auth, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["revision"] == 2
    assert client.put("/v1/season", headers=auth, json=body).json() == first.json()
    assert (
        client.put(
            "/v1/season", headers=auth, json={**body, "season": "autumn_winter"}
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/v1/season",
            headers={**headers(), "Idempotency-Key": "conflict"},
            json=body,
        ).status_code
        == 409
    )
    noop = client.put(
        "/v1/season",
        headers={**headers(), "Idempotency-Key": "noop"},
        json={**body, "expected_revision": 2},
    ).json()["data"]
    assert not noop["changed"] and noop["revision"] == 2
    assert (
        client.get(
            "/v1/garment-search",
            params={"limit": 1, "cursor": old["next_cursor"]},
            headers=headers(),
        ).status_code
        == 409
    )
    new = client.get("/v1/garment-search", headers=headers()).json()["data"]
    assert new["match_count"] == 6  # 3 spring/summer + 3 year-round
    all_items = client.get("/v1/garment-search?view=all", headers=headers()).json()[
        "data"
    ]
    assert all_items["match_count"] == 22
    assert (
        client.get("/v1/garment-search?limit=1&limit=2", headers=headers()).status_code
        == 422
    )


def test_two_competing_switches_only_one_revision_wins(store):
    db, _, key = store
    setup_selection(db)
    principal = Principal(
        OWNER_A,
        "client-a",
        frozenset({"wardrobe.read", "wardrobe.season"}),
        "agent",
        "open-wardrobe",
        token_hash(TOKEN_A),
    )
    service = SeasonService(db, CatalogService(db.sessions, key))

    def attempt(index):
        try:
            return service.set(
                principal,
                SeasonInput(season="spring_summer", expected_revision=1),
                f"concurrent-{index}",
            )["data"]["revision"]
        except BusinessError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(map(str, results)) == ["2", "SEASON_REVISION_CONFLICT"]
    with db.sessions.begin() as s:
        assert (
            s.scalar(
                select(WardrobePreference.revision).where(
                    WardrobePreference.owner_id == OWNER_A
                )
            )
            == 2
        )


def test_all_query_does_not_initialize_and_unknown_sleeve_excluded(store):
    db, _, key = store
    principal = Principal(
        OWNER_A,
        "client-a",
        frozenset({"wardrobe.read"}),
        "agent",
        "open-wardrobe",
        token_hash(TOKEN_A),
    )
    service = SeasonService(db, CatalogService(db.sessions, key))
    assert (
        service.search(principal, SearchInput(view="all"))["data"]["match_count"] == 6
    )
    with db.sessions.begin() as s:
        assert s.get(WardrobePreference, OWNER_A) is None
    with pytest.raises(BusinessError):
        service.search(principal, SearchInput())
    initialize(db, OWNER_A, "autumn_winter")
    with db.write() as s:
        first = s.scalar(select(Garment).where(Garment.owner_id == OWNER_A))
        first.category = "长袖衬衫"  # no sleeve evidence: not inferred
    assert service.search(principal, SearchInput())["data"]["match_count"] == 0


def test_filtered_search_distinguishes_catalog_and_match_counts(live_service, store):
    client, _ = live_service
    db, _, _ = store
    setup_selection(db)
    filters = {"view": "current", "category": "长袖衬衫", "limit": 1}
    data = client.get("/v1/garment-search", params=filters, headers=headers()).json()[
        "data"
    ]
    assert data["match_count"] == 1
    assert data["catalog_count"] == 22
    assert data["catalog_eligible_count"] == data["eligible_count"] == 13
    assert data["count_scopes"]["match_count"] == "complete_filtered_selection"
    assert (
        data["count_scopes"]["catalog_eligible_count"]
        == "owner_catalog_current_storage_season"
    )
    assert (
        rpc(
            client,
            "tools/call",
            {"name": "wardrobe.search_garments", "arguments": filters},
        ).json()["result"]["structuredContent"]["data"]
        == data
    )
    page = client.get(
        "/v1/garment-search", params={"limit": 1}, headers=headers()
    ).json()["data"]
    assert len(page["items"]) == 1 and page["has_more"] and page["match_count"] == 13
    assert page["catalog_eligible_count"] == 13
