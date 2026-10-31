"""Durable jobs, bounded candidate search and atomic recommendation history."""

# ruff: noqa: FURB157 -- keep decimal constants in explicit decimal-string form.

import asyncio
import copy
import itertools
import logging
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from .auth import Authenticator, recheck_grant
from .catalog import CatalogService
from .contracts import Principal, StrictModel, success
from .errors import BusinessError
from .image_previews import image_preview_response
from .models import (
    Asset,
    ClientGrant,
    Garment,
    HistoryEvent,
    Recommendation,
    ServiceState,
)
from .seasons import classification, context, preference
from .thermal_budget import (
    OUTERWEAR_CATEGORIES,
    REASONS,
    TSHIRT_CATEGORIES,
    FirstChoiceGuard,
    assumptions_for,
    check_upper_budget,
    confirmed_value,
    decimal,
    first_choice_decision,
    first_outfit_allowed,
    full_day_allowed,
    is_layerable_inner,
    item_reason,
    outer_thickness,
    outerwear_has_layerable_inner,
    role_assignment_allowed,
    selected_output,
    summer_layering_enabled,
    thermal_context,
    thermal_sort_key,
)
from .weather import (
    DisabledWeather,
    Interval,
    Location,
    WeatherSnapshot,
    evaluator_weather,
    instant,
    missing_feels_note,
    weather_context,
    weather_output,
)
from .writes import digest, idempotent


class RecommendationInput(StrictModel):
    location: Location
    expected_season_revision: int | None = Field(default=None, ge=1)
    target_date: str | None = Field(
        default=None,
        pattern=r"^(?:\d{4}-\d{2}-\d{2}|today|tomorrow|day_after_tomorrow)$",
        description="ISO date or relative day resolved by the server in location.timezone at first submission; use tomorrow for 明天, never guess today's date.",
    )
    interval: Interval | None = None
    occasion: str = Field(default="daily", min_length=1, max_length=100)
    count: int = Field(default=2, ge=1, le=3)
    core_ids: list[str] = Field(default_factory=list, max_length=8)
    exclude_ids: list[str] = Field(default_factory=list, max_length=100)
    allow_rewear: bool = False
    required_functions: dict[
        Literal["upper", "lower", "footwear", "outerwear"],
        list[Literal["waterproof", "windproof"]],
    ] = Field(default_factory=dict)
    user_weather: WeatherSnapshot | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (
            set(self.core_ids) & set(self.exclude_ids)
            or self.interval is not None
            and self.target_date is not None
        ):
            raise ValueError("Core/exclude or date/interval conflict")
        if self.target_date is not None and self.target_date not in {
            "today",
            "tomorrow",
            "day_after_tomorrow",
        }:
            date.fromisoformat(self.target_date)
        if (
            self.user_weather is not None
            and self.user_weather.source_kind != "user_input"
        ):
            raise ValueError("Caller weather must be marked user_input")
        return self


class Evaluation(StrictModel):
    candidate_id: str
    suitable: bool
    color: float = Field(ge=0, le=1)
    material: float = Field(ge=0, le=1)
    style: float = Field(ge=0, le=1)
    silhouette: float = Field(ge=0, le=1)
    # Evidence references rather than unbounded invented descriptions.
    evidence: dict[str, list[str]] = Field(default_factory=dict)


class DisabledEvaluator:
    async def evaluate(self, candidates, weather, occasion):
        raise BusinessError(
            "MODEL_NOT_CONFIGURED",
            "An authorized outfit evaluator must be configured before quality-assessed recommendations",
            503,
        )


class FeedbackInput(StrictModel):
    recommendation_id: str
    outfit_id: str
    kind: Literal["shown", "adopted", "worn", "rejected"]
    occurred_at: int | None = None
    note: str | None = Field(default=None, max_length=500)


def _value(garment, field):
    return garment["annotation_fields"].get(field, {}).get("value")


def _candidate(items):
    links = [{"role": role, "garment_id": garment["id"]} for role, garment in items]
    signature = digest(
        sorted(links, key=lambda link: (link["role"], link["garment_id"]))
    )
    return {
        "candidate_id": signature,
        "signature": signature,
        "items": [{"role": role, "garment": garment} for role, garment in items],
    }


def outfit_explanation(outfit, weather, ctx=None, guard=None):
    """Expose exact stored values and uncertainty instead of invented prose."""
    dimensions = {
        "color": ("配色依据", ["primary_color", "secondary_colors", "pattern"]),
        "material": ("材质与厚薄依据", ["material", "thickness", "warmth"]),
        "style": ("风格依据", ["style", "formality"]),
        "silhouette": ("廓形依据", ["silhouette", "length", "fit"]),
    }
    evidence = {}
    for dimension, (label, fields) in dimensions.items():
        evidence[dimension] = {
            "label": label,
            "score": outfit["assessment"][dimension],
            "items": [
                {
                    "garment_id": item["garment"]["id"],
                    "role": item["role"],
                    "fields": {
                        key: item["garment"]["annotation_fields"][key]
                        for key in fields
                        if _value(item["garment"], key) is not None
                    },
                }
                for item in outfit["items"]
            ],
        }
    feels_text = (
        missing_feels_note(weather)
        if weather.feels_like_missing
        else f"体感 {weather.feels_like_min:g}–{weather.feels_like_max:g}°C"
    )
    note = ""
    if ctx is not None:
        check = check_upper_budget(outfit["items"], ctx)
        note = f"；本套上身约 {check.weight}℃，参考缺口约 {ctx.gap}℃（保暖估算）"
        decision = first_choice_decision(outfit, ctx, guard)
        if outer_thickness(outfit) in {"thick", "very_thick"}:
            if decision.allowed and decision.reason == "medium_pair_insufficient":
                note += "；中等内搭＋中等外套的等效7.0℃仍低于本次下沿，因此本套使用厚外套兜底"
            elif decision.allowed and decision.reason == "very_cold_no_light":
                note += "；体感参考中位低于10℃，未找到合法的薄／中外套完整组合，使用厚外套兜底"
            else:
                note += "；本套偏厚，仅作后续备选"
        if check.template == "summer_open_shirt":
            note += "；T恤内搭＋短袖衬衫敞开外层；两层季节兼容"
        if outfit.get("repeat_reason") == "thermal_first_choice_constraint":
            note += "；受厚度首套约束沿用重复方案，保留14天重复标记"
        if check.unmodeled_extra:
            note += "；连体衣未建模额外保暖"
    return {
        "weather": f"气温 {weather.temperature_min:g}–{weather.temperature_max:g}°C；{feels_text}；降水概率 {weather.precipitation_probability:.0%}；风速最高 {weather.wind_max_m_s:g} m/s{note}",
        "dimensions": evidence,
        "uncertainty": "照片观察与未核实标签不等于确定事实；未知材质、防水或保暖性能不能作为保证。",
    }


