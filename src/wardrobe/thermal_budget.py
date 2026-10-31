"""Pure, decimal thermal estimates. No catalog writes or provider calls."""

# ruff: noqa: FURB157 -- keep decimal constants in explicit decimal-string form.

from dataclasses import dataclass
from decimal import Decimal

from .seasons import classification, compatible

TSHIRT_CATEGORIES = frozenset({"T恤", "短袖T恤", "长袖T恤"})
LAYERABLE_INNER_CATEGORIES = TSHIRT_CATEGORIES | frozenset(
    {"长袖衬衫", "shirt", "长袖上衣", "长款纽扣上衣", "针织上衣"}
)
LOWER_TOLERANCE = Decimal("2.0")
UPPER_TOLERANCE = Decimal("2.0")
MEDIUM_PAIR_MAX = Decimal("7.0")
THICKNESS_RANK = {
    name: rank for rank, name in enumerate(("thin", "medium", "thick", "very_thick"))
}
UNKNOWN_STACK_SENTINEL = 4
OUTERWEAR_CATEGORIES = frozenset({"外套", "翻领外套", "连帽分格外套", "连帽外套"})
WEIGHTS = dict(
    zip(
        ("thin", "medium", "thick", "very_thick"),
        map(Decimal, ("1.5", "3.5", "6", "9")),
        strict=True,
    )
)
REASONS = {
    "invalid_layers": "上身叠穿结构不合法；仅支持规定的两层模板。",
    "season_incompatible": "内外两层季节不兼容。",
    "season_unassigned": "有单品季节未分配，未参与叠穿。",
    "missing_layerable_inner": "外套必须搭配独立可叠内搭；当前没有合格可叠内搭。",
    "hottest_heavy_first_blocked": "最高温不低于22℃，厚／特别厚外套不能作为首选；当前未找到合规的轻档首套，因此返回部分结果。",
    "light_complete_available": "存在合法的薄／中外套完整组合，厚外套仅作后续备选。",
    "mild_light_capacity_sufficient": "中等内搭＋中等外套的等效7.0℃可覆盖下沿，不能仅因库存缺货启用厚外套首选。",
    "over_budget": "上身组合过暖，超过保暖预算上限。",
    "under_budget": "上身组合欠暖，未达到保暖预算下限。",
    "outerwear_too_thick": "当前保暖缺口较小，特别厚外套不适用。",
    "pants_too_light": "低于10℃时长裤需已核实 thick／very_thick 厚度。",
    "pants_too_heavy": "不低于16℃时长裤仅允许 medium／thin 厚度。",
    "very_thick_pants_requires_full_day": "特别厚长裤仅显式全天出门允许。",
    "thickness_pending": "厚薄尚未由用户核实。",
    "no_admissible_first": "本次未找到可用首套，不能以偏厚组合递补。",
    "search_incomplete": "本次有界搜索未穷尽，无法证明没有更合适的组合。",
}


def decimal(value):
    if isinstance(value, bool):
        raise TypeError("Boolean is not a temperature")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Finite decimal required")
    return result


def median(values):
    points = sorted(decimal(v) for v in values)
    if not points:
        raise ValueError("Median needs observations")
    middle = len(points) // 2
    return (
        points[middle]
        if len(points) % 2
        else (points[middle - 1] + points[middle]) / Decimal("2")
    )


def confirmed_value(garment, field):
    entry = garment["annotation_fields"].get(field, {})
    return (
        entry.get("value")
        if entry.get("verification_state") == "user_verified"
        else None
    )


def full_day_allowed(occasion):
    return occasion.strip().casefold() in {
        "full_day",
        "all_day",
        "full_day_out",
        "全天出门",
        "全天外出",
    }


@dataclass(frozen=True)
class ThermalContext:
    reference: Decimal
    basis: str
    method: str
    sample_count: int
    gap: Decimal
    lower: Decimal
    upper: Decimal
    coldest: Decimal
    hottest: Decimal
    outerwear_required: bool
    full_day: bool

    @property
    def very_cold(self):
        return self.reference < Decimal("10")

    @property
    def medium_pair_insufficient(self):
        return MEDIUM_PAIR_MAX < self.lower

    @property
    def hottest_blocks_heavy_first(self):
        return self.hottest >= Decimal("22")

    def output(self):
        return {
            "rule_version": "thermal-budget/2",
            "lower_tolerance_c": float(LOWER_TOLERANCE),
            "upper_tolerance_c": float(UPPER_TOLERANCE),
            "very_cold": self.very_cold,
            "very_cold_threshold_c": 10,
            "medium_pair_max_c": float(MEDIUM_PAIR_MAX),
            "medium_pair_insufficient": self.medium_pair_insufficient,
            "hottest_first_choice_guard": {
                "threshold_c": 22,
                "active": self.hottest_blocks_heavy_first,
                "blocked_outerwear_thicknesses": ["thick", "very_thick"],
            },
            "reference_temperature_c": float(self.reference),
            "reference_basis": self.basis,
            "reference_method": self.method,
            "sample_count": self.sample_count,
            "gap_c": float(self.gap),
            "lower_bound_c": float(self.lower),
            "upper_bound_c": float(self.upper),
            "coldest_c": float(self.coldest),
            "hottest_c": float(self.hottest),
            "full_day_allowed": self.full_day,
        }


