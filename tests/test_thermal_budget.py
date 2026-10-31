"""O1–O8, A1–A11: hand-computed decimal boundaries and synthetic garments."""

# ruff: noqa: FURB157 -- keep decimal constants in explicit decimal-string form.

import copy
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_recommendation_review_regressions import payload

from wardrobe.recommendations import (
    candidates_for,
    role_satisfies,
    temperature_status,
)
from wardrobe.thermal_budget import (
    FirstChoiceGuard,
    assumptions_for,
    budget_reason,
    check_upper_budget,
    first_outfit_allowed,
    full_day_allowed,
    layer_weight,
    median,
    thermal_context,
)


def weather(low="16.6", high="23.2", **stats):
    return SimpleNamespace(
        temperature_min=low,
        temperature_max=high,
        feels_like_min=low,
        feels_like_max=high,
        **stats,
    )


def garment(
    role,
    thickness="thin",
    *,
    identifier=None,
    category=None,
    verified=True,
    warmth=None,
):
    return {
        "id": identifier or f"{role}-{thickness}",
        "roles": [role],
        "category": category
        or {
            "upper": "短袖T恤",
            "lower": "长裤",
            "outerwear": "外套",
            "one_piece": "连体衣",
            "footwear": "鞋",
        }[role],
        "annotation_fields": {
            "sleeve": {
                "value": "短袖"
                if (category or "") in {"短袖衬衫", "短袖T恤"}
                or role == "upper"
                and category is None
                else "长袖"
            },
            "thickness": {
                "value": thickness,
                "verification_state": "user_verified" if verified else "unverified",
            },
            "warmth": {"value": warmth, "verification_state": "user_verified"},
        },
        "recommendation_eligible": True,
        "availability": "unknown",
    }


def items(upper="thin", outer="medium"):
    return [
        {"role": role, "garment": garment(role, thickness)}
        for role, thickness in [("upper", upper), ("outerwear", outer)]
    ]


def wardrobe(upper="thin", outer="medium", lower="thin"):
    return [
        garment("upper", upper),
        garment("outerwear", outer),
        garment("lower", lower),
        garment("footwear"),
    ]


def test_a2_exact_decimal_midpoint_and_a1_paired_counterexample():
    a2 = thermal_context(weather("18.8", "22.3"), outerwear_required=True)
    assert (a2.reference, a2.gap, a2.lower, a2.upper) == (
        D("20.55"),
        D("5.45"),
        D("3.45"),
        D("7.45"),
    )
    assert (a2.basis, a2.method, a2.sample_count) == ("feels_like", "range_midpoint", 0)
    for upper, outer, weight, reason in [
        ("thin", "medium", "5", None),
        ("medium", "medium", "7", None),
        ("thin", "thick", "7.5", "over_budget"),
        ("thin", "thin", "3", "under_budget"),
    ]:
        check = check_upper_budget(items(upper, outer), a2)
        assert (check.weight, check.reason) == (D(weight), reason)
    a1 = thermal_context(weather())
    assert (a1.reference, a1.gap, a1.upper) == (D("19.9"), D("6.1"), D("8.1"))
    assert check_upper_budget(items("thin", "thick"), a1).passed
    assert not check_upper_budget(items("thin", "thick"), a2).passed
    assert float(a2.upper) == 7.45  # display never participates in comparisons
    assert budget_reason(D("7.5"), a2) == "over_budget"


@pytest.mark.parametrize(
    "weight,reason",
    [
        ("7.45", None),
        ("7.4501", "over_budget"),
        ("3.45", None),
        ("3.4499", "under_budget"),
    ],
)
def test_a2_closed_bounds_and_micro_crossings(weight, reason):
    assert budget_reason(D(weight), thermal_context(weather("18.8", "22.3"))) == reason