def _balanced_product(roles, buckets):
    """Diagonal first sweep covers every value before expanding combinations.

    Rotate the largest role in the inner loop; offsets of the remaining roles
    enumerate every tuple once. This avoids spending the entire bound on the
    first upper garment as a lexicographic Cartesian product would.
    """
    if not roles:
        yield []
        return
    roles = sorted(roles, key=lambda role: -len(buckets[role]))
    largest, *other = roles
    for offsets in itertools.product(*(range(len(buckets[role])) for role in other)):
        for index, garment in enumerate(buckets[largest]):
            yield [(largest, garment)] + [
                (
                    role,
                    buckets[role][(index + offset + position + 1) % len(buckets[role])],
                )
                for position, (role, offset) in enumerate(
                    zip(other, offsets, strict=True)
                )
            ]


def _core_assignments(cores, roles, buckets, assigned=None, spend=None):
    assigned = assigned or {}
    if not cores:
        yield assigned
        return
    first, *rest = cores
    for role in roles:
        if spend is not None and not spend():
            return
        if role not in assigned and any(g["id"] == first["id"] for g in buckets[role]):
            yield from _core_assignments(
                rest, roles, buckets, {**assigned, role: first}, spend
            )


def _interleave(groups):
    pending = deque(groups)
    while pending:
        group = pending.popleft()
        try:
            yield next(group)
        except StopIteration:
            continue
        pending.append(group)


def temperature_status(garment, role, weather, ctx=None):
    """Reject clear thermal conflicts; unknown facts never become a neutral pass."""
    if role == "base_layer":
        role = "upper"
    if role == "footwear":
        return "eligible"
    thickness = confirmed_value(garment, "thickness")
    warmth = confirmed_value(garment, "warmth")
    ctx = ctx or thermal_context(weather)
    hottest, coldest = ctx.hottest, ctx.coldest
    # Explicit user policy: shorts and short sleeves need no thickness label.
    # This is a shape exemption, not a fabricated thickness/warmth value.
    short = (role == "lower" and garment.get("category") == "短裤") or (
        role == "upper"
        and (
            garment.get("category") in {"短袖T恤", "短袖衬衫", "短袖连帽上衣"}
            or _value(garment, "sleeve") == "短袖"
        )
    )
    if hottest > Decimal("28") and (
        warmth == "high" or thickness in {"medium", "thick", "very_thick"}
    ):
        return "conflict"
    if short:
        return "conflict" if coldest < Decimal("10") and role == "lower" else "eligible"
    if thickness not in {"thin", "medium", "thick", "very_thick"}:
        return "pending"
    if hottest > Decimal("28") and (thickness != "thin" or warmth == "high"):
        return "conflict"
    if role == "outerwear" and ctx.gap < Decimal("7") and thickness == "very_thick":
        return "conflict"
    if role == "lower" and garment.get("category") == "长裤":
        if coldest < Decimal("10") and thickness not in {"thick", "very_thick"}:
            return "conflict"
        if coldest >= Decimal("16") and thickness not in {"thin", "medium"}:
            return "conflict"
    if coldest < Decimal("10"):
        if role == "outerwear" and thickness == "thin" and warmth != "high":
            return "conflict"
        if role == "lower" and thickness == "thin":
            return "conflict"
    return "eligible"


def outerwear_requirement(garments, payload, weather):
    """One trusted decision shared by candidate generation and both guards."""
    feels = weather.feels_like_min
    missing = feels is None
    minimum = getattr(weather, "temperature_min", feels)
    comparison = minimum if missing else feels
    weather_required = decimal(comparison) < Decimal("22")
    core_outer = any(
        g["id"] in payload.core_ids
        and ("outerwear" in g["roles"] or g.get("category") in OUTERWEAR_CATEGORIES)
        for g in garments
    )
    return {
        "feels_like_min": feels,
        "temp_min": minimum,
        "feels_like_missing": missing,
        "weather_outerwear_required": weather_required,
        "outerwear_required": weather_required
        or "outerwear" in payload.required_functions
        or core_outer,
    }


def role_satisfies(garment, role, payload, weather, ctx=None):
    if role == "base_layer":
        role = "upper"
    ctx = ctx or thermal_context(weather, full_day=full_day_allowed(payload.occasion))
    if item_reason(garment, role, ctx):
        return False
    if temperature_status(garment, role, weather, ctx) != "eligible":
        return False
    if role == "upper" and garment.get("category") in OUTERWEAR_CATEGORIES:
        return False
    if payload.occasion == "formal" and _value(garment, "formality") != "formal":
        return False
    requirements = payload.required_functions.get(role, [])
    if role == "one_piece":
        requirements = [
            *requirements,
            *payload.required_functions.get("upper", []),
            *payload.required_functions.get("lower", []),
        ]
    return all(
        garment["annotation_fields"].get(function, {}).get("verification_state")
        == "user_verified"
        and _value(garment, function) is True
        for function in requirements
    )


def validate_outfits(outfits, garments, payload, weather, guard=None):
    """Validate the entire batch against server facts, never model diagnostics."""
    facts = {g["id"]: g for g in garments}
    required = outerwear_requirement(garments, payload, weather)["outerwear_required"]
    ctx = thermal_context(
        weather,
        outerwear_required=required,
        full_day=full_day_allowed(payload.occasion),
    )
    if guard is None:
        _, domain = candidates_for(garments, payload, weather)
        guard = domain.guard
    for index, outfit in enumerate(outfits):
        items = outfit["items"]
        ids = [i["garment"]["id"] for i in items]
        roles = [i["role"] for i in items]
        check = check_upper_budget(items, ctx)
        invalid = (
            len(ids) != len(set(ids))
            or len(roles) != len(set(roles))
            or not set(payload.core_ids) <= set(ids)
            or bool(set(ids) & set(payload.exclude_ids))
            or required
            and "outerwear" not in roles
            or not outerwear_has_layerable_inner(items)
            or not check.passed
            or bool(
                {"upper_warmth_c", "W_upper", "passed", "thermal_budget", "_thermal"}
                & outfit.keys()
            )
            or index == 0
            and not first_outfit_allowed(outfit, ctx, guard)
            or set(roles)
            not in (
                {"upper", "lower", "footwear", "outerwear"},
                {"one_piece", "upper", "footwear", "outerwear"},
                {"upper", "lower", "footwear"},
                {"one_piece", "footwear"},
                {"base_layer", "upper", "lower", "footwear"},
            )
        )
        for item in items:
            garment = facts.get(item["garment"]["id"])
            invalid = invalid or (
                garment is None
                or item["garment"] != garment
                or not garment["recommendation_eligible"]
                or not role_assignment_allowed(garment, item["role"], check.template)
                or not role_satisfies(garment, item["role"], payload, weather, ctx)
            )
        if invalid:
            raise BusinessError(
                "OUTFIT_CONSTRAINT_VIOLATION",
                "Outfit violates inventory, core/exclusion, layer structure, season or thermal constraints",
                502,
            )


