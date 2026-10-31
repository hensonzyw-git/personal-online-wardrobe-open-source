"""Offline asynchronous/API/MCP contracts, atomic guards and history."""

import copy
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import OWNER_A
from sqlalchemy import func, select
from test_foundation import headers, rpc
from test_outerwear_tshirt import add_outer
from test_recommendations import (
    FixtureEvaluator,
    FixtureWeather,
    completed,
    request_payload,
    seed_wardrobe,
    submit,
)

from wardrobe import recommendations
from wardrobe.catalog import CatalogService
from wardrobe.models import (
    Garment,
    GarmentAsset,
    HistoryEvent,
    Recommendation,
    ServiceState,
)
from wardrobe.recommendations import RecommendationService
from wardrobe.weather import weather_context
from wardrobe.writes import digest


class SyntheticWeather(FixtureWeather):
    low, high, middle = 16.6, 23.2, None

    async def fetch(self, location, interval, now):
        value = weather_context(await super().fetch(location, interval, now))
        data = {
            "temperature_min": self.low,
            "temperature_max": self.high,
            "feels_like_min": self.low,
            "feels_like_max": self.high,
        }
        if self.middle is not None:
            data.update(
                feels_like_median=Decimal(str(self.middle)), feels_like_sample_count=15
            )
        return value.model_copy(update=data)


class RecordingEvaluator(FixtureEvaluator):
    def __init__(self):
        self.calls = []
        self.only_thick = False

    async def evaluate(self, candidates, weather, occasion):
        self.calls.append(copy.deepcopy(candidates))
        values = await super().evaluate(candidates, weather, occasion)
        result = []
        for value, candidate in zip(values, candidates, strict=True):
            thick = any(
                i["role"] == "outerwear"
                and i["garment"]["annotation_fields"]["thickness"]["value"] == "thick"
                for i in candidate["items"]
            )
            if self.only_thick and not thick:
                continue
            result.append(
                {
                    **value,
                    **{
                        k: 1.0 if thick else 0.7
                        for k in ("color", "material", "style", "silhouette")
                    },
                }
            )
        return result


@pytest.fixture
def providers():
    return {"weather": SyntheticWeather(), "evaluator": RecordingEvaluator()}


def set_thickness(db, identifier, level, verified=True):
    with db.write() as session:
        g = session.get(Garment, identifier)
        g.annotation_fields = {
            **g.annotation_fields,
            "thickness": {
                "value": level,
                "verification_state": "user_verified" if verified else "unverified",
            },
        }


def clone(db, identifier):
    with db.write() as session:
        original = session.get(Garment, identifier)
        new_id = str(uuid4())
        session.add(
            Garment(
                **{
                    **{
                        c.name: copy.deepcopy(getattr(original, c.name))
                        for c in Garment.__table__.columns
                    },
                    "id": new_id,
                }
            )
        )
        for asset in session.scalars(
            select(GarmentAsset).where(GarmentAsset.garment_id == identifier)
        ):
            session.add(
                GarmentAsset(
                    **{
                        **{
                            c.name: getattr(asset, c.name)
                            for c in GarmentAsset.__table__.columns
                        },
                        "garment_id": new_id,
                    }
                )
            )
        return new_id


def seed(client, db, *, outer="medium", upper="thin", lower="thin", single=False):
    ids = seed_wardrobe(client, db)
    outer_id = add_outer(client, db)
    set_thickness(db, outer_id, outer)
    for identifier in ids[:3]:
        set_thickness(db, identifier, upper)
    for identifier in ids[3:5]:
        set_thickness(db, identifier, lower)
    if single:
        with db.write() as session:
            for identifier in (ids[1], ids[2], ids[4], ids[6]):
                session.get(Garment, identifier).availability = "unavailable"
    return ids, outer_id


def assert_no_history(db):
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
        assert session.get(ServiceState, OWNER_A).history_revision == 0


def assert_api_mcp(client, identifier, result):
    response = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.get_recommendation",
            "arguments": {"recommendation_id": identifier},
        },
    ).json()
    assert response["result"]["structuredContent"]["data"]["result"] == result
    assert (
        client.get(f"/v1/recommendations/{identifier}", headers=headers()).json()[
            "data"
        ]["result"]
        == result
    )


def budget(result):
    return result["search"]["layering_check"]["thermal_budget"]