def test_o1_medians_precedence_fallback_clipping_and_odd_even():
    assert median(["18.8", "22.3"]) == D("20.55")
    assert median(["0", "-5", "-1"]) == D("-1")
    hourly = thermal_context(
        weather(
            feels_like_median=D("21"),
            feels_like_sample_count=15,
            temperature_median=D("20"),
            temperature_sample_count=15,
        )
    )
    assert (hourly.reference, hourly.method, hourly.sample_count) == (
        D("21"),
        "hourly_median",
        15,
    )
    assert thermal_context(
        weather(temperature_median=D("20"), temperature_sample_count=15)
    ).reference == D("19.9")
    missing = weather(temperature_median=D("21"), temperature_sample_count=15)
    missing.feels_like_min = missing.feels_like_max = None
    ctx = thermal_context(missing)
    assert (ctx.reference, ctx.basis, ctx.method) == (
        D("21"),
        "temperature",
        "hourly_median",
    )
    assert thermal_context(weather("-5", "-1")).gap == D("12")
    assert thermal_context(weather("29", "30")).gap == D("0")


@pytest.mark.parametrize(
    "low,allowed",
    [
        ("9.999", {"thick", "very_thick"}),
        ("10", {"thin", "medium", "thick", "very_thick"}),
        ("15.999", {"thin", "medium", "thick", "very_thick"}),
        ("16", {"thin", "medium"}),
    ],
)
@pytest.mark.parametrize("thickness", ["thin", "medium", "thick", "very_thick"])
def test_a10_a11_pants_temperature_matrix(low, allowed, thickness):
    pants = garment("lower", thickness)
    value = role_satisfies(
        pants, "lower", payload(occasion="full_day"), weather(low, low)
    )
    assert value is (thickness in allowed)


@pytest.mark.parametrize(
    "occasion,allowed",
    [
        ("full_day", True),
        (" ALL_DAY ", True),
        ("Full_Day_Out", True),
        ("全天出门", True),
        ("全天外出", True),
        ("daily", False),
        ("work", False),
        ("commute", False),
        ("weekend", False),
        ("非全天出门", False),
        ("上班不全天出门", False),
        ("", False),
    ],
)
def test_a10_a11_full_day_exact_synonyms(occasion, allowed):
    assert full_day_allowed(occasion) is allowed
    if occasion:
        assert (
            role_satisfies(
                garment("lower", "very_thick"),
                "lower",
                payload(occasion=occasion),
                weather("12", "12"),
            )
            is allowed
        )
    assert role_satisfies(
        garment("outerwear", "very_thick"), "outerwear", payload(), weather("8", "8")
    )


def test_a3_hot_endpoint_and_non_pants_cold_regression():
    assert (
        temperature_status(
            garment("outerwear", "thick"), "outerwear", weather("20", "29")
        )
        == "conflict"
    )
    assert (
        temperature_status(
            garment("lower", "thin", category="裙"), "lower", weather("8", "8")
        )
        == "conflict"
    )
    pants = garment("lower", "medium", warmth="high")
    assert not role_satisfies(pants, "lower", payload(), weather("8", "8"))
    hot = weather("20", "28.001")
    hot.feels_like_max = "28"
    assert temperature_status(garment("upper", "medium"), "upper", hot) == "conflict"
    assert (
        temperature_status(garment("upper", "medium"), "upper", weather("20", "28"))
        == "eligible"
    )


def test_a4_o6_cold_medium_outer_pair_positive_and_negative():
    ctx = thermal_context(weather("8", "8"), outerwear_required=True)
    assert (
        temperature_status(
            garment("outerwear", "medium"), "outerwear", weather("8", "8")
        )
        == "eligible"
    )
    assert check_upper_budget(items("very_thick", "medium"), ctx).weight == D("12.5")
    assert check_upper_budget(items("very_thick", "medium"), ctx).passed
    assert check_upper_budget(items("thick", "medium"), ctx).reason == "under_budget"
    assert not candidates_for(
        wardrobe("thick", "medium", "thick"), payload(), weather("8", "8")
    )[0]
    assert candidates_for(
        wardrobe("very_thick", "medium", "thick"), payload(), weather("8", "8")
    )[0]


def test_a6_unknowns_estimation_only_for_eligible_required_tshirt():
    g = garment("upper", "thick", verified=False)
    before = copy.deepcopy(g)
    required = thermal_context(weather(), outerwear_required=True)
    term = layer_weight(g, "upper", required)
    assert term.value == D("1.5") and term.sources == (
        "layerable_inner_budget_assumption",
    )
    assert layer_weight(g, "upper", thermal_context(weather())).value is None
    assert g == before
    long_t = garment("upper", None, category="长袖T恤")
    assert temperature_status(long_t, "upper", weather()) == "pending"
    assert (
        temperature_status(
            garment("outerwear", "medium", verified=False), "outerwear", weather()
        )
        == "pending"
    )
    high = layer_weight(
        garment("upper", "thin", warmth="high"),
        "upper",
        thermal_context(weather("8", "8")),
    )
    assert high.value == D("6") and "verified_warmth_high_cold" in high.sources