def candidates_for(garments, payload, weather, *, deadline=None):
    """At most 500 complete variants, with cores bound before the search cap."""
    stop_at = time.monotonic() + min(
        2, max(0, deadline - time.time()) if deadline is not None else 2
    )
    requirement = outerwear_requirement(garments, payload, weather)
    ctx = thermal_context(
        weather,
        outerwear_required=requirement["outerwear_required"],
        full_day=full_day_allowed(payload.occasion),
    )
    reasons = Counter()
    eligible_before_exclusion = [g for g in garments if g["recommendation_eligible"]]
    eligible = [
        g for g in eligible_before_exclusion if g["id"] not in payload.exclude_ids
    ]
    outer_before_exclusion = {
        g["id"]
        for g in eligible_before_exclusion
        if "outerwear" in g["roles"]
        and role_satisfies(g, "outerwear", payload, weather, ctx)
    }

    pending, blocked = {}, set()

    def role_eligible(garment, role):
        thermal = temperature_status(garment, role, weather, ctx)
        reason = item_reason(garment, role, ctx)
        if reason:
            reasons[reason] += 1
        if thermal == "pending":
            pending[garment["id"]] = {
                k: garment.get(k) for k in ("id", "name", "category")
            }
        if thermal != "eligible":
            blocked.add(garment["id"])
            return False
        return role_satisfies(garment, role, payload, weather, ctx)

    buckets = {
        role: sorted(
            [g for g in eligible if role in g["roles"] and role_eligible(g, role)],
            key=lambda g: g["id"],
        )
        for role in ("upper", "lower", "footwear", "one_piece", "outerwear")
    }
    selected, seen, searched = [], set(), 0
    requirement = outerwear_requirement(garments, payload, weather)
    needs_outer = requirement["outerwear_required"]
    if needs_outer:
        buckets["upper"] = [g for g in buckets["upper"] if is_layerable_inner(g)]
    summer_enabled = summer_layering_enabled(ctx)
    buckets["base_layer"] = sorted(
        [
            g
            for g in eligible
            if summer_enabled
            and role_assignment_allowed(g, "base_layer", "summer_open_shirt")
            and classification(g) in {"spring_summer", "all_year"}
            and role_eligible(g, "base_layer")
        ],
        key=lambda g: g["id"],
    )
    layering = {
        "rule_version": "outerwear-layerable-inner/1",
        **requirement,
        "all_eligible_outerwear_excluded": bool(outer_before_exclusion)
        and outer_before_exclusion <= set(payload.exclude_ids),
        "eligible_layerable_inner_count": sum(
            is_layerable_inner(g) for g in buckets["upper"]
        ),
        "eligible_tshirt_count": sum(
            g.get("category") in TSHIRT_CATEGORIES for g in buckets["upper"]
        ),
    }
    core_ids = set(payload.core_ids)
    cores = sorted([g for g in eligible if g["id"] in core_ids], key=lambda g: g["id"])
    states, truncated = 0, False

    def spend():
        nonlocal states, truncated
        if states >= 10_000 or time.monotonic() >= stop_at:
            truncated = True
            return False
        states += 1
        return True

    def template_groups(roles, template_buckets):
        if len(cores) > len(roles) or any(not template_buckets[role] for role in roles):
            return
        for assignment in _core_assignments(
            cores, roles, template_buckets, spend=spend
        ):
            if truncated:
                return
            constrained = {
                role: [assignment[role]]
                if role in assignment
                else [g for g in template_buckets[role] if g["id"] not in core_ids]
                for role in roles
            }
            if all(constrained.values()):
                pair_roles = tuple(
                    r
                    for r in roles
                    if r in {"base_layer", "upper", "outerwear"}
                    or r == "one_piece"
                    and "upper" not in roles
                )
                rest = tuple(r for r in roles if r not in pair_roles)
                yield _pair_group(pair_roles, rest, constrained)

    groups = []
    if len(cores) == len(core_ids):
        templates = [
            ("upper", "lower", "footwear"),
            ("one_piece", "upper", "footwear")
            if needs_outer
            else ("one_piece", "footwear"),
        ]
        if summer_enabled:
            templates.append(("base_layer", "upper", "lower", "footwear"))
        # Core assignment exploration shares the pair/completion state budget.
        for template in templates:
            roles = (*template, "outerwear") if needs_outer else template
            template_buckets = buckets
            if "base_layer" in template:
                template_buckets = {
                    **buckets,
                    "upper": [
                        g
                        for g in buckets["upper"]
                        if g.get("category") == "短袖衬衫"
                        and classification(g) in {"spring_summer", "all_year"}
                    ],
                }
            groups.append(template_groups(roles, template_buckets))
    pair_groups = []
    for group in _interleave(groups):
        if truncated:
            break
        pair_groups.append(group)

    def completions(pair, rest, constrained):
        for extra in _balanced_product(rest, constrained):
            if not spend():
                return
            items = pair + extra
            ids = [g["id"] for _, g in items]
            if len(ids) == len(set(ids)) and core_ids <= set(ids):
                yield _candidate(items)

    viable = []
    best_light = None
    best_light_key = None
    template_pairs, template_witnesses = Counter(), Counter()
    for pair, rest, constrained in _interleave(pair_groups):
        if not spend():
            break
        check = check_upper_budget([{"role": r, "garment": g} for r, g in pair], ctx)
        if not check.passed:
            reasons[check.reason] += 1
            continue
        template_pairs[check.template] += 1
        rotated = dict(constrained)
        for role in rest:
            offset = len(viable) % len(constrained[role])
            rotated[role] = constrained[role][offset:] + constrained[role][:offset]
        complete = completions(pair, rest, rotated)
        first = next(complete, None)
        if truncated:
            break
        if first is None:
            continue
        template_witnesses[check.template] += 1
        if outer_thickness(first) in {"thin", "medium"}:
            key = thermal_sort_key(check)
            best_light_key = key if best_light_key is None else min(best_light_key, key)
            best_light = (
                check.distance
                if best_light is None
                else min(best_light, check.distance)
            )
        viable.append((thermal_sort_key(check), first, complete))
    pair_search_complete = not truncated
    guard = FirstChoiceGuard(best_light, pair_search_complete, best_light_key)
    # Keep the best first-choice representative ahead of the expansion cap.
    viable.sort(key=lambda v: (not first_outfit_allowed(v[1], ctx, guard), v[0]))
    sort_keys = sorted({v[0] for v in viable})
    scheduled = itertools.chain.from_iterable(
        _interleave(
            [
                itertools.chain([first], rest)
                for d, first, rest in viable
                if d == sort_key
            ]
        )
        for sort_key in sort_keys
    )
    # The first admissible witness must survive even if 500 better-distance
    # candidates are forbidden as first choice (for example truncated proof).
    preferred = next(
        (v[1] for v in viable if first_outfit_allowed(v[1], ctx, guard)), None
    )
    if preferred is not None:
        selected.append(preferred)
        seen.add(preferred["signature"])
    for candidate in scheduled:
        if searched >= 500:
            truncated = True
            break
        searched += 1
        if candidate["signature"] not in seen:
            seen.add(candidate["signature"])
            selected.append(candidate)
        if len(selected) >= 500:
            truncated = True
            break
    for candidate in selected:
        candidate["_thermal"] = check_upper_budget(candidate["items"], ctx)
        decision = first_choice_decision(candidate, ctx, guard)
        candidate["_first_allowed"] = decision.allowed
        candidate["_layer_template"] = candidate["_thermal"].template
        if not decision.allowed:
            reasons[decision.reason] += 1
    if not pair_search_complete:
        reasons["search_incomplete"] += 1
    layering["thermal_budget"] = {
        **ctx.output(),
        "pair_states_checked": states,
        "pair_search_complete": pair_search_complete,
        "pair_search_truncated": not pair_search_complete,
        "reason_counts": dict(reasons),
        "first_choice_guard": guard.output(),
        "season_pair_rule_version": "layer-season-compatibility/1",
        "summer_layering_rule_version": "summer-shirt-layering/1",
        "summer_layering_enabled": summer_enabled,
        "template_legal_pair_counts": dict(template_pairs),
        "template_complete_witness_counts": dict(template_witnesses),
    }
    search = SearchDiagnostics(
        {
            "rule_version": "wardrobe-candidates/7",
            "layering_check": layering,
            "temperature_check": {
                "rule_version": "wardrobe-temperature/3",
                "pending_count": len(pending),
                "blocked_count": len(blocked),
                "pending_items": [pending[k] for k in sorted(pending)[:10]],
            },
            "searched_variants": searched,
            "accepted_candidates": len(selected),
            "bounded_search": True,
            "base_limit": 500,
            "variant_limit": 500,
            "search_truncated": truncated,
            "eligible_role_counts": {
                role: len(items) for role, items in buckets.items()
            },
            "candidate_role_coverage": {
                role: len(
                    {
                        i["garment"]["id"]
                        for c in selected
                        for i in c["items"]
                        if i["role"] == role
                    }
                )
                for role in buckets
            },
        }
    )
    search.guard = guard
    return selected, search


