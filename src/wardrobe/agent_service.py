"""Provider-owned workflow over the existing wardrobe authority.

GETs only project state. Recommendation work is claimed durably and enqueued
atomically with its operation binding.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timedelta
from importlib.metadata import version
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .auth import recheck_grant
from .contracts import Principal
from .errors import BusinessError
from .image_previews import present_images
from .intakes import TTL, fail
from .models import AgentOperation, ClientGrant, Idempotency, Recommendation
from .recommendations import RecommendationInput
from .seasons import preference
from .writes import canonical, digest, idempotent

PROTOCOL = "wardrobe-agent/1"
LEASE = 240
LOG = logging.getLogger(__name__)
SCOPES = {
    "query": "wardrobe.read",
    "recommend": "wardrobe.recommend",
    "feedback": "wardrobe.feedback",
    "season": "wardrobe.season",
}
MESSAGES = {
    "UNAUTHORIZED": "衣橱授权已失效，请重新授权。",
    "SCOPE_DENIED": "当前连接没有这项权限。",
    "AGENT_DISABLED": "提供方尚未启用此版本的业务接口。",
    "AGENT_PROTOCOL_UNSUPPORTED": "连接器协议不兼容，请由提供方处理。",
    "REVISION_CONFLICT": "草稿已变化，请重新查看当前草稿。",
    "INVALID_CONFIRMATION": "确认卡已变化或过期，需要重新查看并确认。",
    "DUPLICATE_SET_CHANGED": "重复衣物提示已变化，需要重新核对。",
    "OBSERVATION_OUTCOME_UNKNOWN": "识别结果尚未确定，已停止重复调用。",
    "OPERATION_OUTCOME_UNKNOWN": "原操作结果尚未确定，正在等待核对。",
    "PRODUCT_NOT_READY": "商品资料尚未就绪，可等待或明确选择跳过。",
}


def envelope(data, *, state="succeeded", reference=None, kind=None):
    return {
        "schema_version": PROTOCOL,
        "request_id": str(uuid4()),
        "status": state,
        "operation_ref": reference,
        "kind": kind,
        "data": data,
    }


def operation_from_path(path):
    match = re.match(
        r"^/v1/agent/operations/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)",
        path,
    )
    return match[1] if match else None


def error_response(error, reference=None):
    code = (
        error.code
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", error.code)
        else "OPERATION_FAILED"
    )
    recovery = (
        "reauthorize"
        if code in {"UNAUTHORIZED", "SCOPE_DENIED"}
        else "refresh_review"
        if code
        in {
            "REVISION_CONFLICT",
            "INVALID_CONFIRMATION",
            "DUPLICATE_SET_CHANGED",
            "INPUT_CHANGED",
        }
        else "support"
        if "UNKNOWN" in code
        else "poll"
        if code.endswith("_BUSY")
        else "stop"
    )
    return {
        **envelope(None, state="failed", reference=reference),
        "error": {
            "code": code,
            "message": MESSAGES.get(
                code, "衣橱操作未完成，请保留操作引用供提供方核查。"
            ),
            "recovery": recovery,
            "retry_after_ms": 1000 if recovery == "poll" else None,
        },
    }


class AgentService:
    def __init__(self, database, settings, recommendations, seasons, catalog, assets):
        self.db, self.settings = database, settings
        self.recommendations, self.seasons = recommendations, seasons
        self.catalog, self.assets = catalog, assets

    def auth(self, s, p, scope, *, write=False):
        p.require(scope)
        recheck_grant(s, p, required_scope=scope)
        if write and not self.settings.agent_enabled:
            fail("AGENT_DISABLED", status=503)

    def capabilities(self, p):
        with self.db.sessions.begin() as s:
            permitted = sorted(set(SCOPES.values()) & p.scopes)
            if not permitted:
                fail("SCOPE_DENIED", status=403)
            for scope in permitted:
                self.auth(s, p, scope)
            return envelope(
                {
                    "protocol_major": 1,
                    "service_version": version("open-wardrobe"),
                    "database_revision": "0010_agent_facade",
                    "compatible_majors": [1],
                    "principal_ref": hmac.new(
                        self.assets.key,
                        canonical(
                            {
                                "domain": "agent-principal/1",
                                "owner": p.owner_id,
                                "client": p.client_id,
                            }
                        ),
                        hashlib.sha256,
                    ).hexdigest(),
                    "enabled": self.settings.agent_enabled,
                    "scopes": permitted,
                    "tools": {
                        name: scope in permitted
                        and (name == "query" or self.settings.agent_enabled)
                        for name, scope in SCOPES.items()
                    },
                    "dependencies": {"supplier_health": "unprobed"},
                    "attachment_delivery": "private_files",
                }
            )

    def operation(self, s, p, ref, *, write=False):
        op = s.get(AgentOperation, ref)
        if (
            op is None
            or op.kind != "recommendation"
            or (op.owner_id, op.client_id) != (p.owner_id, p.client_id)
        ):
            fail("NOT_FOUND", status=404)
        self.auth(s, p, "wardrobe.recommend" if write else "wardrobe.read", write=write)
        return op

    def old(self, s, p, kind, key, value):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            fail("IDEMPOTENCY_KEY_REQUIRED", status=422)
        op = s.scalar(
            select(AgentOperation).where(
                AgentOperation.owner_id == p.owner_id,
                AgentOperation.client_id == p.client_id,
                AgentOperation.kind == kind,
                AgentOperation.request_key == key,
            )
        )
        if op and op.request_hash != digest(value):
            fail("IDEMPOTENCY_CONFLICT")
        return op

    def recommend(self, p, payload, key):
        value = payload.model_dump(mode="json")
        if "timezone" not in payload.location.model_fields_set:
            value["location"].pop("timezone")
        if (
            payload.user_weather is not None
            and "timezone" not in payload.user_weather.location.model_fields_set
        ):
            value["user_weather"]["location"].pop("timezone")
        resolved = self.recommendations.with_default_timezone(payload)
        with self.db.write() as s:
            self.auth(s, p, "wardrobe.recommend", write=True)
            self.auth(s, p, "wardrobe.read")
            old = self.old(s, p, "recommendation", key, value)
            if old:
                return self.present(s, p, old)
            frozen = resolved.model_dump(mode="json")
            if payload.interval is None:
                offset = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
                requested = payload.target_date or "today"
                if requested in offset:
                    frozen["target_date"] = (
                        datetime.now(ZoneInfo(resolved.location.timezone)).date()
                        + timedelta(days=offset[requested])
                    ).isoformat()
            selected = preference(s, p.owner_id)
            if (
                payload.expected_season_revision is not None
                and payload.expected_season_revision != selected.revision
            ):
                fail("SEASON_REVISION_CONFLICT")
            frozen["expected_season_revision"] = selected.revision
            now = int(time.time())
            op = AgentOperation(
                id=str(uuid4()),
                owner_id=p.owner_id,
                client_id=p.client_id,
                grant_id=p.grant_id,
                kind="recommendation",
                request_key=key,
                request_hash=digest(value),
                state="processing",
                stage="recommend",
                details={"input": frozen},
                lease_until=0,
                created_at=now,
                updated_at=now,
                expires_at=now + TTL,
            )
            s.add(op)
            s.flush()
            return self.present(s, p, op)

    def present(self, s, p, op):
        if op.kind == "recommendation":
            # Callers authorize the operation for their read or write scope.
            row = (
                s.get(Recommendation, op.recommendation_id)
                if op.recommendation_id
                else None
            )
            state = op.state
            if op.state != "cancelled" and row:
                state = (
                    "processing" if row.state in {"queued", "running"} else row.state
                )
            if row and row.deadline <= int(time.time()) and state == "processing":
                state = "failed"
            return envelope(
                {
                    "result": present_images(row.result)
                    if row and row.result
                    else None,
                    "error": error_response(BusinessError(row.error["code"], ""))[
                        "error"
                    ]
                    if row and row.error
                    else None,
                    "poll_after_ms": 1000 if state == "processing" else None,
                },
                state=state,
                reference=op.id,
                kind=op.kind,
            )

    def get(self, p, ref):
        with self.db.sessions.begin() as s:
            return self.present(s, p, self.operation(s, p, ref))

    def by_key(self, p, kind, key):
        if kind != "recommendation" or not 1 <= len(key) <= 128:
            fail("INVALID_ARGUMENT", status=422)
        with self.db.sessions.begin() as s:
            self.auth(s, p, "wardrobe.read")
            op = s.scalar(
                select(AgentOperation).where(
                    AgentOperation.owner_id == p.owner_id,
                    AgentOperation.client_id == p.client_id,
                    AgentOperation.kind == kind,
                    AgentOperation.request_key == key,
                )
            )
            if not op:
                fail("NOT_FOUND", status=404)
            return self.present(s, p, self.operation(s, p, op.id))

    def command_receipt(self, p, kind, key):
        mapping = {
            "feedback": ("feedback", "wardrobe.feedback"),
            "season": ("set_season", "wardrobe.season"),
        }
        if kind not in mapping or not 1 <= len(key) <= 128:
            fail("INVALID_ARGUMENT", status=422)
        action, scope = mapping[kind]
        with self.db.sessions.begin() as s:
            self.auth(s, p, scope)
            saved = s.get(Idempotency, (p.owner_id, p.client_id, action, key))
            if not saved:
                fail("NOT_FOUND", status=404)
            return envelope(saved.response["data"])

    def cancel_operation(self, p, ref, key):
        with self.db.write() as s:
            op = s.get(AgentOperation, ref)
            if (
                op is None
                or (op.owner_id, op.client_id) != (p.owner_id, p.client_id)
                or op.kind != "recommendation"
            ):
                fail("NOT_FOUND", status=404)
            op = self.operation(s, p, ref, write=True)

            def work():
                if op.state == "cancelled":
                    return self.present(s, p, op)
                projected = self.present(s, p, op)["status"]
                if op.state in {"succeeded", "partial", "failed"} or projected in {
                    "succeeded",
                    "partial",
                    "failed",
                }:
                    fail("OPERATION_NOT_CANCELLABLE")
                if op.recommendation_id:
                    self.recommendations.seal_cancelled(s, op.recommendation_id)
                op.state, op.stage = "cancelled", "closed"
                op.lease_token, op.lease_until = None, 0
                op.updated_at = int(time.time())
                return self.present(s, p, op)

            return idempotent(s, p, "agent-cancel-operation", key, {"ref": ref}, work)

    def resume(self, p, ref, key):
        with self.db.write() as s:
            op = self.operation(s, p, ref, write=True)

            def work():
                if op.state in {"unknown", "needs_input"}:
                    op.state, op.stage, op.lease_until = "processing", "recommend", 0
                    op.details = {**op.details, "error_code": None}
                return self.present(s, p, op)

            return idempotent(s, p, "agent-resume", key, {"ref": ref}, work)

    def principal(self, s, op):
        grant = s.get(ClientGrant, op.grant_id)
        if (
            grant is None
            or grant.created_at > int(time.time())
            or grant.audience != self.settings.audience
            or grant.channel != "agent"
        ):
            fail("UNAUTHORIZED", status=401)
        return Principal(
            op.owner_id,
            op.client_id,
            frozenset(grant.scopes),
            grant.channel,
            grant.audience,
            grant.token_hash,
        )

    def claim(self):
        with self.db.write() as s:
            op = s.scalar(
                select(AgentOperation)
                .where(
                    AgentOperation.kind == "recommendation",
                    AgentOperation.state == "processing",
                    AgentOperation.lease_until <= int(time.time()),
                )
                .order_by(AgentOperation.updated_at)
                .limit(1)
            )
            if not op:
                return None
            op.lease_token, op.lease_until = str(uuid4()), int(time.time()) + LEASE
            return op.id, op.lease_token

    def _enqueue_recommendation(self, ref, token):
        with self.db.write() as s:
            op = s.get(AgentOperation, ref)
            if (
                op is None
                or op.kind != "recommendation"
                or op.state != "processing"
                or op.lease_token != token
            ):
                return
            p = self.principal(s, op)
            self.operation(s, p, ref, write=True)
            if not op.recommendation_id:
                result = self.recommendations._submit_in_session(
                    s,
                    p,
                    RecommendationInput.model_validate(op.details["input"]),
                    "agent-" + ref,
                )
                s.flush()
                op.recommendation_id = result["data"]["recommendation_id"]
            op.stage, op.state, op.lease_until = "recommended", "needs_input", 0

    async def advance(self, ref, token):
        with self.db.sessions.begin() as s:
            op = s.get(AgentOperation, ref)
            if op is None or op.kind != "recommendation":
                return
            p = self.principal(s, op)
            self.operation(s, p, ref, write=True)
            if op.lease_token != token or op.state != "processing":
                return
        await asyncio.to_thread(self._enqueue_recommendation, ref, token)

    def record_failure(self, ref, token, code):
        try:
            with self.db.write() as s:
                op = s.get(AgentOperation, ref)
                if op and op.lease_token == token and op.state == "processing":
                    op.state = "unknown" if "UNKNOWN" in code else "needs_input"
                    op.details = {**op.details, "error_code": code}
                    op.lease_until, op.lease_token = 0, None
        except SQLAlchemyError:
            # Leave the durable lease/intents for reconciliation after storage returns.
            LOG.warning("agent_storage_unavailable", extra={"operation_ref": ref})

    async def run(self):
        while True:
            if not self.settings.agent_enabled:
                await asyncio.sleep(0.5)
                continue
            try:
                claimed = await asyncio.to_thread(self.claim)
            except SQLAlchemyError:
                LOG.warning("agent_storage_unavailable")
                await asyncio.sleep(1)
                continue
            if not claimed:
                await asyncio.sleep(0.25)
                continue
            try:
                await self.advance(*claimed)
            except SQLAlchemyError:
                LOG.warning(
                    "agent_storage_unavailable", extra={"operation_ref": claimed[0]}
                )
                await asyncio.sleep(1)
            except (BusinessError, asyncio.CancelledError) as error:
                code = (
                    error.code
                    if isinstance(error, BusinessError)
                    else "OPERATION_OUTCOME_UNKNOWN"
                )
                self.record_failure(*claimed, code)
                if isinstance(error, asyncio.CancelledError):
                    raise
            except Exception as error:  # noqa: BLE001 -- persist uncertainty at the worker boundary
                LOG.warning(
                    "agent_operation_unknown",
                    extra={
                        "operation_ref": claimed[0],
                        "error_type": type(error).__name__,
                    },
                )
                self.record_failure(*claimed, "OPERATION_OUTCOME_UNKNOWN")