def test_a1c_o8_two_terms_lower_excluded_and_one_piece_mapping():
    ctx = thermal_context(weather(), outerwear_required=True)
    assert check_upper_budget(items("medium", "thin"), ctx).passed
    check_items = items() + [
        {"role": "lower", "garment": garment("lower", "very_thick")},
        {"role": "one_piece", "garment": garment("one_piece", "very_thick")},
    ]
    check = check_upper_budget(check_items, ctx)
    assert check.weight == D("5") and len(check.terms) == 2 and check.unmodeled_extra
    assert any(
        "未建模额外保暖" in n for n in assumptions_for([{"items": check_items}], ctx)
    )
    solo = check_upper_budget(
        [{"role": "one_piece", "garment": garment("one_piece", "medium")}], ctx
    )
    assert solo.weight == D("3.5") and solo.terms[0].role == "one_piece"
    warm = thermal_context(
        weather("20", "26", feels_like_median=D("23"), feels_like_sample_count=3),
        outerwear_required=True,
    )
    assert check_upper_budget(items("thin", "thin"), warm).passed


def test_o11_replaces_distance_fallback_and_keeps_gap_hard_threshold():
    ctx = thermal_context(weather())
    candidate = {"items": items("thin", "thick")}
    for distance, complete in [
        (D("1.1"), True),
        (D("1.4"), True),
        (None, False),
        (None, True),
    ]:
        assert not first_outfit_allowed(
            candidate, ctx, FirstChoiceGuard(distance, complete)
        )
    for temp, expected in [("19.001", "conflict"), ("19", "eligible")]:
        assert (
            temperature_status(
                garment("outerwear", "very_thick"), "outerwear", weather(temp, temp)
            )
            == expected
        )


def test_search_prechecks_pairs_before_500_and_keeps_tail_cores():
    garments = wardrobe("medium", "thick")
    garments += [
        garment("outerwear", "thick", identifier=f"bad-{i:03}") for i in range(80)
    ]
    garments += [garment("outerwear", "thin", identifier="zz-only-legal")]
    candidates, search = candidates_for(garments, payload(), weather())
    assert candidates and all(
        next(i["garment"]["id"] for i in c["items"] if i["role"] == "outerwear")
        == "zz-only-legal"
        for c in candidates
    )
    assert (
        search["layering_check"]["thermal_budget"]["reason_counts"]["over_budget"] >= 80
    )
    reverse, _ = candidates_for(list(reversed(garments)), payload(), weather())
    assert [c["signature"] for c in candidates] == [c["signature"] for c in reverse]
    assert candidates_for(garments, payload(core_ids=["zz-only-legal"]), weather())[0]


def test_o5_witness_requires_feasible_completion_and_respects_core_exclude():
    garments = wardrobe(outer="thick")
    light = garment("outerwear", "medium")
    garments.append(light)
    candidates, search = candidates_for(garments, payload(), weather())
    assert search.guard.best_light_distance == D("1.1")
    thick = next(
        c
        for c in candidates
        if any(i["garment"]["id"] == "outerwear-thick" for i in c["items"])
    )
    assert not first_outfit_allowed(thick, thermal_context(weather()), search.guard)
    for req in (
        payload(exclude_ids=[light["id"]]),
        payload(core_ids=["outerwear-thick"]),
    ):
        values, diagnostics = candidates_for(garments, req, weather())
        assert values and diagnostics.guard.best_light_distance is None
        assert not first_outfit_allowed(
            values[0], thermal_context(weather()), diagnostics.guard
        )
    # A light coat sharing the only lower ID cannot form a complete alternative.
    garments[-1]["id"] = garments[2]["id"]
    _, search = candidates_for(garments, payload(), weather())
    assert search.guard.best_light_distance is None