class SearchDiagnostics(dict):
    """The immutable first-choice proof is internal, never JSON/model input."""


def _pair_group(pair_roles, rest, constrained):
    for items in _balanced_product(pair_roles, constrained):
        yield items, rest, constrained


def choose(evaluated, history, payload, now, ctx=None, guard=None):
    recent14 = [
        e
        for e in history
        if e["occurred_at"] >= now - 14 * 86400
        and e["kind"] in {"generated", "shown", "adopted", "worn"}
    ]
    used = {e["payload"]["signature"] for e in recent14}
    fresh = [c for c in evaluated if c["signature"] not in used]
    pool = evaluated  # First-choice eligibility precedes the freshness class.

    def rank(candidate):
        ids = {i["garment"]["id"] for i in candidate["items"]}
        penalty = 0
        for event in recent14:
            if event["occurred_at"] < now - 7 * 86400:
                continue
            other = set(event["payload"]["garment_ids"])
            penalty += (
                len(ids & other)
                / max(len(ids), 1)
                * (3 if event["kind"] == "worn" else 1)
            )
        return (
            *(
                thermal_sort_key(check_upper_budget(candidate["items"], ctx))
                if ctx
                else (0, 0, Decimal("0"))
            ),
            candidate["signature"] in used and not payload.allow_rewear,
            penalty,
            -candidate["quality_score"],
            candidate["signature"],
        )

    ranked = sorted(pool, key=rank)
    result = []
    while ranked and len(result) < payload.count:
        # Among equal freshness classes prefer options differing from this batch.
        allowed = [
            c
            for c in ranked
            if result or ctx is None or first_outfit_allowed(c, ctx, guard)
        ]
        if not allowed:
            break
        choice = min(
            allowed,
            key=lambda c: (
                *rank(c)[:5],
                sum(
                    len(
                        {i["garment"]["id"] for i in c["items"]}
                        & {i["garment"]["id"] for i in previous["items"]}
                    )
                    for previous in result
                ),
                *rank(c)[5:],
            ),
        )
        thermal_repeat = (
            not result
            and ctx is not None
            and choice["signature"] in used
            and not payload.allow_rewear
            and any(not first_outfit_allowed(c, ctx, guard) for c in fresh)
        )
        ranked.remove(choice)
        result.append(
            {
                **{k: v for k, v in choice.items() if not k.startswith("_")},
                "outfit_id": str(uuid4()),
                "exact_repeat_14d": choice["signature"] in used,
                "repeat_reason": "thermal_first_choice_constraint"
                if thermal_repeat
                else "explicit_rewear"
                if payload.allow_rewear and choice["signature"] in used
                else "insufficient_nonrepeated_candidates"
                if choice["signature"] in used
                else None,
            }
        )
    return result, len(fresh)


def valid_search_observations(budget, truncated):
    """Validate bounded runtime observations without treating counts as proof.

    A transaction rebuild can spend a different number of states. Its immutable
    guard is checked separately; public counters must still be well formed.
    """
    if not isinstance(budget, dict) or not isinstance(
        budget.get("first_choice_guard"), dict
    ):
        return False
    states = budget.get("pair_states_checked")
    complete = budget.get("pair_search_complete")
    pair_truncated = budget.get("pair_search_truncated")
    if (
        type(states) is not int
        or not 0 <= states <= 10_000
        or type(complete) is not bool
        or type(pair_truncated) is not bool
        or type(truncated) is not bool
        or complete == pair_truncated
        or pair_truncated
        and not truncated
        or complete
        != budget.get("first_choice_guard", {}).get("alternative_search_complete")
    ):
        return False
    templates = {"single_upper_or_one_piece", "outerwear_inner", "summer_open_shirt"}
    for name in (
        "reason_counts",
        "template_legal_pair_counts",
        "template_complete_witness_counts",
    ):
        counts = budget.get(name)
        if not isinstance(counts, dict) or any(
            not isinstance(k, str) or type(v) is not int or not 0 <= v <= 10_000
            for k, v in counts.items()
        ):
            return False
        if name != "reason_counts" and (
            not counts.keys() <= templates or sum(counts.values()) > states
        ):
            return False
    pairs = budget["template_legal_pair_counts"]
    return all(
        value <= pairs.get(key, 0)
        for key, value in budget["template_complete_witness_counts"].items()
    )


