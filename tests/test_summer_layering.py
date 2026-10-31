"""O19/O20: exact summer roles, Decimal budgets and both publication guards."""

import copy
from decimal import Decimal as D

import pytest
from conftest import OWNER_A
from test_recommendation_review_regressions import payload
from test_recommendations import completed, request_payload, seed_wardrobe, submit
from test_thermal_budget import garment, weather
from test_thermal_budget_recommendations import (
    RecordingEvaluator,
    SyntheticWeather,
    assert_api_mcp,
    assert_no_history,
    budget,
)

from wardrobe import recommendations
from wardrobe.errors import BusinessError
from wardrobe.models import Garment, WardrobePreference
from wardrobe.recommendations import (
    RecommendationService,
    candidates_for,
    choose,
    validate_outfits,
)
from wardrobe.thermal_budget import (
    OUTERWEAR_CATEGORIES,
    assumptions_for,
    check_upper_budget,
    role_assignment_allowed,
    thermal_context,
)


@pytest.fixture
def providers():
    return {"weather": SyntheticWeather(), "evaluator": RecordingEvaluator()}


def summer_catalog(*, inner_thickness="thin", outer_thickness="thin", base_only=False):
    inner = garment("upper", inner_thickness, identifier="t")
    if base_only:
        inner["roles"] = ["base_layer"]
    return [
        inner,
        garment("upper", outer_thickness, identifier="s", category="短袖衬衫"),
        garment("lower", category="短裤"),
        garment("footwear"),
    ]


def summer_pair(gs):
    return [
        {"role": "base_layer", "garment": gs[0]},
        {"role": "upper", "garment": gs[1]},
    ]


@pytest.mark.parametrize(
    "reference,enabled,weight",
    [
        ("21.999", False, None),
        ("22", True, D(3)),
        ("24", True, D(3)),
        ("28", True, None),
    ],
)
def test_a22_summer_trigger_bounds_and_o17_second_key(reference, enabled, weight):
    gs = summer_catalog()
    before = copy.deepcopy(gs)
    w = weather(reference, reference)
    candidates, search = candidates_for(gs, payload(count=1), w)
    diag = search["layering_check"]["thermal_budget"]
    assert diag["summer_layering_enabled"] is enabled
    layered = [c for c in candidates if c["_layer_template"] == "summer_open_shirt"]
    assert bool(layered) is (weight is not None)
    if reference == "24":
        selected, _ = choose(
            [
                {
                    **c,
                    "quality_score": 0.6
                    if "base_layer" in {i["role"] for i in c["items"]}
                    else 1,
                }
                for c in candidates
            ],
            [],
            payload(count=1),
            100,
            thermal_context(w),
            search.guard,
        )
        assert {i["role"] for i in selected[0]["items"]} == {
            "base_layer",
            "upper",
            "lower",
            "footwear",
        }
        validate_outfits(selected, gs, payload(count=1), w, search.guard)
    if reference == "28":
        assert (
            not layered and diag["upper_bound_c"] == 2 and diag["lower_bound_c"] == -2
        )
        assert all(c["_thermal"].weight == D("1.5") for c in candidates)
    assert gs == before and "短袖衬衫" not in OUTERWEAR_CATEGORIES


@pytest.mark.parametrize("base_only", [False, True])
@pytest.mark.parametrize("core", ["t", "s", "both", None])
def test_a22c_core_mapping_and_unchanged_catalog_roles(base_only, core):
    gs = summer_catalog(base_only=base_only)
    before = copy.deepcopy(gs)
    core_ids = ["t", "s"] if core == "both" else [core] if core else []
    req = payload(core_ids=core_ids, count=1)
    w = weather("24", "24")
    candidates, search = candidates_for(gs, req, w)
    assert candidates and candidates[0]["_layer_template"] == "summer_open_shirt"
    selected, _ = choose(
        [{**c, "quality_score": 1} for c in candidates],
        [],
        req,
        100,
        thermal_context(w),
        search.guard,
    )
    validate_outfits(selected, gs, req, w, search.guard)
    assert gs == before
    assert not role_assignment_allowed(gs[1], "base_layer", "summer_open_shirt")
    assert not role_assignment_allowed(gs[0], "base_layer", "single_upper_or_one_piece")


