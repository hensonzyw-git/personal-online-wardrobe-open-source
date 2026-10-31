"""User policy: outerwear includes a distinct layerable inner, with season compatibility."""

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_catalog_writes import png, upload
from test_foundation import headers
from test_recommendation_review_regressions import catalog, coverage, payload
from test_recommendations import (
    FixtureEvaluator,
    FixtureWeather,
    completed,
    request_payload,
    seed_wardrobe,
    submit,
)

from wardrobe.models import Garment, HistoryEvent
from wardrobe.recommendations import (
    RecommendationService,
    candidates_for,
    outerwear_has_layerable_inner,
)


@pytest.mark.parametrize("mode", ["cold", "core", "required_function"])
def test_all_outerwear_triggers_accept_o18_shirts_before_search(mode):
    garments = catalog(cold=mode == "cold")
    for g in garments:
        if "upper" in g["roles"] and g["id"] != "upper-069":
            g["category"] = "长袖衬衫"
            g["annotation_fields"]["sleeve"] = {"value": "长袖"}
    kwargs = (
        {"core_ids": ["outerwear-013"]}
        if mode == "core"
        else {"required_functions": {"outerwear": []}}
        if mode == "required_function"
        else {}
    )
    weather = SimpleNamespace(
        feels_like_min=5 if mode == "cold" else 24,
        feels_like_max=9 if mode == "cold" else 26,
    )
    candidates, search = candidates_for(garments, payload(**kwargs), weather)
    assert candidates and len(coverage(candidates, "upper")) == 70
    assert search["layering_check"]["outerwear_required"]
    assert search["layering_check"]["eligible_tshirt_count"] == 1
    assert search["eligible_role_counts"]["upper"] == 70
    assert search["layering_check"]["eligible_layerable_inner_count"] == 70
    assert search["searched_variants"] <= 500
    assert all(outerwear_has_layerable_inner(c["items"]) for c in candidates)


@pytest.mark.parametrize(
    "reason", ["short_shirt", "excluded", "unavailable", "hooded", "cross_season"]
)
def test_missing_or_unusable_inner_never_falls_back_to_invalid_categories(reason):
    garments = catalog()
    upper_ids = [g["id"] for g in garments if "upper" in g["roles"]]
    for g in garments:
        if "upper" not in g["roles"]:
            continue
        if reason in {"short_shirt", "hooded", "cross_season"}:
            g["category"] = (
                "短袖衬衫"
                if reason == "short_shirt"
                else "连帽上衣"
                if reason == "hooded"
                else "shirt"
            )
            g["annotation_fields"]["sleeve"] = {
                "value": "短袖" if reason == "short_shirt" else "长袖"
            }
            if reason == "cross_season":
                g["annotation_fields"]["seasons"] = {
                    "value": ["spring_summer"],
                    "verification_state": "user_verified",
                    "source_kind": "user_supplied",
                }
            g["name"] = "T恤 in a name cannot establish the category"
        if reason == "unavailable":
            g["recommendation_eligible"] = False
    options = {"core_ids": ["outerwear-013"]}
    if reason == "excluded":
        options["exclude_ids"] = upper_ids
    candidates, search = candidates_for(
        garments,
        payload(**options),
        SimpleNamespace(feels_like_min=20, feels_like_max=23),
    )
    assert candidates == []
    assert search["layering_check"]["eligible_tshirt_count"] == 0


def test_hooded_core_is_preserved_as_an_unsatisfied_constraint_not_replaced():
    garments = catalog()
    garments[0]["category"] = "连帽上衣"
    candidates, _ = candidates_for(
        garments,
        payload(core_ids=["upper-000", "outerwear-013"]),
        SimpleNamespace(feels_like_min=20, feels_like_max=23),
    )
    assert candidates == []


def test_no_outerwear_keeps_non_tshirt_upper_options():
    garments = catalog()
    for g in garments:
        if "upper" in g["roles"]:
            g["category"] = "针织上衣"
    candidates, search = candidates_for(
        garments, payload(), SimpleNamespace(feels_like_min=24, feels_like_max=26)
    )
    assert candidates and len(coverage(candidates, "upper")) == 70
    assert not search["layering_check"]["outerwear_required"]
    assert all(
        not any(i["role"] == "outerwear" for i in c["items"]) for c in candidates
    )