def test_o5_repeated_allowed_first_beats_fresh_forbidden_thick():
    from wardrobe.recommendations import choose

    garments = wardrobe(outer="thick") + [garment("outerwear", "medium")]
    candidates, search = candidates_for(garments, payload(count=1), weather())
    evaluated = [{**c, "quality_score": 1} for c in candidates]
    light = next(c for c in evaluated if c["_first_allowed"])
    history = [
        {
            "kind": "generated",
            "occurred_at": 100,
            "payload": {
                "signature": light["signature"],
                "garment_ids": [i["garment"]["id"] for i in light["items"]],
            },
        }
    ]
    result, _ = choose(
        evaluated,
        history,
        payload(count=1),
        100,
        thermal_context(weather()),
        search.guard,
    )
    assert (
        result[0]["signature"] == light["signature"] and result[0]["exact_repeat_14d"]
    )


def test_pair_state_and_time_caps_are_honest_and_never_allow_unproven_thick(
    monkeypatch,
):
    from wardrobe import recommendations

    garments = [garment("upper", identifier=f"u-{n:03}") for n in range(100)]
    garments += [
        garment("outerwear", "thick", identifier=f"o-{n:03}") for n in range(100)
    ]
    garments += [garment("lower"), garment("footwear")]
    candidates, search = candidates_for(garments, payload(), weather())
    diagnostics = search["layering_check"]["thermal_budget"]
    assert diagnostics["pair_states_checked"] == 10_000
    assert diagnostics["pair_search_truncated"] and search["search_truncated"]
    assert len(candidates) <= 500
    assert all(
        not first_outfit_allowed(c, thermal_context(weather()), search.guard)
        for c in candidates
    )
    clock = iter([0, 3])
    monkeypatch.setattr(recommendations.time, "monotonic", lambda: next(clock, 3))
    _, short = candidates_for(wardrobe(outer="thick"), payload(), weather())
    assert short["layering_check"]["thermal_budget"]["pair_search_truncated"]


def test_shortlist_keeps_thermal_choice_ahead_of_diversity_and_quality():
    from wardrobe.outfit_provider import shortlist

    ctx = thermal_context(weather())
    candidates = [
        {
            "candidate_id": str(n),
            "items": items("thin", "thick"),
            "_first_allowed": False,
            "_thermal": check_upper_budget(items("thin", "thick"), ctx),
        }
        for n in range(50)
    ]
    better = {
        "candidate_id": "zz",
        "items": items(),
        "_first_allowed": True,
        "_thermal": check_upper_budget(items(), ctx),
    }
    selected = shortlist(candidates + [better])
    assert len(selected) == 24 and selected[0] is better


def test_s1_prime_a12_new_lower_bound_vs_old_and_three_keys():
    from wardrobe.thermal_budget import thermal_sort_key

    ctx = thermal_context(
        weather("16", "22.9", feels_like_median=D("19.4")), outerwear_required=True
    )
    assert (ctx.gap, ctx.lower, ctx.upper) == (D("6.6"), D("4.6"), D("8.6"))
    assert D("5.0") < ctx.gap - D("1.5")  # old lower bound only, no runtime switch
    middle = check_upper_budget(items("thin", "medium"), ctx)
    pair = check_upper_budget(items("medium", "medium"), ctx)
    assert middle.passed and middle.weight == D("5.0")
    assert check_upper_budget(items("thin", "thin"), ctx).reason == "under_budget"
    assert thermal_sort_key(pair) == (1, -2, D("0.4"))
    assert thermal_sort_key(middle) == (1, -2, D("1.6"))
    assert not first_outfit_allowed(
        {"items": items("thin", "thick")}, ctx, FirstChoiceGuard(None, True)
    )