def test_a20e_unknown_assumptions_only_for_summer_layers_and_no_writeback():
    gs = summer_catalog(inner_thickness=None, outer_thickness=None)
    before = copy.deepcopy(gs)
    ctx = thermal_context(weather("24", "24"))
    check = check_upper_budget(summer_pair(gs), ctx)
    assert check.passed and check.weight == D(3) and check.counted_layer_count == 2
    assert all(
        t.thickness is None
        and t.rank_thickness == "thin"
        and t.sources == ("summer_layer_budget_assumption",)
        for t in check.terms
    )
    notes = assumptions_for([{"items": summer_pair(gs)}], ctx)
    assert sum("未写回" in note for note in notes) == 2
    solo = check_upper_budget([{"role": "upper", "garment": gs[1]}], ctx)
    assert solo.weight == D(0) and solo.terms[0].value is None
    assert gs == before


@pytest.mark.parametrize(
    "mutation",
    [
        "required",
        "core_outer",
        "function",
        "wrong_outer",
        "hooded",
        "unassigned",
        "autumn",
        "medium",
        "exclude",
    ],
)
def test_a22f_negative_templates_and_hot_budget(mutation):
    gs = summer_catalog()
    req = payload()
    w = weather("24", "24")
    if mutation == "required":
        w.feels_like_min = "20"
    elif mutation in {"core_outer", "function"}:
        gs.append(garment("outerwear"))
        req = (
            payload(core_ids=[gs[-1]["id"]])
            if mutation == "core_outer"
            else payload(required_functions={"outerwear": []})
        )
    elif mutation in {"wrong_outer", "hooded", "unassigned"}:
        gs[1]["category"] = {
            "wrong_outer": "shirt",
            "hooded": "短袖连帽上衣",
            "unassigned": "shirt",
        }[mutation]
    elif mutation == "autumn":
        gs[1]["annotation_fields"]["seasons"] = {
            "value": ["autumn_winter"],
            "verification_state": "user_verified",
            "source_kind": "user_supplied",
        }
    elif mutation == "medium":
        gs[1]["annotation_fields"]["thickness"]["value"] = "medium"
    else:
        req = payload(exclude_ids=["s"])
    candidates, _ = candidates_for(gs, req, w)
    assert not any(c["_layer_template"] == "summer_open_shirt" for c in candidates)


def test_base_layer_projects_all_upper_function_and_formality_constraints():
    gs = summer_catalog()
    req = payload(required_functions={"upper": ["waterproof"]}, occasion="formal")
    for g in gs:
        g["annotation_fields"]["formality"] = {"value": "formal"}
        g["annotation_fields"]["waterproof"] = {
            "value": True,
            "verification_state": "user_verified",
        }
    w = weather("24", "24")
    assert any(
        c["_layer_template"] == "summer_open_shirt"
        for c in candidates_for(gs, req, w)[0]
    )
    gs[0]["annotation_fields"]["waterproof"]["verification_state"] = "unverified"
    assert not any(
        c["_layer_template"] == "summer_open_shirt"
        for c in candidates_for(gs, req, w)[0]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "three_layers",
        "duplicate_role",
        "duplicate_id",
        "outer_category",
        "wrong_base",
        "no_lower",
    ],
)
def test_a21_structural_bypass_rejected(mutation):
    gs = summer_catalog()
    req, w = payload(), weather("24", "24")
    candidates, search = candidates_for(gs, req, w)
    outfit = copy.deepcopy(candidates[0])
    outfit = {k: v for k, v in outfit.items() if not k.startswith("_")}
    if mutation == "three_layers":
        outer = garment("outerwear")
        gs.append(outer)
        outfit["items"].append({"role": "outerwear", "garment": outer})
    elif mutation == "duplicate_role":
        outfit["items"].append({"role": "upper", "garment": gs[0]})
    elif mutation == "duplicate_id":
        outfit["items"][0]["garment"] = outfit["items"][1]["garment"]
    elif mutation == "outer_category":
        outfit["items"][1]["garment"]["category"] = "外套"
    elif mutation == "wrong_base":
        outfit["items"][0]["role"] = "accessory"
    else:
        outfit["items"] = [i for i in outfit["items"] if i["role"] != "lower"]
    with pytest.raises(BusinessError, match="Outfit violates"):
        validate_outfits([outfit], gs, req, w, search.guard)


