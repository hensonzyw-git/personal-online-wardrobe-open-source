"""Human-confirmed storage selection. Unknown categories never become year-round."""

import time
from typing import Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select

from .auth import recheck_grant
from .contracts import StrictModel, success
from .errors import BusinessError
from .image_previews import image_preview_response
from .models import Garment, HistoryEvent, ServiceState, WardrobePreference
from .writes import audit, digest, idempotent

Season = Literal["spring_summer", "autumn_winter"]
RULES_VERSION = "confirmed-seasons/1"
RULES = {
    "短袖衬衫": ("spring_summer", "短袖"),
    "短裤": ("spring_summer", None),
    "短袖连帽上衣": ("spring_summer", "短袖"),
    **{
        c: ("autumn_winter", "长袖")
        for c in (
            "外套",
            "翻领外套",
            "连帽分格外套",
            "连帽外套",
            "长袖衬衫",
            "针织上衣",
            "连帽上衣",
            "长袖上衣",
            "长款纽扣上衣",
        )
    },
    "长裤": ("autumn_winter", None),
    "短袖T恤": ("all_year", "短袖"),
    "低帮运动鞋": ("all_year", None),
    "中帮运动鞋": ("all_year", None),
}
RULES_DIGEST = digest(RULES)


def classification(garment):
    # Chat intake explicitly asks for storage season. Only a user-attested
    # single storage enum can override category rules; visual climate labels
    # (spring/summer etc.) and unverified model suggestions remain ineffective.
    fields = (
        garment["annotation_fields"]
        if isinstance(garment, dict)
        else garment.annotation_fields
    )
    category = (
        garment.get("category") if isinstance(garment, dict) else garment.category
    )
    field = fields.get("seasons", {})
    value = field.get("value")
    if (
        field.get("verification_state") == "user_verified"
        and field.get("source_kind") == "user_supplied"
        and isinstance(value, list)
        and len(value) == 1
        and value[0] in {"spring_summer", "autumn_winter", "all_year"}
    ):
        return value[0]
    rule = RULES.get(category)
    if rule is None:
        return "unassigned"
    sleeve = fields.get("sleeve", {}).get("value")
    return rule[0] if rule[1] is None or sleeve == rule[1] else "unassigned"


def compatible(a, b):
    """Unknown storage seasons cannot authorize a two-layer combination."""
    sa, sb = classification(a), classification(b)
    if "unassigned" in {sa, sb}:
        return False
    return sa == sb or "all_year" in {sa, sb}


def preference(session, owner):
    # SELECT bypasses the ORM identity map when another transaction changed state.
    row = session.execute(
        select(WardrobePreference)
        .where(WardrobePreference.owner_id == owner)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(
            "SEASON_NOT_INITIALIZED",
            "Storage season requires explicit initialization",
            503,
        )
    if row.rules_version != RULES_VERSION or row.rules_digest != RULES_DIGEST:
        raise BusinessError(
            "SEASON_RULES_UNAVAILABLE", "Exact confirmed rules are unavailable", 503
        )
    return row


def context(row):
    return {
        "active_season": row.active_season,
        "revision": row.revision,
        "rules_version": row.rules_version,
        "rules_digest": row.rules_digest,
    }


def initialize(database, owner, season):
    if season not in {"spring_summer", "autumn_winter"}:
        raise ValueError("Explicit storage season required")
    with database.write() as session:
        existing = session.get(WardrobePreference, owner)
        if existing:
            if context(existing) != {
                "active_season": season,
                "revision": 1,
                "rules_version": RULES_VERSION,
                "rules_digest": RULES_DIGEST,
            }:
                raise BusinessError(
                    "SEASON_INITIALIZATION_CONFLICT",
                    "Initialization cannot overwrite state",
                    409,
                )
            return context(existing)
        now = int(time.time())
        row = WardrobePreference(
            owner_id=owner,
            active_season=season,
            revision=1,
            rules_version=RULES_VERSION,
            rules_digest=RULES_DIGEST,
            initialized_at=now,
            updated_at=now,
        )
        session.add(row)
        return context(row)


class SeasonInput(StrictModel):
    season: Season
    expected_revision: int = Field(ge=1)


class SearchInput(StrictModel):
    view: Literal["current", "spring_summer", "autumn_winter", "all"] = "current"
    category: str | None = Field(default=None, min_length=1, max_length=100)
    category_group: Literal["upper", "lower", "outerwear", "footwear"] | None = None
    q: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, min_length=1, max_length=60)
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=4096)


