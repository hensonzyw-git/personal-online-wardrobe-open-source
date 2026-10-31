import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .contracts import Principal
from .errors import BusinessError
from .models import ClientGrant


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


AGENT_SCOPES = frozenset(
    {
        "wardrobe.read",
        "wardrobe.intake",
        "wardrobe.intake_upload",
        "wardrobe.intake_observe",
        "wardrobe.create",
        "wardrobe.propose_change",
        "wardrobe.recommend",
        "wardrobe.feedback",
        "wardrobe.season",
        "wardrobe.annotate",
    }
)
HUMAN_SCOPES = frozenset({"human.decision", "human.preview", "human.intake_create"})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Authenticator:
    def __init__(self, sessions, audience: str, now=time.time):
        self.sessions, self.audience, self.now = sessions, audience, now

    def authenticate(self, token: str) -> Principal:
        if len(token) < 32 or len(token) > 512:
            raise BusinessError("UNAUTHORIZED", "Valid authentication is required", 401)
        with self.sessions.begin() as session:
            grant = session.scalar(
                select(ClientGrant).where(ClientGrant.token_hash == token_hash(token))
            )
            now = int(self.now())
            if (
                grant is None
                or grant.revoked_at is not None
                or grant.created_at > now
                or grant.expires_at <= now
                or grant.audience != self.audience
            ):
                raise BusinessError(
                    "UNAUTHORIZED", "Valid authentication is required", 401
                )
            return Principal(
                grant.owner_id,
                grant.client_id,
                frozenset(grant.scopes),
                grant.channel,
                grant.audience,
                grant.token_hash,
            )


def recheck_grant(session, principal, *, required_scope):
    grant = session.get(ClientGrant, principal.grant_id) if principal.grant_id else None
    now = int(time.time())
    if (
        grant is None
        or grant.revoked_at is not None
        or grant.expires_at <= now
        or grant.owner_id != principal.owner_id
        or grant.client_id != principal.client_id
        or grant.channel != principal.channel
        or grant.audience != principal.audience
        or required_scope not in principal.scopes
        or required_scope not in grant.scopes
    ):
        raise BusinessError(
            "UNAUTHORIZED", "Authorization changed; obtain a fresh request context", 401
        )


class PrivateServiceMiddleware:
    """Validate every HTTP request, including MCP discovery and legacy sessions."""

    def __init__(self, app, *, authenticator: Authenticator, settings):
        self.app, self.authenticator, self.settings = app, authenticator, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            headers = scope.get("headers", [])
            hosts = [
                value.decode("latin-1")
                for key, value in headers
                if key.lower() == b"host"
            ]
            try:
                host = urlsplit("//" + hosts[0]).hostname if len(hosts) == 1 else None
            except ValueError:
                host = None
            if host not in self.settings.allowed_hosts:
                raise BusinessError("HOST_DENIED", "Request host is not allowed", 421)
            origins = [
                value.decode("latin-1")
                for key, value in headers
                if key.lower() == b"origin"
            ]
            if (
                len(origins) > 1
                or origins
                and origins[0] not in self.settings.allowed_origins
            ):
                raise BusinessError(
                    "ORIGIN_DENIED", "Request origin is not allowed", 403
                )
            if scope["path"] != "/healthz":
                credentials = [
                    value.decode("latin-1")
                    for key, value in headers
                    if key.lower() == b"authorization"
                ]
                if len(credentials) != 1:
                    raise BusinessError(
                        "UNAUTHORIZED", "Valid authentication is required", 401
                    )
                parts = credentials[0].split(" ")
                if len(parts) != 2 or parts[0].lower() != "bearer":
                    raise BusinessError(
                        "UNAUTHORIZED", "Valid authentication is required", 401
                    )
                try:
                    principal = await run_in_threadpool(
                        self.authenticator.authenticate, parts[1]
                    )
                except SQLAlchemyError:
                    raise BusinessError(
                        "DATABASE_UNAVAILABLE",
                        "Authentication store is unavailable",
                        503,
                    ) from None
                if scope["path"].startswith("/mcp") and principal.channel != "agent":
                    raise BusinessError(
                        "CHANNEL_DENIED", "This credential is not an agent channel", 403
                    )
                scope["wardrobe.principal"] = principal
            # Buffer only small JSON/control requests. Binary upload is streamed
            # under its own declared size limit, checked before creating a file.
            agent_binary = scope["method"] == "PUT" and re.fullmatch(
                r"/v1/agent/intakes/[0-9a-f-]{36}/images/[0-9a-f-]{36}", scope["path"]
            )
            if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"} and not (
                scope["path"].endswith("/content") or agent_binary
            ):
                chunks, count = [], 0
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    chunk = message.get("body", b"")
                    count += len(chunk)
                    if count > 1024 * 1024:
                        raise BusinessError(
                            "REQUEST_TOO_LARGE", "Control request exceeds 1 MiB", 413
                        )
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                body = b"".join(chunks)
                if body:
                    try:
                        json.loads(
                            body,
                            object_pairs_hook=_unique_json,
                            parse_constant=lambda _value: (_ for _ in ()).throw(
                                ValueError()
                            ),
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        raise BusinessError(
                            "INVALID_ARGUMENT",
                            "A valid JSON document with unique fields is required",
                            422,
                        ) from None
                delivered = False
                upstream_receive = receive

                async def buffered_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {
                            "type": "http.request",
                            "body": body,
                            "more_body": False,
                        }
                    return await upstream_receive()

                receive = buffered_receive
            await self.app(scope, receive, send)
        except BusinessError as error:
            extra = {"Cache-Control": "no-store"}
            if error.http_status == 401:
                extra["WWW-Authenticate"] = "Bearer"
            response = error.response()
            if scope["path"].startswith("/v1/agent/"):
                from .agent_service import error_response, operation_from_path

                response = error_response(error, operation_from_path(scope["path"]))
            await JSONResponse(response, status_code=error.http_status, headers=extra)(
                scope, receive, send
            )