def thermal_context(weather, *, outerwear_required=False, full_day=False):
    tmin = decimal(getattr(weather, "temperature_min", weather.feels_like_min))
    tmax = decimal(getattr(weather, "temperature_max", weather.feels_like_max))
    fmin, fmax = weather.feels_like_min, weather.feels_like_max
    feels = fmin is not None and fmax is not None
    low, high = (decimal(fmin), decimal(fmax)) if feels else (tmin, tmax)
    prefix = "feels_like" if feels else "temperature"
    middle = getattr(weather, prefix + "_median", None)
    reference = (low + high) / Decimal("2") if middle is None else decimal(middle)
    if low > high or not low <= reference <= high:
        raise ValueError("Inconsistent temperature statistics")
    gap = min(Decimal("12"), max(Decimal("0"), Decimal("26") - reference))
    return ThermalContext(
        reference,
        prefix,
        "range_midpoint" if middle is None else "hourly_median",
        0 if middle is None else getattr(weather, prefix + "_sample_count", 0),
        gap,
        gap - LOWER_TOLERANCE,
        gap + UPPER_TOLERANCE,
        min(tmin, low),
        max(tmax, high),
        outerwear_required,
        full_day,
    )


def item_reason(garment, role, ctx):
    thickness = confirmed_value(garment, "thickness")
    if role == "outerwear" and ctx.gap < Decimal("7") and thickness == "very_thick":
        return "outerwear_too_thick"
    if role == "lower" and garment.get("category") == "长裤":
        if thickness not in WEIGHTS:
            return "thickness_pending"
        if ctx.coldest < Decimal("10") and thickness not in {"thick", "very_thick"}:
            return "pants_too_light"
        if ctx.coldest >= Decimal("16") and thickness not in {"thin", "medium"}:
            return "pants_too_heavy"
        if thickness == "very_thick" and not ctx.full_day:
            return "very_thick_pants_requires_full_day"
    return None


@dataclass(frozen=True)
class LayerWeight:
    role: str
    garment_id: str
    thickness: str | None
    value: Decimal | None
    sources: tuple[str, ...]
    rank_thickness: str | None
    season: str

    def output(self):
        return {
            "role": self.role,
            "garment_id": self.garment_id,
            "thickness": self.thickness,
            "warmth_c": None if self.value is None else float(self.value),
            "sources": list(self.sources),
            "effective_thickness": self.rank_thickness,
            "classification": self.season,
        }


def layer_weight(garment, role, ctx, template="outerwear_inner"):
    thickness = confirmed_value(garment, "thickness")
    value = WEIGHTS.get(thickness)
    sources = ("verified_thickness",) if value is not None else ("unknown_not_counted",)
    rank_thickness = thickness if value is not None else None
    if (
        value is None
        and template == "outerwear_inner"
        and role == "upper"
        and ctx.outerwear_required
        and is_layerable_inner(garment, role)
    ):
        value, sources = WEIGHTS["thin"], ("layerable_inner_budget_assumption",)
        rank_thickness = "thin"
    if (
        value is None
        and template == "summer_open_shirt"
        and role in {"base_layer", "upper"}
    ):
        value, sources = WEIGHTS["thin"], ("summer_layer_budget_assumption",)
        rank_thickness = "thin"
    if ctx.coldest < Decimal("10") and confirmed_value(garment, "warmth") == "high":
        value = max(value or Decimal("0"), Decimal("6"))
        sources += ("verified_warmth_high_cold",)
    return LayerWeight(
        role,
        garment["id"],
        thickness,
        value,
        sources,
        rank_thickness,
        classification(garment),
    )


def budget_reason(weight, ctx):
    weight = decimal(weight)
    if weight > ctx.upper:
        return "over_budget"
    if weight < ctx.lower:
        return "under_budget"
    return None


def is_layerable_inner(garment, role="upper"):
    category = garment.get("category")
    return (
        role == "upper"
        and "upper" in garment.get("roles", [])
        and category in LAYERABLE_INNER_CATEGORIES
        and (
            category in TSHIRT_CATEGORIES
            or classification(garment) in {"autumn_winter", "all_year"}
        )
    )