class HistorySearch(StrictModel):
    kind: Literal["generated", "shown", "adopted", "worn", "rejected"] | None = None
    recommendation_id: str | None = Field(default=None, max_length=36)
    outfit_id: str | None = Field(default=None, max_length=36)
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=4096)


class SeasonService:
    def __init__(self, database, catalog):
        self.database, self.catalog = database, catalog

    @staticmethod
    def counts(session, owner, current):
        rows = list(
            session.scalars(
                select(Garment).where(
                    Garment.owner_id == owner, Garment.state == "active"
                )
            )
        )
        kinds = [classification(g) for g in rows]
        eligible = sum(k in {current, "all_year"} for k in kinds)
        return {
            "catalog_count": len(rows),
            "catalog_eligible_count": eligible,
            "eligible_count": eligible,  # Compatibility alias; always catalog-wide.
            "count_scopes": {
                "catalog_count": "owner_catalog",
                "catalog_eligible_count": "owner_catalog_current_storage_season",
                "eligible_count": "owner_catalog_current_storage_season",
                "all_year_count": "owner_catalog",
                "unassigned_count": "owner_catalog",
            },
            "all_year_count": kinds.count("all_year"),
            "unassigned_count": kinds.count("unassigned"),
        }

    def get(self, principal):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            row = preference(session, principal.owner_id)
            return success(
                {
                    **context(row),
                    **self.counts(session, principal.owner_id, row.active_season),
                }
            )

    def set(self, principal, payload, key):
        principal.require("wardrobe.season")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.season")

            def work():
                row = preference(session, principal.owner_id)
                if row.revision != payload.expected_revision:
                    raise BusinessError(
                        "SEASON_REVISION_CONFLICT",
                        "Storage season changed; refresh before another explicit choice",
                        409,
                    )
                old, changed = row.active_season, row.active_season != payload.season
                if changed:
                    row.active_season, row.revision, row.updated_at = (
                        payload.season,
                        row.revision + 1,
                        int(time.time()),
                    )
                receipt = str(uuid4())
                audit(session, principal, "set_season", principal.owner_id, receipt)
                return success(
                    {
                        **context(row),
                        "receipt_id": receipt,
                        "previous_season": old,
                        "changed": changed,
                        "updated_at": row.updated_at,
                        **self.counts(session, principal.owner_id, row.active_season),
                    }
                )

            return idempotent(
                session, principal, "set_season", key, payload.model_dump(), work
            )

    @image_preview_response
    def search(self, principal, query):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            pref = (
                preference(session, principal.owner_id)
                if query.view != "all"
                else session.get(WardrobePreference, principal.owner_id)
            )
            current = pref.active_season if pref else None
            if pref and (
                pref.rules_version != RULES_VERSION or pref.rules_digest != RULES_DIGEST
            ):
                raise BusinessError(
                    "SEASON_RULES_UNAVAILABLE", "Confirmed rule version mismatch", 503
                )
            state = session.get(ServiceState, principal.owner_id)
            binding = {
                "v": 2,
                "type": "garment-search",
                "owner": principal.owner_id,
                "filters": query.model_dump(exclude={"cursor"}),
                "catalog_revision": state.catalog_revision,
                "season": context(pref) if pref else None,
                "rules_digest": RULES_DIGEST,
            }
            last = self._cursor(query.cursor, binding)
            rows = list(
                session.scalars(
                    select(Garment)
                    .where(
                        Garment.owner_id == principal.owner_id,
                        Garment.state == "active",
                    )
                    .order_by(Garment.id)
                )
            )
            target = current if query.view == "current" else query.view
            matched = []
            colors = {"白色": "white", "黑色": "black", "蓝色": "blue", "灰色": "gray"}
            for g in rows:
                kind = classification(g)
                values = {k: f.get("value") for k, f in g.annotation_fields.items()}
                if target != "all" and kind not in {target, "all_year"}:
                    continue
                if query.category and g.category != query.category:
                    continue
                if query.category_group and query.category_group not in g.roles:
                    continue
                if (
                    query.q
                    and query.q.casefold()
                    not in " ".join(
                        str(v or "")
                        for v in [
                            g.name,
                            values.get("brand"),
                            values.get("model"),
                            (g.product_reference or {}).get("article_number"),
                            (g.identity_fields or {})
                            .get("article_number", {})
                            .get("value"),
                        ]
                    ).casefold()
                ):
                    continue
                actual_colors = [
                    values.get("primary_color"),
                    *(values.get("secondary_colors") or []),
                ]
                if query.color and colors.get(
                    query.color, query.color
                ).casefold() not in {
                    colors.get(c, c).casefold()
                    for c in actual_colors
                    if isinstance(c, str)
                }:
                    continue
                matched.append(g)
            remaining = [g for g in matched if last is None or g.id > last]
            page = remaining[: query.limit]
            items = []
            for g in page:
                view = self.catalog._view(g, session)
                items.append(
                    {
                        k: view[k]
                        for k in ("id", "name", "category", "roles", "image_references")
                    }
                    | {
                        "season_assignment": classification(g),
                        "stored": classification(g) not in {current, "all_year"},
                        "primary_color": g.annotation_fields.get("primary_color", {}),
                    }
                )
            more = len(remaining) > query.limit
            counts = self.counts(session, principal.owner_id, current)
            counts["count_scopes"]["match_count"] = "complete_filtered_selection"
            return success(
                {
                    "items": items,
                    "match_count": len(matched),
                    **counts,
                    "active_season": current,
                    "next_cursor": self.catalog._encode(
                        {**binding, "last": page[-1].id}
                    )
                    if more
                    else None,
                    "has_more": more,
                    "catalog_revision": state.catalog_revision,
                }
            )

    def _cursor(self, cursor, binding):
        if cursor is None:
            return None
        decoded = self.catalog._decode(cursor)
        if set(decoded) != {*binding, "last"} or any(
            decoded[k] != v for k, v in binding.items()
        ):
            raise BusinessError(
                "CURSOR_STALE",
                "Snapshot or filters changed; refresh from first page",
                409,
            )
        return decoded["last"]

    @image_preview_response
    def history(self, principal, query):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            state = session.get(ServiceState, principal.owner_id)
            binding = {
                "v": 2,
                "type": "history-search",
                "owner": principal.owner_id,
                "filters": query.model_dump(exclude={"cursor"}),
                "history_revision": state.history_revision,
            }
            last = self._cursor(query.cursor, binding)
            stmt = select(HistoryEvent).where(
                HistoryEvent.owner_id == principal.owner_id,
                HistoryEvent.active.is_(True),
            )
            for field in ("kind", "recommendation_id", "outfit_id"):
                if getattr(query, field) is not None:
                    stmt = stmt.where(
                        getattr(HistoryEvent, field) == getattr(query, field)
                    )
            rows = list(
                session.scalars(
                    stmt.order_by(
                        HistoryEvent.occurred_at.desc(), HistoryEvent.id.desc()
                    )
                )
            )
            rows = [
                e for e in rows if last is None or (e.occurred_at, e.id) < tuple(last)
            ]
            page, more = rows[: query.limit], len(rows) > query.limit
            return success(
                {
                    "items": [self.event(e) for e in page],
                    "has_more": more,
                    "history_revision": state.history_revision,
                    "next_cursor": self.catalog._encode(
                        {**binding, "last": [page[-1].occurred_at, page[-1].id]}
                    )
                    if more
                    else None,
                }
            )

    @staticmethod
    def event(e):
        return {
            "id": e.id,
            "recommendation_id": e.recommendation_id,
            "outfit_id": e.outfit_id,
            "kind": e.kind,
            "occurred_at": e.occurred_at,
            "revision": e.revision,
            "source": e.payload.get("source"),
            "note": e.payload.get("note"),
            "wardrobe_context": e.payload.get("wardrobe_context"),
        }

    @image_preview_response
    def feedback(self, principal, event_id):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            row = session.get(HistoryEvent, event_id)
            if (
                row is None
                or row.owner_id != principal.owner_id
                or not row.active
                or row.kind == "generated"
            ):
                raise BusinessError("NOT_FOUND", "Feedback not found", 404)
            return success(self.event(row))