@pytest.mark.parametrize(
    "reference,hottest,upper,outer,light,complete,allowed,reason",
    [
        (
            "14",
            "21.999",
            "thick",
            "thick",
            None,
            True,
            True,
            "medium_pair_insufficient",
        ),
        (
            "14",
            "22",
            "thick",
            "thick",
            None,
            True,
            False,
            "hottest_heavy_first_blocked",
        ),
        (
            "14",
            "22",
            "thin",
            "very_thick",
            None,
            True,
            False,
            "hottest_heavy_first_blocked",
        ),
        ("8", "9", "thin", "very_thick", None, True, True, "very_cold_no_light"),
        (
            "8",
            "22",
            "thin",
            "very_thick",
            None,
            True,
            False,
            "hottest_heavy_first_blocked",
        ),
        ("9.999", "21", "thin", "very_thick", None, True, True, "very_cold_no_light"),
        (
            "10",
            "21",
            "thin",
            "very_thick",
            None,
            True,
            True,
            "medium_pair_insufficient",
        ),
        ("14", "21", "thick", "thick", D("1"), True, False, "light_complete_available"),
        ("14", "21", "thick", "thick", None, False, False, "search_incomplete"),
        (
            "17",
            "21",
            "medium",
            "thick",
            None,
            True,
            False,
            "mild_light_capacity_sufficient",
        ),
    ],
)
def test_a13_a15_a16_state_machine(
    reference, hottest, upper, outer, light, complete, allowed, reason
):
    from wardrobe.thermal_budget import first_choice_decision

    ctx = thermal_context(
        weather("5", hottest, feels_like_median=D(reference)), outerwear_required=True
    )
    assert ctx.very_cold is (D(reference) < D("10"))
    choice = first_choice_decision(
        {"items": items(upper, outer)}, ctx, FirstChoiceGuard(light, complete)
    )
    assert (choice.allowed, choice.reason) == (allowed, reason)
    assert isinstance(
        first_outfit_allowed(
            {"items": items(upper, outer)}, ctx, FirstChoiceGuard(light, complete)
        ),
        bool,
    )
    if reference == "17":
        assert ctx.lower == D("7.0") and not ctx.medium_pair_insufficient


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "-Infinity"])
def test_decimal_rejects_nonfinite_and_bool(value):
    from wardrobe.thermal_budget import decimal

    with pytest.raises((ValueError, TypeError)):
        decimal(value)


def test_unknown_rank_count_and_verified_warmth_do_not_invent_thickness():
    from wardrobe.thermal_budget import thermal_sort_key

    ctx = thermal_context(weather("24", "24"))
    g = garment("upper", None)
    check = check_upper_budget([{"role": "upper", "garment": g}], ctx)
    assert check.passed and thermal_sort_key(check) == (4, 0, D("2"))
    high = garment("upper", None, warmth="high")
    term = layer_weight(
        high,
        "upper",
        thermal_context(weather("8", "8")),
        template="single_upper_or_one_piece",
    )
    assert (
        term.value == D("6") and term.thickness is None and term.rank_thickness is None
    )


def test_o17_search_shortlist_choose_three_keys_before_history_and_quality():
    from wardrobe.outfit_provider import shortlist
    from wardrobe.recommendations import choose

    w = weather("20", "24", feels_like_median=D("22"))
    garments = wardrobe(outer="medium") + [garment("outerwear", "thin")]
    garments[0]["category"] = "长袖衬衫"
    garments[0]["annotation_fields"]["sleeve"] = {"value": "长袖"}
    candidates, search = candidates_for(garments, payload(count=1), w)
    assert candidates[0]["_thermal"].weight == D("3")
    evaluated = [
        {**c, "quality_score": 0.6 if c["_thermal"].weight == D("3") else 1}
        for c in candidates
    ]
    preferred = candidates[0]
    history = [
        {
            "kind": "worn",
            "occurred_at": 100,
            "payload": {
                "signature": preferred["signature"],
                "garment_ids": [i["garment"]["id"] for i in preferred["items"]],
            },
        }
    ]
    assert (
        shortlist(list(reversed(candidates)))[0]["signature"] == preferred["signature"]
    )
    chosen, _ = choose(
        evaluated,
        history,
        payload(count=1),
        100,
        thermal_context(w, outerwear_required=True),
        search.guard,
    )
    assert (
        chosen[0]["signature"] == preferred["signature"]
        and chosen[0]["exact_repeat_14d"]
    )


def test_a19b_search_prefers_medium_pair_whole_stack_rank():
    w = weather("16", "22.9", feels_like_median=D("19.4"))
    garments = wardrobe(outer="medium") + [garment("upper", "medium")]
    candidates, _ = candidates_for(garments, payload(), w)
    assert candidates[0]["_thermal"].weight == D("7.0")