def outerwear_has_layerable_inner(items):
    outer_ids = {
        i["garment"]["id"]
        for i in items
        if i["role"] == "outerwear"
        or i["garment"].get("category") in OUTERWEAR_CATEGORIES
    }
    return not outer_ids or any(
        is_layerable_inner(i["garment"], i["role"])
        and i["garment"]["id"] not in outer_ids
        for i in items
    )


def summer_layering_enabled(ctx):
    return not ctx.outerwear_required and ctx.reference >= Decimal("22")


def role_assignment_allowed(garment, role, template):
    if role == "base_layer":
        return (
            template == "summer_open_shirt"
            and garment.get("category") in TSHIRT_CATEGORIES
            and bool({"upper", "base_layer"} & set(garment["roles"]))
        )
    return role in garment["roles"]


def infer_layers(items, ctx):
    roles = [i["role"] for i in items]
    ids = [i["garment"]["id"] for i in items]
    by_role = {i["role"]: i["garment"] for i in items}
    if len(set(roles)) != len(roles) or len(set(ids)) != len(ids):
        return "single_upper_or_one_piece", [], "invalid_layers"
    real_outer = any(
        i["role"] == "outerwear" or i["garment"].get("category") in OUTERWEAR_CATEGORIES
        for i in items
    )
    if "base_layer" in roles:
        template = "summer_open_shirt"
        if (
            not summer_layering_enabled(ctx)
            or real_outer
            or "one_piece" in roles
            or "upper" not in roles
        ):
            return template, [], "invalid_layers"
        inner, outer = by_role["base_layer"], by_role["upper"]
        layers = [("base_layer", inner), ("upper", outer)]
        if (
            not role_assignment_allowed(inner, "base_layer", template)
            or outer.get("category") != "短袖衬衫"
            or "upper" not in outer["roles"]
        ):
            return template, layers, "invalid_layers"
        seasons = {classification(inner), classification(outer)}
        if "unassigned" in seasons:
            return template, layers, "season_unassigned"
        if not seasons <= {"spring_summer", "all_year"}:
            return template, layers, "season_incompatible"
    elif real_outer:
        template = "outerwear_inner"
        if (
            "outerwear" not in roles
            or "upper" not in roles
            or not outerwear_has_layerable_inner(items)
        ):
            return template, [], "missing_layerable_inner"
        layers = [("upper", by_role["upper"]), ("outerwear", by_role["outerwear"])]
        if by_role["upper"].get("category") in OUTERWEAR_CATEGORIES:
            return template, layers, "invalid_layers"
    else:
        template = "single_upper_or_one_piece"
        if "upper" in roles and "one_piece" in roles:
            return template, [], "invalid_layers"
        inner = "upper" if "upper" in roles else "one_piece"
        if inner not in roles:
            return template, [], "invalid_layers"
        return template, [(inner, by_role[inner])], None
    a, b = (g for _, g in layers)
    if not compatible(a, b):
        reason = (
            "season_unassigned"
            if "unassigned" in {classification(a), classification(b)}
            else "season_incompatible"
        )
        return template, layers, reason
    return template, layers, None


@dataclass(frozen=True)
class BudgetCheck:
    weight: Decimal
    distance: Decimal
    terms: tuple[LayerWeight, ...]
    reason: str | None
    unmodeled_extra: bool
    template: str

    @property
    def max_thickness_rank(self):
        return max(
            (
                THICKNESS_RANK[t.rank_thickness]
                for t in self.terms
                if t.value is not None and t.rank_thickness is not None
            ),
            default=UNKNOWN_STACK_SENTINEL,
        )

    @property
    def counted_layer_count(self):
        return sum(t.value is not None for t in self.terms)

    @property
    def passed(self):
        return self.reason is None


def check_upper_budget(items, ctx):
    template, layers, reason = infer_layers(items, ctx)
    terms = tuple(layer_weight(g, role, ctx, template) for role, g in layers)
    weight = sum((t.value for t in terms if t.value is not None), Decimal("0"))
    roles = {i["role"] for i in items}
    return BudgetCheck(
        weight,
        abs(weight - ctx.gap),
        terms,
        reason or budget_reason(weight, ctx),
        "upper" in roles and "one_piece" in roles,
        template,
    )


def thermal_sort_key(check):
    if not check.passed:
        raise ValueError("Only legal in-band stacks may be ranked")
    return check.max_thickness_rank, -check.counted_layer_count, check.distance


