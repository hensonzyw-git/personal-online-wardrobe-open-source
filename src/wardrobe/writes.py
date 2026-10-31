import hashlib
import json
import time
from uuid import uuid4

from .contracts import Principal
from .errors import BusinessError
from .models import AuditEvent, Idempotency, ServiceState


def canonical(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def idempotent(
    session, principal: Principal, action: str, key: str, payload: dict, callback
):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise BusinessError(
            "IDEMPOTENCY_KEY_REQUIRED", "A bounded idempotency key is required", 422
        )
    identifier = (principal.owner_id, principal.client_id, action, key)
    fingerprint = digest(payload)
    saved = session.get(Idempotency, identifier)
    if saved is not None:
        if saved.digest != fingerprint:
            raise BusinessError(
                "IDEMPOTENCY_CONFLICT", "This key was used for different arguments", 409
            )
        return saved.response
    value = callback()
    session.add(
        Idempotency(
            owner_id=principal.owner_id,
            client_id=principal.client_id,
            action=action,
            key=key,
            digest=fingerprint,
            response=value,
        )
    )
    return value


def audit(session, principal, action: str, target_id: str, receipt_id: str):
    session.add(
        AuditEvent(
            id=str(uuid4()),
            owner_id=principal.owner_id,
            client_id=principal.client_id,
            action=action,
            target_id=target_id,
            receipt_id=receipt_id,
            created_at=int(time.time()),
        )
    )


def bump_catalog(session, owner_id: str):
    state = session.get(ServiceState, owner_id)
    if state is None:
        raise BusinessError(
            "SERVICE_NOT_INITIALIZED", "Owner catalog is not initialized", 503
        )
    state.catalog_revision += 1