def seed_summer(client, db):
    ids = seed_wardrobe(client, db)
    with db.write() as session:
        session.get(WardrobePreference, OWNER_A).active_season = "spring_summer"
        for identifier in ids[3:5]:
            session.get(Garment, identifier).category = "短裤"
        shirt = session.get(Garment, ids[1])
        shirt.category = "短袖衬衫"
        shirt.annotation_fields = {
            **shirt.annotation_fields,
            "thickness": {"value": None},
        }
        for identifier in (ids[0], ids[2]):
            g = session.get(Garment, identifier)
            g.annotation_fields = {**g.annotation_fields, "thickness": {"value": None}}
    return ids


def test_s14_summer_task_persistence_api_mcp_unknown_roles_unchanged(
    live_service, providers
):
    client, db = live_service
    ids = seed_summer(client, db)
    providers["weather"].low = providers["weather"].high = 24
    identifier = submit(client, "summer", request_payload(count=1, core_ids=ids[:2]))
    value = completed(client, identifier)
    assert value["state"] == "succeeded", value
    result = value["result"]
    assert result["search"]["rule_version"] == "wardrobe-candidates/7"
    assert (
        result["search"]["temperature_check"]["rule_version"] == "wardrobe-temperature/3"
    )
    assert result["search"]["layering_check"]["rule_version"] == "outerwear-layerable-inner/1"
    assert budget(result)["rule_version"] == "thermal-budget/2"
    selected = next(iter(budget(result)["selected_outfits"].values()))
    assert (
        selected["layer_template"] == "summer_open_shirt"
        and selected["upper_warmth_c"] == 3
    )
    assert "敞开外层" in result["outfits"][0]["explanation"]["weather"]
    assert_api_mcp(client, identifier, result)
    with db.sessions() as session:
        assert session.get(Garment, ids[0]).roles == ["upper"]
        assert (
            session.get(Garment, ids[1]).annotation_fields["thickness"]["value"] is None
        )
    assert len(providers["evaluator"].calls) == 1


@pytest.mark.parametrize("stage", ["process", "commit"])
@pytest.mark.parametrize("mutation", ["season", "three_layers", "template", "source"])
def test_a9_summer_two_guards_reject_entire_batch(
    live_service, providers, monkeypatch, stage, mutation
):
    client, db = live_service
    seed_summer(client, db)
    providers["weather"].low = providers["weather"].high = 24

    def corrupt(outfits):
        assert len(outfits) == 2
        target = outfits[-1]
        if mutation == "season":
            target["items"][0]["garment"]["annotation_fields"]["seasons"] = {
                "value": ["autumn_winter"],
                "verification_state": "user_verified",
                "source_kind": "user_supplied",
            }
        elif mutation == "three_layers":
            target["items"].append(
                {
                    "role": "outerwear",
                    "garment": copy.deepcopy(target["items"][0]["garment"]),
                }
            )
        else:
            target["_thermal"] = {
                "passed": True,
                "layer_template": "single_upper_or_one_piece",
            }

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
            if mutation in {"template", "source"}:
                selected = next(iter(budget(result)["selected_outfits"].values()))
                if mutation == "template":
                    selected["layer_template"] = "single_upper_or_one_piece"
                else:
                    selected["terms"][0]["sources"] = ["verified_thickness"]
            else:
                corrupt(result["outfits"])
            return original(self, *args)

        monkeypatch.setattr(RecommendationService, "_commit", tampered)
    value = completed(client, submit(client, "summer-tamper", request_payload()))
    assert (
        value["state"] == "failed"
        and value["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    ), value
    assert_no_history(db)
