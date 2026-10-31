"""A20f/A22: authoritative seasons, both snapshot shapes and all 16 pairs."""

import copy
from types import SimpleNamespace

import pytest
from test_thermal_budget import garment, weather

from wardrobe.seasons import (
    RULES_DIGEST,
    RULES_VERSION,
    classification,
    compatible,
)
from wardrobe.thermal_budget import (
    check_upper_budget,
    is_layerable_inner,
    thermal_context,
)

KINDS = ("spring_summer", "autumn_winter", "all_year", "unassigned")


def assigned(kind, *, category="shirt", role="upper"):
    g = garment(role, category=category)
    if kind != "unassigned":
        g["annotation_fields"]["seasons"] = {
            "value": [kind],
            "verification_state": "user_verified",
            "source_kind": "user_supplied",
        }
    return g


@pytest.mark.parametrize("a", KINDS)
@pytest.mark.parametrize("b", KINDS)
def test_a22_four_by_four_matrix_and_object_dict_parity(a, b):
    ga, gb = assigned(a), assigned(b)
    expected = (
        a != "unassigned" and b != "unassigned" and (a == b or "all_year" in {a, b})
    )
    for left in (ga, SimpleNamespace(**ga)):
        for right in (gb, SimpleNamespace(**gb)):
            assert classification(left) == a
            assert compatible(left, right) is expected


@pytest.mark.parametrize(
    "source,basis,verified,value,expected",
    [
        ("user_supplied", None, "user_verified", ["all_year"], "all_year"),
        (
            "reported_user",
            "untrusted_caller",
            "user_verified",
            ["spring_summer"],
            "autumn_winter",
        ),
        ("reported_user", None, "user_verified", ["all_year"], "autumn_winter"),
        ("observed", None, "user_verified", ["all_year"], "autumn_winter"),
        ("user_supplied", None, "unverified", ["all_year"], "autumn_winter"),
        (
            "user_supplied",
            None,
            "user_verified",
            ["all_year", "spring_summer"],
            "autumn_winter",
        ),
        ("user_supplied", None, "user_verified", ["summer"], "autumn_winter"),
    ],
)
def test_classification_trust_preserved_for_objects_and_snapshots(
    source, basis, verified, value, expected
):
    g = garment("upper", category="长袖衬衫")
    g["annotation_fields"]["seasons"] = {
        "source_kind": source,
        "verification_basis": basis,
        "verification_state": verified,
        "value": value,
    }
    assert classification(g) == classification(SimpleNamespace(**g)) == expected


@pytest.mark.parametrize(
    "sleeve,expected",
    [("长袖", "autumn_winter"), ("短袖", "unassigned"), (None, "unassigned")],
)
def test_classification_exact_sleeve_and_rules_unchanged(sleeve, expected):
    g = garment("upper", category="长袖衬衫")
    g["annotation_fields"]["sleeve"] = {"value": sleeve}
    assert classification(g) == classification(SimpleNamespace(**g)) == expected
    assert RULES_VERSION == "confirmed-seasons/1"
    # This literal is the approved Base digest, not a recomputation of the tested rules.
    assert (
        RULES_DIGEST
        == "0e38e2f1576bfa7f1785d97df5213cccd3942fdaa8dc0eb3d3c74a569274368d"
    )


@pytest.mark.parametrize(
    "category,kind,allowed",
    [
        ("长袖衬衫", "autumn_winter", True),
        ("shirt", "all_year", True),
        ("shirt", "autumn_winter", True),
        ("shirt", "spring_summer", False),
        ("shirt", "unassigned", False),
        ("针织上衣", "autumn_winter", True),
        ("长袖上衣", "autumn_winter", True),
        ("长款纽扣上衣", "autumn_winter", True),
        ("短袖衬衫", "all_year", False),
        ("连帽上衣", "autumn_winter", False),
        ("短袖连帽上衣", "all_year", False),
        ("hooded garment", "all_year", False),
    ],
)
def test_a20_layerable_category_season_and_actual_role(category, kind, allowed):
    g = assigned(kind, category=category)
    assert is_layerable_inner(g) is allowed
    assert not is_layerable_inner(g, "outerwear")
    g["roles"] = ["outerwear"]
    assert not is_layerable_inner(g)