class RecommendationService:
    def __init__(
        self,
        database,
        catalog,
        assets,
        audience,
        weather=None,
        evaluator=None,
        *,
        default_timezone="UTC",
    ):
        self.database, self.catalog, self.assets = database, catalog, assets
        self.auth = Authenticator(database.sessions, audience)
        self.default_timezone = ZoneInfo(default_timezone).key
        self.weather, self.evaluator = (
            weather or DisabledWeather(),
            evaluator or DisabledEvaluator(),
        )

    def with_default_timezone(self, payload):
        def location(value):
            if "timezone" in value.model_fields_set:
                return value
            return value.model_copy(update={"timezone": self.default_timezone})

        updates = {"location": location(payload.location)}
        if payload.user_weather is not None:
            updates["user_weather"] = payload.user_weather.model_copy(
                update={"location": location(payload.user_weather.location)}
            )
        return payload.model_copy(update=updates)

    def submit(self, principal, payload, key):
        principal.require("wardrobe.recommend")
        with self.database.write() as session:
            return self._submit_in_session(session, principal, payload, key)

    def _submit_in_session(self, session, principal, payload, key):
        """Enqueue and save the receipt in the caller's transaction; never commit."""
        principal.require("wardrobe.recommend")
        raw = payload.model_dump()
        if "timezone" not in payload.location.model_fields_set:
            raw["location"].pop("timezone")
        if (
            payload.user_weather is not None
            and "timezone" not in payload.user_weather.location.model_fields_set
        ):
            raw["user_weather"]["location"].pop("timezone")
        recheck_grant(session, principal, required_scope="wardrobe.recommend")

        def work():
            resolved = self.with_default_timezone(payload)
            selected = preference(session, principal.owner_id)
            if (
                resolved.expected_season_revision is not None
                and selected.revision != resolved.expected_season_revision
            ):
                raise BusinessError(
                    "SEASON_REVISION_CONFLICT", "Storage season changed", 409
                )
            frozen_season = context(selected)
            local = datetime.now(ZoneInfo(resolved.location.timezone))
            data = resolved.model_dump()
            assumptions = []
            if resolved.interval is None:
                offsets = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
                requested = resolved.target_date
                date = (
                    (local.date() + timedelta(days=offsets[requested])).isoformat()
                    if requested in offsets
                    else requested or local.date().isoformat()
                )
                if requested in offsets:
                    assumptions.append(
                        f"Resolved {requested} in {resolved.location.timezone} to {date}"
                    )
                zone = ZoneInfo(resolved.location.timezone)
                data["interval"] = {
                    "start": datetime.fromisoformat(date + "T09:00:00")
                    .replace(tzinfo=zone)
                    .isoformat(),
                    "end": datetime.fromisoformat(date + "T23:00:00")
                    .replace(tzinfo=zone)
                    .isoformat(),
                }
                window = "–".join(
                    datetime.fromisoformat(data["interval"][key]).strftime("%H:%M")
                    for key in ("start", "end")
                )
                assumptions.extend(
                    [
                        f"Default local daytime: {window}",
                        f"默认当地外出时段：{window}，含23:00整点小时（{resolved.location.timezone}）。",
                    ]
                )
            data["target_date"] = None
            data["assumptions"] = assumptions
            now = int(time.time())
            if (
                resolved.user_weather is None
                and instant(data["interval"]["end"]).timestamp() <= now
            ):
                raise BusinessError(
                    "WEATHER_OUT_OF_RANGE",
                    "Forward forecast cannot cover an entirely past interval",
                    422,
                )
            row = Recommendation(
                id=str(uuid4()),
                owner_id=principal.owner_id,
                client_id=principal.client_id,
                grant_id=principal.grant_id,
                payload=data,
                state="queued",
                wardrobe_context=frozen_season,
                created_at=now,
                deadline=now + 120,
                attempts=0,
            )
            session.add(row)
            return success(
                {
                    "recommendation_id": row.id,
                    "state": row.state,
                    "deadline": row.deadline,
                    "deadline_at": row.deadline,
                    "job_id": row.id,
                    "phase": "queued",
                    "poll_after_ms": 1000,
                },
                status="accepted",
            )

        return idempotent(session, principal, "recommend", key, raw, work)

    def seal_cancelled(self, session, identifier):
        """Seal publication while the caller holds the database write lock."""
        row = session.get(Recommendation, identifier)
        if row is not None and row.state == "cancelled":
            return
        changed = session.execute(
            update(Recommendation)
            .where(
                Recommendation.id == identifier,
                Recommendation.state.in_(["queued", "running"]),
            )
            .values(state="cancelled", lease_id=None, lease_until=0)
            .execution_options(synchronize_session=False)
        )
        if row is not None:
            session.refresh(row)
        if changed.rowcount == 0:
            # Re-read even on zero rows: an already sealed job is idempotent.
            if row is not None and row.state == "cancelled":
                return
            raise BusinessError(
                "OPERATION_NOT_CANCELLABLE", "Recommendation is already terminal", 409
            )

    @image_preview_response
    def get(self, principal, identifier):
        principal.require("wardrobe.read")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            row = session.get(Recommendation, identifier)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Recommendation not found", 404)
            if row.state in {"queued", "running"} and row.deadline <= int(time.time()):
                row.state, row.error = (
                    "failed",
                    {
                        "code": "TASK_DEADLINE_EXCEEDED",
                        "message": "Total recommendation deadline exceeded",
                    },
                )
            return success(
                {
                    "recommendation_id": row.id,
                    "state": row.state,
                    "deadline": row.deadline,
                    "deadline_at": row.deadline,
                    "job_id": row.id,
                    "phase": row.state,
                    "poll_after_ms": 1000
                    if row.state in {"queued", "running"}
                    else None,
                    "result": row.result,
                    "wardrobe_context": row.wardrobe_context,
                    "error": row.error,
                },
                status=row.state,
            )

    def claim(self):
        now = int(time.time())
        with self.database.write() as session:
            rows = list(
                session.scalars(
                    select(Recommendation)
                    .where(Recommendation.state.in_(["queued", "running"]))
                    .order_by(Recommendation.created_at)
                    .limit(100)
                )
            )
            for row in rows:
                if row.deadline <= now:
                    row.state, row.error = (
                        "failed",
                        {
                            "code": "TASK_DEADLINE_EXCEEDED",
                            "message": "Total recommendation deadline exceeded",
                        },
                    )
                elif row.state == "running":
                    if row.lease_until <= now:
                        # A provider might already have charged; never resend automatically.
                        row.state, row.error = (
                            "failed",
                            {
                                "code": "UPSTREAM_RESULT_UNKNOWN",
                                "message": "Worker lease expired; prior provider result is unknown",
                            },
                        )
                else:
                    row.state, row.lease_id, row.lease_until = (
                        "running",
                        str(uuid4()),
                        row.deadline,
                    )
                    row.attempts += 1
                    return row.id, row.lease_id
        return None

    def _snapshot(self, identifier, lease):
        with self.database.sessions.begin() as session:
            row = session.get(Recommendation, identifier)
            if (
                row.state != "running"
                or row.lease_id != lease
                or row.deadline <= int(time.time())
            ):
                raise BusinessError(
                    "TASK_DEADLINE_EXCEEDED", "Job is no longer publishable", 409
                )
            grant = session.get(ClientGrant, row.grant_id)
            if grant is None:
                raise BusinessError("UNAUTHORIZED", "Job client is unavailable", 401)
            principal = Principal(
                row.owner_id,
                row.client_id,
                frozenset(grant.scopes),
                grant.channel,
                grant.audience,
                grant.token_hash,
            )
            recheck_grant(session, principal, required_scope="wardrobe.recommend")
            principal.require("wardrobe.recommend")
            selected = preference(session, row.owner_id)
            if row.wardrobe_context != context(selected):
                raise BusinessError(
                    "SEASON_CHANGED", "Season or rules changed since submission", 409
                )
            state = session.get(ServiceState, row.owner_id)
            garments = self._garments(session, principal, selected, row.payload)
            events = list(
                session.scalars(
                    select(HistoryEvent).where(
                        HistoryEvent.owner_id == row.owner_id,
                        HistoryEvent.active.is_(True),
                        HistoryEvent.occurred_at >= int(time.time()) - 14 * 86400,
                    )
                )
            )
            history = [
                {"kind": e.kind, "occurred_at": e.occurred_at, "payload": e.payload}
                for e in events
            ]
            return (
                principal,
                dict(row.payload),
                row.deadline,
                state.catalog_revision,
                state.history_revision,
                garments,
                history,
            )

    def _garments(self, session, principal, selected, request):
        records = list(
            session.scalars(
                select(Garment)
                .where(
                    Garment.owner_id == principal.owner_id, Garment.state == "active"
                )
                .order_by(Garment.id)
                .limit(201)
            )
        )
        if len(records) > 200:
            raise BusinessError(
                "CATALOG_LIMIT",
                "Current bounded search supports at most 200 active garments",
                422,
            )
        stored = {
            g.id
            for g in records
            if classification(g) not in {selected.active_season, "all_year"}
        }
        if stored.intersection(request.get("core_ids", [])):
            raise BusinessError(
                "CORE_ITEM_STORED", "Core garment is stored or unassigned", 422
            )
        records = [g for g in records if g.id not in stored]
        garments = [CatalogService._view(g, session) for g in records]
        for garment in garments:
            if not garment["image_references"] or any(
                not self.assets.originals.joinpath(
                    session.get(Asset, ref["asset_id"]).sha256
                ).is_file()
                for ref in garment["image_references"]
            ):
                garment["recommendation_eligible"] = False
        return garments

    async def process(self, identifier, lease):
        try:
            for _attempt in range(3):
                (
                    principal,
                    data,
                    deadline,
                    catalog_revision,
                    history_revision,
                    garments,
                    history,
                ) = await asyncio.to_thread(self._snapshot, identifier, lease)
                assumptions = data.pop("assumptions")
                payload = RecommendationInput.model_validate(data)
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError()
                async with asyncio.timeout(remaining):
                    now = int(time.time())
                    if payload.user_weather is None:
                        async with asyncio.timeout(min(10, remaining)):
                            weather = await self.weather.fetch(
                                payload.location, payload.interval, now
                            )
                    else:
                        weather = payload.user_weather
                    weather = weather_context(weather)
                    weather.validate_for(
                        payload.location, payload.interval, int(time.time())
                    )
                    candidates, search = candidates_for(
                        garments, payload, weather, deadline=deadline
                    )
                    ctx = thermal_context(
                        weather,
                        outerwear_required=search["layering_check"][
                            "outerwear_required"
                        ],
                        full_day=full_day_allowed(payload.occasion),
                    )
                    guard = search.guard
                    from .outfit_provider import shortlist

                    if any(
                        "_thermal" not in c
                        or type(c.get("_first_allowed")) is not bool
                        or c.get("_layer_template") != c["_thermal"].template
                        for c in candidates
                    ):
                        raise BusinessError(
                            "OUTFIT_CONSTRAINT_VIOLATION",
                            "Missing server thermal metadata",
                            502,
                        )
                    candidates = shortlist(candidates)
                    if candidates and any(c["_first_allowed"] for c in candidates):
                        async with asyncio.timeout(
                            min(60, max(0.01, deadline - time.time()))
                        ):
                            raw_evaluations = await self.evaluator.evaluate(
                                copy.deepcopy(candidates),
                                evaluator_weather(weather),
                                payload.occasion,
                            )
                        evaluations = [
                            Evaluation.model_validate(e) for e in raw_evaluations
                        ]
                        known = {c["candidate_id"]: c for c in candidates}
                        search["evaluated_candidates"] = len(evaluations)
                        if len({e.candidate_id for e in evaluations}) != len(
                            evaluations
                        ) or any(e.candidate_id not in known for e in evaluations):
                            raise BusinessError(
                                "INVALID_MODEL_OUTPUT",
                                "Evaluator returned an unknown or duplicate candidate",
                                502,
                            )
                        accepted = []
                        for evaluation in evaluations:
                            if (
                                not evaluation.suitable
                                or min(
                                    evaluation.color,
                                    evaluation.material,
                                    evaluation.style,
                                    evaluation.silhouette,
                                )
                                < 0.5
                            ):
                                continue
                            candidate = known[evaluation.candidate_id]
                            for garment_id, fields in evaluation.evidence.items():
                                item = next(
                                    (
                                        i["garment"]
                                        for i in candidate["items"]
                                        if i["garment"]["id"] == garment_id
                                    ),
                                    None,
                                )
                                if item is None or any(
                                    field not in item["annotation_fields"]
                                    or _value(item, field) is None
                                    for field in fields
                                ):
                                    raise BusinessError(
                                        "INVALID_MODEL_OUTPUT",
                                        "Evaluator cited unavailable field evidence",
                                        502,
                                    )
                            accepted.append(
                                {
                                    **candidate,
                                    "quality_score": (
                                        evaluation.color
                                        + evaluation.material
                                        + evaluation.style
                                        + evaluation.silhouette
                                    )
                                    / 4,
                                    "assessment": evaluation.model_dump(),
                                }
                            )
                    else:
                        accepted = []
                    outfits, fresh_count = choose(
                        accepted, history, payload, int(time.time()), ctx, guard
                    )
                    validate_outfits(outfits, garments, payload, weather, guard)
                    search["layering_check"]["thermal_budget"]["selected_outfits"] = (
                        selected_output(outfits, ctx, guard)
                    )
                    assumptions.extend(assumptions_for(outfits, ctx))
                    warnings = (
                        ["Unknown availability must be checked before wearing"]
                        if any(
                            i["garment"]["availability"] == "unknown"
                            for o in outfits
                            for i in o["items"]
                        )
                        else []
                    )
                    if weather.feels_like_missing:
                        assumptions.append(missing_feels_note(weather))
                        warnings.append(missing_feels_note(weather))
                    thermal = search["temperature_check"]
                    if thermal["pending_count"]:
                        warnings.append(
                            "部分服装的厚薄尚未由用户确认，已排除；未知信息不能证明温度适配。"
                        )
                    if (
                        weather.source_kind == "user_input"
                        and instant(payload.interval.end).timestamp() <= time.time()
                    ):
                        warnings.append(
                            "Target interval has already ended; user-provided weather is retrospective context, not a validated forecast"
                        )
                    for outfit in outfits:
                        outfit["explanation"] = outfit_explanation(
                            outfit, weather, ctx, guard
                        )
                        outfit["unknown_fields"] = {
                            i["garment"]["id"]: [
                                k
                                for k in (
                                    "primary_color",
                                    "material",
                                    "warmth",
                                    "waterproof",
                                    "style",
                                    "silhouette",
                                )
                                if _value(i["garment"], k) is None
                            ]
                            for i in outfit["items"]
                        }
                    missing = []
                    budget_diagnostics = search["layering_check"]["thermal_budget"]
                    if not outfits:
                        missing.extend(
                            (
                                REASONS[r].replace("当前未找到", "本次有界搜索未找到")
                                if budget_diagnostics["pair_search_truncated"]
                                else REASONS[r]
                            )
                            for r in budget_diagnostics["reason_counts"]
                            if r in REASONS
                        )
                        if candidates:
                            missing.append(REASONS["no_admissible_first"])
                    if not outfits:
                        layering = search["layering_check"]
                        if (
                            layering["outerwear_required"]
                            and not search["eligible_role_counts"]["outerwear"]
                        ):
                            missing.append(
                                "缺少外套（outerwear）：天气或结构化要求需要外套，但用户已排除当前全部合格外套。"
                                if layering["all_eligible_outerwear_excluded"]
                                else "缺少合格外套（outerwear）：当前没有同时满足库存、温度及其他约束的外套。"
                            )
                        if (
                            layering["outerwear_required"]
                            and not layering["eligible_layerable_inner_count"]
                        ):
                            missing.append(
                                "外套必须搭配独立可叠内搭；当前没有同时满足库存、温度及其他约束的可用内搭。"
                            )
                        if (
                            payload.core_ids
                            and layering["outerwear_required"]
                            and not candidates
                        ):
                            missing.append(
                                "无法同时满足指定核心单品（core）与外套及独立可叠内搭约束；未丢弃核心单品。"
                            )
                        if thermal["pending_count"]:
                            missing.append(
                                "请先逐件确认服装厚薄；当前没有信息充分且符合温度约束的完整搭配。"
                            )
                        if not missing:
                            missing.append(
                                "当前库存、温度及核心／排除约束下没有通过质量评估的完整搭配。"
                            )
                    if outfits and len(outfits) < payload.count:
                        missing.append(
                            f"仅有 {len(outfits)} 套合规搭配，少于请求的 {payload.count} 套。"
                        )
                    result = {
                        "location": payload.location.model_dump(),
                        "interval": payload.interval.model_dump(),
                        "occasion": payload.occasion,
                        "assumptions": assumptions,
                        "weather": weather_output(weather),
                        "weather_acceptance_eligible": weather.source_kind
                        == "forecast",
                        "outfits": outfits,
                        "completeness": "complete"
                        if len(outfits) == payload.count
                        else "partial",
                        "warnings": warnings,
                        "missing": missing,
                        "search": search,
                        "fresh_qualified_count": fresh_count,
                        "catalog_revision": catalog_revision,
                        "history_revision": history_revision,
                    }
                    if await asyncio.to_thread(
                        self._commit,
                        principal,
                        identifier,
                        lease,
                        catalog_revision,
                        history_revision,
                        weather,
                        result,
                    ):
                        return
            raise BusinessError(
                "REVISION_CONFLICT",
                "Inventory or history changed repeatedly; request a fresh job",
                409,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await asyncio.to_thread(
                self._fail,
                identifier,
                lease,
                "TASK_DEADLINE_EXCEEDED",
                "Provider or total job budget expired; any outstanding provider outcome is unknown",
            )
        except BusinessError as error:
            await asyncio.to_thread(
                self._fail, identifier, lease, error.code, error.message
            )
        except Exception:  # noqa: BLE001 -- fail closed at the injected provider boundary
            await asyncio.to_thread(
                self._fail,
                identifier,
                lease,
                "INVALID_PROVIDER_RESULT",
                "Provider result or internal processing failed validation",
            )

    def _commit(
        self,
        principal,
        identifier,
        lease,
        catalog_revision,
        history_revision,
        weather,
        result,
    ):
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.recommend")
            principal.require("wardrobe.recommend")
            row = session.get(Recommendation, identifier)
            now = int(time.time())
            if (
                row.state != "running"
                or row.lease_id != lease
                or row.lease_until <= now
                or row.deadline <= now
            ):
                raise BusinessError(
                    "TASK_DEADLINE_EXCEEDED", "Late job output cannot be published", 409
                )
            frozen = {k: v for k, v in row.payload.items() if k != "assumptions"}
            payload = RecommendationInput.model_validate(frozen)
            weather = weather_context(weather)
            weather.validate_for(payload.location, payload.interval, now)
            selected = preference(session, principal.owner_id)
            if row.wardrobe_context != context(selected):
                raise BusinessError(
                    "SEASON_CHANGED", "Season or rules changed before publication", 409
                )
            result["wardrobe_context"] = row.wardrobe_context
            state = session.get(ServiceState, principal.owner_id)
            if (
                state.catalog_revision != catalog_revision
                or state.history_revision != history_revision
            ):
                return False
            garments = self._garments(session, principal, selected, row.payload)
            requirement = outerwear_requirement(garments, payload, weather)
            layering = result["search"]["layering_check"]
            if (
                result["location"] != payload.location.model_dump()
                or result["interval"] != payload.interval.model_dump()
                or result["weather"] != weather_output(weather)
                or any(layering.get(key) != value for key, value in requirement.items())
            ):
                raise BusinessError(
                    "OUTFIT_CONSTRAINT_VIOLATION",
                    "Weather or layering diagnostics do not match trusted context",
                    502,
                )
            # Rebuild the first-choice domain from this transaction's authoritative facts.
            _, domain = candidates_for(
                garments, payload, weather, deadline=row.deadline
            )
            guard = domain.guard
            ctx = thermal_context(
                weather,
                outerwear_required=requirement["outerwear_required"],
                full_day=full_day_allowed(payload.occasion),
            )
            validate_outfits(result["outfits"], garments, payload, weather, guard)
            budget = layering.get("thermal_budget", {})
            expected = {
                **ctx.output(),
                **{
                    key: domain["layering_check"]["thermal_budget"][key]
                    for key in (
                        "season_pair_rule_version",
                        "summer_layering_rule_version",
                        "summer_layering_enabled",
                    )
                },
                "first_choice_guard": domain["layering_check"]["thermal_budget"][
                    "first_choice_guard"
                ],
                "selected_outfits": selected_output(result["outfits"], ctx, guard),
            }
            if (
                result["search"].get("rule_version") != domain["rule_version"]
                or result["search"].get("temperature_check", {}).get("rule_version")
                != domain["temperature_check"]["rule_version"]
                or layering.get("rule_version") != "outerwear-layerable-inner/1"
                or any(
                    layering.get(k) != domain["layering_check"][k]
                    for k in (
                        "eligible_layerable_inner_count",
                        "eligible_tshirt_count",
                        "all_eligible_outerwear_excluded",
                    )
                )
                or not valid_search_observations(
                    budget, result["search"].get("search_truncated")
                )
                or any(budget.get(k) != v for k, v in expected.items())
                or set(budget)
                != set(domain["layering_check"]["thermal_budget"])
                | {"selected_outfits"}
                or not set(assumptions_for(result["outfits"], ctx))
                <= set(result["assumptions"])
                or any(
                    o.get("explanation") != outfit_explanation(o, weather, ctx, guard)
                    for o in result["outfits"]
                )
            ):
                raise BusinessError(
                    "OUTFIT_CONSTRAINT_VIOLATION",
                    "Thermal diagnostics do not match server facts",
                    502,
                )
            if row.deadline <= time.time():
                raise BusinessError(
                    "TASK_DEADLINE_EXCEEDED",
                    "Thermal validation exceeded the job deadline",
                    409,
                )
            if len(result["outfits"]) > payload.count or (
                result["completeness"] == "complete"
            ) != (len(result["outfits"]) == payload.count):
                raise BusinessError(
                    "OUTFIT_CONSTRAINT_VIOLATION",
                    "Outfit count diagnostics do not match the batch",
                    502,
                )
            for outfit in result["outfits"]:
                session.add(
                    HistoryEvent(
                        id=str(uuid4()),
                        owner_id=principal.owner_id,
                        recommendation_id=row.id,
                        outfit_id=outfit["outfit_id"],
                        kind="generated",
                        occurred_at=now,
                        payload={
                            "signature": outfit["signature"],
                            "garment_ids": sorted(
                                {i["garment"]["id"] for i in outfit["items"]}
                            ),
                            "target_interval": result["interval"],
                            "source": "wardrobe_service",
                            "wardrobe_context": row.wardrobe_context,
                        },
                        revision=1,
                        active=True,
                    )
                )
            if result["outfits"]:
                state.history_revision += 1
            row.result, row.state = (
                result,
                "succeeded" if result["completeness"] == "complete" else "partial",
            )
            return True

    def _fail(self, identifier, lease, code, message):
        with self.database.write() as session:
            row = session.get(Recommendation, identifier)
            if row and row.state == "running" and row.lease_id == lease:
                row.state, row.error = "failed", {"code": code, "message": message}

    def feedback(self, principal, payload, key):
        principal.require("wardrobe.feedback")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.feedback")

            def work():
                row = session.get(Recommendation, payload.recommendation_id)
                if (
                    row is None
                    or row.owner_id != principal.owner_id
                    or row.state not in {"succeeded", "partial"}
                ):
                    raise BusinessError(
                        "NOT_FOUND", "Completed recommendation not found", 404
                    )
                outfit = next(
                    (
                        o
                        for o in row.result["outfits"]
                        if o["outfit_id"] == payload.outfit_id
                    ),
                    None,
                )
                if outfit is None:
                    raise BusinessError("NOT_FOUND", "Outfit not found", 404)
                now = int(time.time())
                occurred = (
                    payload.occurred_at if payload.occurred_at is not None else now
                )
                if occurred > now + 30 or occurred < row.created_at:
                    raise BusinessError(
                        "INVALID_ARGUMENT",
                        "Feedback occurrence must follow recommendation generation and cannot be in the future",
                        422,
                    )
                identifier = str(uuid4())
                session.add(
                    HistoryEvent(
                        id=identifier,
                        owner_id=principal.owner_id,
                        recommendation_id=row.id,
                        outfit_id=outfit["outfit_id"],
                        kind=payload.kind,
                        occurred_at=occurred,
                        payload={
                            "signature": outfit["signature"],
                            "garment_ids": sorted(
                                {i["garment"]["id"] for i in outfit["items"]}
                            ),
                            "source": "caller_reported",
                            "wardrobe_context": row.wardrobe_context,
                            "client_id": principal.client_id,
                            "note": payload.note,
                        },
                        revision=1,
                        active=True,
                    )
                )
                session.get(ServiceState, principal.owner_id).history_revision += 1
                return success(
                    {
                        "event_id": identifier,
                        "revision": 1,
                        "kind": payload.kind,
                        "recommendation_id": row.id,
                        "outfit_id": payload.outfit_id,
                        "occurred_at": occurred,
                        "note": payload.note,
                    }
                )

            return idempotent(
                session, principal, "feedback", key, payload.model_dump(), work
            )

    @image_preview_response
    def history(self, principal, query):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            state = session.get(ServiceState, principal.owner_id)
            binding = {
                "v": 1,
                "owner": principal.owner_id,
                "limit": query.limit,
                "type": "history",
                "revision": state.history_revision,
            }
            statement = (
                select(HistoryEvent)
                .where(
                    HistoryEvent.owner_id == principal.owner_id,
                    HistoryEvent.active.is_(True),
                )
                .order_by(HistoryEvent.id)
            )
            if query.cursor:
                cursor = self.catalog._decode(query.cursor)
                if any(cursor.get(k) != v for k, v in binding.items()) or set(
                    cursor
                ) != {*binding, "last"}:
                    raise BusinessError(
                        "STALE_CURSOR",
                        "History changed or cursor does not match this query",
                        409,
                    )
                statement = statement.where(HistoryEvent.id > cursor["last"])
            rows = list(session.scalars(statement.limit(query.limit + 1)))
            page = rows[: query.limit]
            return success(
                {
                    "items": [
                        {
                            "id": e.id,
                            "recommendation_id": e.recommendation_id,
                            "outfit_id": e.outfit_id,
                            "kind": e.kind,
                            "occurred_at": e.occurred_at,
                            "revision": e.revision,
                            "source": e.payload.get("source"),
                        }
                        for e in page
                    ],
                    "next_cursor": self.catalog._encode(
                        {**binding, "last": page[-1].id}
                    )
                    if len(rows) > query.limit
                    else None,
                    "has_more": len(rows) > query.limit,
                    "history_revision": state.history_revision,
                }
            )

    async def run(self):
        while True:
            try:
                claimed = await asyncio.to_thread(self.claim)
                if claimed:
                    await self.process(*claimed)
                else:
                    await asyncio.sleep(0.2)
            except SQLAlchemyError as error:
                logging.getLogger(__name__).warning(
                    "Worker database unavailable (%s)", type(error).__name__
                )
                await asyncio.sleep(1)