def test_one_piece_with_outerwear_gets_separate_tshirt_and_no_duplicate_garments():
    garments = catalog()
    garments.append(
        {
            **copy.deepcopy(garments[0]),
            "id": "dress",
            "category": "连体衣",
            "roles": ["one_piece"],
        }
    )
    candidates, _ = candidates_for(
        garments,
        payload(core_ids=["dress", "outerwear-013"]),
        SimpleNamespace(feels_like_min=20, feels_like_max=23),
    )
    assert candidates
    for candidate in candidates:
        assert {i["role"] for i in candidate["items"]} == {
            "upper",
            "one_piece",
            "footwear",
            "outerwear",
        }
        assert len({i["garment"]["id"] for i in candidate["items"]}) == 4
        assert outerwear_has_layerable_inner(candidate["items"])


def test_a_single_garment_cannot_count_as_both_outerwear_and_tshirt():
    g = {"id": "same", "category": "短袖T恤"}
    assert not outerwear_has_layerable_inner(
        [{"role": "outerwear", "garment": g}, {"role": "upper", "garment": g}]
    )


@pytest.fixture
def providers():
    return {"weather": FixtureWeather(), "evaluator": FixtureEvaluator()}


def add_outer(client, db):
    asset = upload(client, png("blue"), key="outer-asset")
    r = client.post(
        "/v1/garments",
        headers={**headers(), "Idempotency-Key": "outer-garment"},
        json={
            "name": "Synthetic outer",
            "category": "外套",
            "roles": ["outerwear"],
            "asset_ids": [asset],
            "annotation_fields": {"sleeve": "长袖"},
        },
    )
    assert r.status_code == 200
    identifier = r.json()["data"]["garment_id"]
    with db.write() as session:
        g = session.get(Garment, identifier)
        g.annotation_fields = {
            **g.annotation_fields,
            "thickness": {"value": "thin", "verification_state": "user_verified"},
            "primary_color": {"value": "blue"},
        }
    return identifier


def test_new_outerwear_recommendation_persists_distinct_tshirt_and_history(
    live_service,
):
    client, db = live_service
    seed_wardrobe(client, db)
    outer = add_outer(client, db)
    job = submit(client, "outer-with-inner", request_payload(core_ids=[outer]))
    result = completed(client, job)
    assert result["state"] == "succeeded", result
    assert len(result["result"]["outfits"]) == 2
    for outfit in result["result"]["outfits"]:
        assert outerwear_has_layerable_inner(outfit["items"])
        assert any(
            i["role"] == "upper" and i["garment"]["category"] == "短袖T恤"
            for i in outfit["items"]
        )
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 2


def test_missing_tshirt_is_explicit_partial_with_no_model_or_generated_history(
    live_service, monkeypatch
):
    client, db = live_service
    ids = seed_wardrobe(client, db)
    outer = add_outer(client, db)
    with db.write() as session:
        for identifier in ids[:3]:
            g = session.get(Garment, identifier)
            g.category = "连帽上衣"
            g.annotation_fields = {**g.annotation_fields, "sleeve": {"value": "长袖"}}

    async def forbidden(*_):
        pytest.fail("no T-shirt candidates must not call the evaluator")

    monkeypatch.setattr(FixtureEvaluator, "evaluate", forbidden)
    job = submit(client, "outer-missing-inner", request_payload(core_ids=[outer]))
    result = completed(client, job)
    assert result["state"] == "partial", result
    assert result["result"]["outfits"] == []
    assert any("可叠内搭" in text for text in result["result"]["missing"])
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0


def test_publication_guard_rolls_back_even_if_an_upstream_step_breaks_layering(
    live_service, monkeypatch
):
    client, db = live_service
    seed_wardrobe(client, db)
    outer = add_outer(client, db)
    original = RecommendationService._commit

    def tampered(
        self,
        principal,
        identifier,
        lease,
        catalog_revision,
        history_revision,
        weather,
        result,
    ):
        for outfit in result["outfits"][-1:]:
            for item in outfit["items"]:
                if item["role"] == "upper":
                    item["garment"]["category"] = "长袖衬衫"
        return original(
            self,
            principal,
            identifier,
            lease,
            catalog_revision,
            history_revision,
            weather,
            result,
        )

    monkeypatch.setattr(RecommendationService, "_commit", tampered)
    job = submit(
        client, "reject-invalid-publication", request_payload(core_ids=[outer])
    )
    result = completed(client, job)
    assert (
        result["state"] == "failed"
        and result["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    ), result
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