@pytest.mark.parametrize(
    "kind,reason",
    [("spring_summer", "season_incompatible"), ("unassigned", "season_unassigned")],
)
def test_a22_pair_check_rejects_cross_season_and_unknown_even_with_all_year(
    kind, reason
):
    inner = assigned("all_year", category="短袖T恤")
    outer = assigned(kind, category="unknown-outer", role="outerwear")
    if kind == "spring_summer":
        inner = assigned("autumn_winter", category="长袖衬衫")
    before = copy.deepcopy([inner, outer])
    ctx = thermal_context(weather("22", "22"), outerwear_required=True)
    check = check_upper_budget(
        [{"role": "upper", "garment": inner}, {"role": "outerwear", "garment": outer}],
        ctx,
    )
    assert check.reason == reason
    assert [inner, outer] == before


@pytest.mark.parametrize(
    "mutation",
    [
        "short_shirt",
        "hooded",
        "wrong_category",
        "knit_outer_only",
        "cross_season",
        "unassigned_outer",
        "same_id",
        "three_layers",
    ],
)
def test_a20b_a20c_a21_authoritative_inventory_cannot_authorize_bad_layers(mutation):
    from test_recommendation_review_regressions import payload

    from wardrobe.errors import BusinessError
    from wardrobe.recommendations import validate_outfits
    from wardrobe.thermal_budget import FirstChoiceGuard

    upper = assigned("all_year", category="长袖衬衫")
    outer = garment("outerwear", "medium")
    lower, shoe = garment("lower"), garment("footwear")
    gs = [upper, outer, lower, shoe]
    if mutation == "short_shirt":
        upper["category"] = "短袖衬衫"
    elif mutation == "hooded":
        upper["category"] = "连帽上衣"
    elif mutation == "wrong_category":
        upper["category"] = "长裤"  # stored upper role cannot defeat the category rule
    elif mutation == "knit_outer_only":
        upper["category"], upper["roles"] = "针织上衣", ["outerwear"]
    elif mutation == "cross_season":
        upper["category"] = "短袖T恤"
        upper["annotation_fields"]["seasons"]["value"] = ["spring_summer"]
    elif mutation == "unassigned_outer":
        outer["category"] = "unknown-outer"
    elif mutation == "same_id":
        upper["id"] = outer["id"]
    candidate_items = [
        {"role": role, "garment": g}
        for role, g in zip(("upper", "outerwear", "lower", "footwear"), gs, strict=True)
    ]
    if mutation == "three_layers":
        inner = garment("upper", identifier="third")
        gs.append(inner)
        candidate_items.append({"role": "base_layer", "garment": inner})
    # Every garment snapshot exactly matches the authority. The structural and
    # seasonal rules themselves must reject this, rather than snapshot mismatch.
    with pytest.raises(BusinessError) as error:
        validate_outfits(
            [{"items": candidate_items}],
            gs,
            payload(),
            weather("16.6", "23.2"),
            FirstChoiceGuard(None, True),
        )
    assert error.value.code == "OUTFIT_CONSTRAINT_VIOLATION"


def test_a6_o18_unknown_shirt_estimate_does_not_clear_pending_or_write_catalog():
    from decimal import Decimal

    from wardrobe.recommendations import temperature_status
    from wardrobe.thermal_budget import layer_weight

    g = assigned("autumn_winter", category="shirt")
    g["annotation_fields"]["thickness"] = {
        "value": "thick",
        "verification_state": "unverified",
    }
    before = copy.deepcopy(g)
    w = weather()
    term = layer_weight(g, "upper", thermal_context(w, outerwear_required=True))
    assert term.value == Decimal("1.5") and term.thickness is None
    assert term.sources == ("layerable_inner_budget_assumption",)
    assert temperature_status(g, "upper", w) == "pending"
    assert g == before