def key_output(key):
    if key is None:
        return None
    rank, negative_count, distance = key
    return {
        "max_thickness_rank": rank,
        "counted_layer_count": -negative_count,
        "distance_c": float(distance),
    }


@dataclass(frozen=True)
class FirstChoiceGuard:
    best_light_distance: Decimal | None
    alternative_search_complete: bool
    best_light_key: tuple[int, int, Decimal] | None = None

    @property
    def has_light_complete_candidate(self):
        return self.best_light_distance is not None

    def output(self):
        return {
            "best_light_distance_c": None
            if self.best_light_distance is None
            else float(self.best_light_distance),
            "alternative_search_complete": self.alternative_search_complete,
            "has_light_complete_candidate": self.has_light_complete_candidate,
            "best_light_key": key_output(self.best_light_key),
        }


def outer_thickness(candidate):
    return next(
        (
            confirmed_value(i["garment"], "thickness")
            for i in candidate["items"]
            if i["role"] == "outerwear"
        ),
        None,
    )


@dataclass(frozen=True)
class FirstChoiceDecision:
    allowed: bool
    reason: str


def first_choice_decision(candidate, ctx, guard):
    check = check_upper_budget(candidate["items"], ctx)
    if not check.passed:
        return FirstChoiceDecision(False, check.reason)
    if any(item_reason(i["garment"], i["role"], ctx) for i in candidate["items"]):
        return FirstChoiceDecision(False, "invalid_layers")
    thickness = outer_thickness(candidate)
    if thickness not in {"thick", "very_thick"}:
        return FirstChoiceDecision(True, "no_heavy_outerwear")
    if ctx.hottest_blocks_heavy_first:
        return FirstChoiceDecision(False, "hottest_heavy_first_blocked")
    if guard is not None and guard.has_light_complete_candidate:
        return FirstChoiceDecision(False, "light_complete_available")
    if guard is None or not guard.alternative_search_complete:
        return FirstChoiceDecision(False, "search_incomplete")
    if ctx.very_cold:
        return FirstChoiceDecision(True, "very_cold_no_light")
    if ctx.medium_pair_insufficient:
        return FirstChoiceDecision(True, "medium_pair_insufficient")
    return FirstChoiceDecision(False, "mild_light_capacity_sufficient")


def first_outfit_allowed(candidate, ctx, guard):
    return first_choice_decision(candidate, ctx, guard).allowed


def selected_output(outfits, ctx, guard):
    result = {}
    for index, outfit in enumerate(outfits):
        check = check_upper_budget(outfit["items"], ctx)
        decision = first_choice_decision(outfit, ctx, guard)
        rank = check.max_thickness_rank
        result[outfit["outfit_id"]] = {
            "upper_warmth_c": float(check.weight),
            "distance_c": float(check.distance),
            "terms": [term.output() for term in check.terms],
            "first_choice_allowed": decision.allowed,
            "first_choice_reason": decision.reason,
            "thick_fallback": index == 0
            and decision.allowed
            and outer_thickness(outfit) in {"thick", "very_thick"},
            "layer_template": check.template,
            "max_thickness": next(
                (name for name, value in THICKNESS_RANK.items() if value == rank), None
            ),
            "max_thickness_rank": rank,
            "counted_layer_count": check.counted_layer_count,
            "season_compatible": True if len(check.terms) == 2 else None,
        }
    return result


def assumptions_for(outfits, ctx):
    notes = [
        f"保暖参考温度约 {ctx.reference}℃，缺口约 {ctx.gap}℃；使用两层等效保暖估算。"
    ]
    if ctx.method == "range_midpoint":
        notes.append("仅有温度极值，本次使用 min／max 中点近似，无法还原逐时中位数。")
    if ctx.basis == "temperature":
        notes.append("体感缺失，保暖参考温度回退同窗气温。")
    for outfit in outfits:
        check = check_upper_budget(outfit["items"], ctx)
        if any("layerable_inner_budget_assumption" in t.sources for t in check.terms):
            notes.append("可叠内搭厚薄未知，仅在预算中按1.5℃估算，未写回衣物资料。")
        for term in check.terms:
            if "summer_layer_budget_assumption" in term.sources:
                notes.append(
                    f"夏季叠穿{term.role}（{term.garment_id}）厚薄未知，仅在预算中按1.5℃估算，未写回衣物资料。"
                )
        if check.template == "summer_open_shirt":
            notes.append("T恤内搭＋短袖衬衫敞开外层；两层季节兼容。")
        if any(t.value is None for t in check.terms):
            notes.append("未知厚薄项未计入已知保暖量。")
        if check.unmodeled_extra:
            notes.append("连体衣在独立可叠内搭之外的未建模额外保暖未计入预算。")
    return list(dict.fromkeys(notes))