def test_a1_medium_first_despite_thick_score_history_and_idempotency(
    live_service, providers
):
    client, db = live_service
    _ids, medium = seed(client, db)
    thick = clone(db, medium)
    set_thickness(db, thick, "thick")
    providers["weather"].middle = 19.4
    body = request_payload(count=1)
    identifier = submit(client, "thermal-a1", body)
    value = completed(client, identifier)
    assert value["state"] == "succeeded", value
    result = value["result"]
    assert budget(result)["reference_temperature_c"] == 19.4
    assert budget(result)["gap_c"] == 6.6
    assert budget(result)["reference_method"] == "hourly_median"
    outfit = result["outfits"][0]
    assert (
        next(i["garment"]["id"] for i in outfit["items"] if i["role"] == "outerwear")
        == medium
    )
    assert (
        budget(result)["selected_outfits"][outfit["outfit_id"]]["upper_warmth_c"] == 5
    )
    assert "本套上身约 5.0℃" in outfit["explanation"]["weather"]
    assert_api_mcp(client, identifier, result)
    assert submit(client, "thermal-a1", body) == identifier
    assert len(providers["evaluator"].calls) == 1
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 1
    # A frozen legacy result and its history stay opaque during a new run.
    with db.write() as session:
        row = session.get(Recommendation, identifier)
        row.result = {"legacy": True, "outfits": []}

    def rows_hash():
        with db.sessions() as session:
            rows = [
                session.get(Recommendation, identifier),
                *session.scalars(
                    select(HistoryEvent).where(
                        HistoryEvent.recommendation_id == identifier
                    )
                ),
            ]
            return digest(
                [
                    {c.name: getattr(r, c.name) for c in r.__table__.columns}
                    for r in rows
                ]
            )

    before = rows_hash()
    assert (
        completed(client, submit(client, "thermal-a1-next", body))["state"]
        == "succeeded"
    )
    assert before == rows_hash()


@pytest.mark.parametrize("count", [1, 2])
def test_a1b_only_thick_returns_partial_without_model_or_history(
    live_service, providers, count
):
    client, db = live_service
    seed(client, db, outer="thick", single=True)
    providers["weather"].middle = 19.4
    value = completed(
        client, submit(client, "thermal-a1b", request_payload(count=count))
    )
    assert value["state"] == "partial", value
    result = value["result"]
    assert result["outfits"] == [] and result["completeness"] == "partial"
    assert any("最高温不低于22℃" in n for n in result["missing"])
    assert providers["evaluator"].calls == []
    assert all("模型未返回" not in note for note in result["missing"])
    assert_no_history(db)


@pytest.mark.parametrize(
    "upper,outer,state",
    [
        ("thin", "medium", "succeeded"),
        ("medium", "medium", "succeeded"),
        ("thin", "thick", "partial"),
        ("thin", "thin", "partial"),
    ],
)
def test_a2_midpoint_prefilter_and_partial_api_mcp(
    live_service, providers, upper, outer, state
):
    client, db = live_service
    seed(client, db, upper=upper, outer=outer)
    providers["weather"].low, providers["weather"].high = 18.8, 22.3
    identifier = submit(client, "thermal-a2", request_payload(count=1))
    value = completed(client, identifier)
    assert value["state"] == state, value
    result = value["result"]
    assert budget(result)["reference_temperature_c"] == 20.55
    assert budget(result)["gap_c"] == 5.45
    assert budget(result)["upper_bound_c"] == 7.45
    assert result["search"]["layering_check"]["outerwear_required"]
    assert_api_mcp(client, identifier, result)
    if state == "partial":
        assert not providers["evaluator"].calls
        assert_no_history(db)
        assert any(
            ("过暖" if outer == "thick" else "欠暖") in n for n in result["missing"]
        )
    else:
        assert all(
            any(i["role"] == "outerwear" for i in o["items"]) for o in result["outfits"]
        )


@pytest.mark.parametrize("exclude,core", [(True, False), (False, True)])
def test_a5_a7_exclusion_or_incompatible_core_never_relaxed(
    live_service, providers, exclude, core
):
    client, db = live_service
    _ids, medium = seed(client, db)
    thick = clone(db, medium)
    set_thickness(db, thick, "very_thick")
    body = request_payload(
        count=1,
        exclude_ids=[medium] if exclude else [],
        core_ids=[thick] if core else [],
    )
    value = completed(client, submit(client, "thermal-exclude-core", body))
    assert value["state"] == "partial" and value["result"]["outfits"] == []
    assert not providers["evaluator"].calls
    assert_no_history(db)


def test_a6_estimated_tshirt_never_writes_annotations(live_service):
    client, db = live_service
    ids, _ = seed(client, db, single=True)
    set_thickness(db, ids[0], "thick", verified=False)
    with db.sessions() as session:
        before = copy.deepcopy(session.get(Garment, ids[0]).annotation_fields)
    result = completed(
        client, submit(client, "thermal-unknown", request_payload(count=1))
    )["result"]
    assert len(result["outfits"]) == 1
    terms = next(iter(budget(result)["selected_outfits"].values()))["terms"]
    assert any("layerable_inner_budget_assumption" in t["sources"] for t in terms)
    assert any("1.5" in n and "未写回" in n for n in result["assumptions"])
    with db.sessions() as session:
        assert session.get(Garment, ids[0]).annotation_fields == before


@pytest.mark.parametrize("stage", ["process", "commit"])
@pytest.mark.parametrize(
    "mutation", ["over_budget", "fake_weight", "fake_passed", "first_choice", "pants"]
)
def test_a9_two_guards_atomic_batch_zero_history(
    live_service, providers, monkeypatch, stage, mutation
):
    client, db = live_service
    ids, medium = seed(client, db)
    bad = clone(db, medium)
    set_thickness(db, bad, "thick")
    providers["weather"].low, providers["weather"].high = 18.8, 22.3
    if mutation == "first_choice":
        providers["weather"].low, providers["weather"].high = 16.6, 23.2
    if mutation == "pants":
        set_thickness(db, ids[4], "very_thick")
    with db.sessions() as session:
        bad_outer = CatalogService._view(session.get(Garment, bad), session)
        bad_pants = CatalogService._view(session.get(Garment, ids[4]), session)

    def corrupt(outfits):
        assert len(outfits) == 2
        if mutation in {"fake_weight", "fake_passed"}:
            outfits[-1]["upper_warmth_c" if mutation == "fake_weight" else "passed"] = (
                7.45 if mutation == "fake_weight" else True
            )
            return
        chosen = outfits[0] if mutation == "first_choice" else outfits[-1]
        target = "lower" if mutation == "pants" else "outerwear"
        next(i for i in chosen["items"] if i["role"] == target)["garment"] = (
            copy.deepcopy(bad_pants if mutation == "pants" else bad_outer)
        )
        chosen["upper_warmth_c"] = 5  # deliberately untrusted self-report

    if stage == "process":
        original = recommendations.choose

        def tampered(*args):
            outfits, count = original(*args)
            corrupt(outfits)
            return outfits, count

        monkeypatch.setattr(recommendations, "choose", tampered)
    else:
        original = RecommendationService._commit

        def tampered(self, *args):
            result = args[-1]
            if mutation == "fake_passed":
                budget(result)["passed"] = True
            elif mutation == "fake_weight":
                budget(result)["selected_outfits"][result["outfits"][-1]["outfit_id"]][
                    "upper_warmth_c"
                ] = 7.45
            else:
                corrupt(result["outfits"])
            return original(self, *args)

        monkeypatch.setattr(RecommendationService, "_commit", tampered)
    value = completed(client, submit(client, "thermal-tamper", request_payload()))
    assert (
        value["state"] == "failed"
        and value["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    ), value
    assert value["result"] is None
    assert_no_history(db)


@pytest.mark.parametrize(
    "occasion,state",
    [
        ("daily", "partial"),
        ("work", "partial"),
        ("commute", "partial"),
        ("weekend", "partial"),
        ("full_day", "succeeded"),
        ("all_day", "succeeded"),
        ("full_day_out", "succeeded"),
    ],
)
@pytest.mark.parametrize("core", [False, True])
def test_a10_a11_pants_gate_in_tasks_even_when_core(
    live_service, providers, occasion, state, core
):
    client, db = live_service
    ids, _ = seed(client, db, upper="very_thick", outer="medium", lower="very_thick")
    providers["weather"].low = providers["weather"].high = 8
    value = completed(
        client,
        submit(
            client,
            "thermal-pants",
            request_payload(
                count=1, occasion=occasion, core_ids=[ids[3]] if core else []
            ),
        ),
    )
    assert value["state"] == state, value
    if state == "partial":
        assert not providers["evaluator"].calls
        assert_no_history(db)
        assert any("特别厚长裤" in n for n in value["result"]["missing"])
    else:
        assert (
            next(iter(budget(value["result"])["selected_outfits"].values()))[
                "upper_warmth_c"
            ]
            == 12.5
        )


def test_o5_model_omits_better_witness_returns_partial(live_service, providers):
    client, db = live_service
    _, medium = seed(client, db)
    thick = clone(db, medium)
    set_thickness(db, thick, "thick")
    providers["evaluator"].only_thick = True
    value = completed(
        client, submit(client, "thermal-no-first", request_payload(count=1))
    )
    assert value["state"] == "partial" and value["result"]["outfits"] == []
    assert any("首套" in n for n in value["result"]["missing"])
    assert_no_history(db)


def test_o8_one_piece_has_only_two_terms_and_discloses_unmodeled_warmth(live_service):
    client, db = live_service
    ids, _ = seed(client, db)
    with db.write() as session:
        g = session.get(Garment, ids[3])
        g.category, g.roles = "连体衣", ["one_piece"]
        g.annotation_fields = {
            **g.annotation_fields,
            "seasons": {
                "value": ["all_year"],
                "verification_state": "user_verified",
                "source_kind": "user_supplied",
            },
        }
    value = completed(
        client,
        submit(
            client, "thermal-one-piece", request_payload(count=1, core_ids=[ids[3]])
        ),
    )
    assert value["state"] == "succeeded", value
    assert any("未建模额外保暖" in n for n in value["result"]["assumptions"])
    terms = next(iter(budget(value["result"])["selected_outfits"].values()))["terms"]
    assert {t["role"] for t in terms} == {"upper", "outerwear"}


@pytest.mark.parametrize(
    "upper,outer,middle", [("medium", "thin", 19.9), ("thin", "thin", 23)]
)
def test_a1c_layering_equivalence_in_tasks(
    live_service, providers, upper, outer, middle
):
    client, db = live_service
    seed(client, db, upper=upper, outer=outer)
    providers["weather"].middle = middle
    value = completed(
        client, submit(client, "thermal-equivalence", request_payload(count=1))
    )
    assert value["state"] == "succeeded", value
    assert next(iter(budget(value["result"])["selected_outfits"].values()))[
        "upper_warmth_c"
    ] == (5 if upper == "medium" else 3)


@pytest.mark.parametrize(
    "upper,state", [("thick", "partial"), ("very_thick", "succeeded")]
)
def test_a4_cold_medium_coat_requires_enough_inner_warmth(
    live_service, providers, upper, state
):
    client, db = live_service
    seed(client, db, upper=upper, outer="medium", lower="thick")
    providers["weather"].low = providers["weather"].high = 8
    value = completed(client, submit(client, "thermal-cold", request_payload(count=1)))
    assert value["state"] == state, value
    if state == "partial":
        assert any("欠暖" in n for n in value["result"]["missing"])
        assert not providers["evaluator"].calls
        assert_no_history(db)


@pytest.mark.parametrize(
    "middle,hottest,upper,outer,state,reason",
    [
        (8, 9, "thin", "very_thick", "succeeded", "very_cold_no_light"),
        (8, 22, "thin", "very_thick", "partial", None),
        (14, 18, "thick", "thick", "succeeded", "medium_pair_insufficient"),
        (14, 22, "thick", "thick", "partial", None),
    ],
)
def test_a15_a16_a16b_cold_and_medium_capacity_tasks(
    live_service, providers, middle, hottest, upper, outer, state, reason
):
    client, db = live_service
    seed(client, db, upper=upper, outer=outer, lower="thick", single=True)
    providers["weather"].low = 5 if middle == 8 else 12
    providers["weather"].high = hottest
    providers["weather"].middle = middle
    value = completed(client, submit(client, "heavy-state", request_payload(count=1)))
    assert value["state"] == state, value
    if reason:
        chosen = next(iter(budget(value["result"])["selected_outfits"].values()))
        assert chosen["first_choice_reason"] == reason and chosen["thick_fallback"]
        explanation = value["result"]["outfits"][0]["explanation"]["weather"]
        assert ("低于10℃" if middle == 8 else "等效7.0℃仍低于本次下沿") in explanation
    else:
        assert value["result"]["outfits"] == []
        assert any("最高温不低于22℃" in note for note in value["result"]["missing"])
        assert providers["evaluator"].calls == []
        assert_no_history(db)


@pytest.mark.parametrize(
    "category", ["长袖衬衫", "长袖上衣", "长款纽扣上衣", "针织上衣", "shirt"]
)
def test_a20_shirt_and_upper_knit_complete_without_tshirts(
    live_service, providers, category
):
    client, db = live_service
    ids, _ = seed(client, db, outer="thin")
    with db.write() as session:
        for identifier in ids[:3]:
            g = session.get(Garment, identifier)
            g.category = category
            g.annotation_fields = {
                **g.annotation_fields,
                "sleeve": {"value": "长袖"},
                "seasons": {
                    "value": ["autumn_winter"],
                    "verification_state": "user_verified",
                    "source_kind": "user_supplied",
                },
            }
    providers["weather"].low, providers["weather"].high, providers["weather"].middle = (
        20,
        24,
        22,
    )
    value = completed(client, submit(client, "layerable-without-t", request_payload()))
    assert value["state"] == "succeeded", value
    layering = value["result"]["search"]["layering_check"]
    assert (
        layering["eligible_tshirt_count"] == 0
        and layering["eligible_layerable_inner_count"] == 3
    )
    assert value["result"]["missing"] == []
    assert all(
        c["upper_warmth_c"] == 3
        for c in layering["thermal_budget"]["selected_outfits"].values()
    )


@pytest.mark.parametrize(
    "category", ["连帽上衣", "短袖连帽上衣", "hooded garment", "短袖衬衫"]
)
def test_a20d_a20f_forbidden_inners_do_not_call_model_even_all_year(
    live_service, providers, category
):
    client, db = live_service
    ids, _ = seed(client, db)
    with db.write() as session:
        for identifier in ids[:3]:
            g = session.get(Garment, identifier)
            g.category = category
            g.annotation_fields = {
                **g.annotation_fields,
                "seasons": {
                    "value": ["all_year"],
                    "verification_state": "user_verified",
                    "source_kind": "user_supplied",
                },
            }
    value = completed(client, submit(client, "invalid-inner", request_payload()))
    assert value["state"] == "partial" and value["result"]["outfits"] == [], value
    assert any("可叠内搭" in note for note in value["result"]["missing"])
    assert providers["evaluator"].calls == []
    assert_no_history(db)


@pytest.mark.parametrize(
    "mutation",
    [
        "negative_count",
        "bool_count",
        "oversized_count",
        "false_truncation",
        "template_count",
        "invalid_guard_shape",
    ],
)
def test_commit_rejects_malformed_runtime_observations(
    live_service, monkeypatch, mutation
):
    client, db = live_service
    seed(client, db)
    original = RecommendationService._commit

    def tampered(self, *args):
        diag = budget(args[-1])
        if mutation == "invalid_guard_shape":
            diag["first_choice_guard"] = None
        elif mutation == "false_truncation":
            diag["pair_search_truncated"] = diag["pair_search_complete"]
        elif mutation == "template_count":
            diag["template_complete_witness_counts"] = {"outerwear_inner": 10_000}
        else:
            diag["pair_states_checked"] = {
                "negative_count": -1,
                "bool_count": True,
                "oversized_count": 10_001,
            }[mutation]
        return original(self, *args)

    monkeypatch.setattr(RecommendationService, "_commit", tampered)
    value = completed(client, submit(client, "malformed-counts", request_payload()))
    assert (
        value["state"] == "failed"
        and value["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    ), value
    assert_no_history(db)


@pytest.mark.parametrize(
    "path,old_version",
    [
        (("rule_version",), "wardrobe-candidates/6"),
        (("temperature_check", "rule_version"), "wardrobe-temperature/2"),
        (("layering_check", "rule_version"), "outerwear-tshirt/2"),
        (("layering_check", "thermal_budget", "rule_version"), "thermal-budget/1"),
    ],
)
def test_a9_commit_rejects_stale_rule_versions(
    live_service, monkeypatch, path, old_version
):
    client, db = live_service
    seed(client, db)
    original = RecommendationService._commit

    def tampered(self, *args):
        diagnostics = args[-1]["search"]
        for key in path[:-1]:
            diagnostics = diagnostics[key]
        diagnostics[path[-1]] = old_version
        return original(self, *args)

    monkeypatch.setattr(RecommendationService, "_commit", tampered)
    value = completed(client, submit(client, "stale-version", request_payload()))
    assert value["state"] == "failed", value
    assert value["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    assert value["result"] is None
    assert_no_history(db)
