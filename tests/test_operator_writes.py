"""Deterministic W-EDIT/CONC/IDEM/ATOM/PROP/DEL persistence contracts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from operator_support import OWNER, READ_TOKEN, T, headers, snapshot, uid
from operator_support import (
    operator_env as operator_env,  # noqa: PLC0414 - pytest fixture re-export
)
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from wardrobe.errors import BusinessError
from wardrobe.labels import LabelValues
from wardrobe.models import (
    AuditEvent,
    Garment,
    GarmentAsset,
    Idempotency,
    ServiceState,
)


def edit(e, patch, *, revision=1, key="synthetic-edit", target=10):
    return e.client.patch(
        f"/v1/operator/garments/{uid(target)}",
        headers=headers(key=key),
        json={"base_revision": revision, "patch": patch},
    )


def action(e, operation, *, revision=1, key=None, target=10):
    return e.client.post(
        f"/v1/operator/garments/{uid(target)}/{operation}",
        headers=headers(key=key or "synthetic-" + operation),
        json={"base_revision": revision},
    )


LABEL_EXAMPLES = {
    "brand": "合成品牌",
    "model": "synthetic-model",
    "subcategory": "T恤",
    "primary_color": "blue",
    "secondary_colors": ["white"],
    "color_distribution": "solid",
    "lightness": "light",
    "saturation": "low",
    "pattern": ["plain"],
    "pattern_density": "low",
    "visual_focus": "collar",
    "material": "linen",
    "composition": "cotton",
    "texture": "smooth",
    "thickness": "medium",
    "warmth": "medium",
    "breathability": "high",
    "windproof": False,
    "waterproof": True,
    "silhouette": "straight",
    "length": "regular",
    "sleeve": "long",
    "neckline": "round",
    "closure": "buttons",
    "fit": "regular",
    "shoe_shape": "round",
    "style": ["simple"],
    "formality": "casual",
    "occasions": ["daily"],
    "layering": ["base"],
    "seasons": ["spring"],
    "weather_notes": "synthetic",
    "size": "M",
    "fit_feedback": "comfortable",
    "preference": "preferred",
    "notes": "synthetic only",
}


@pytest.mark.parametrize("key,value", list(LABEL_EXAMPLES.items()))
def test_w_edit_1_all_36_label_keys_persist_and_audit(operator_env, key, value):
    e = operator_env
    assert set(LABEL_EXAMPLES) == set(LabelValues.model_fields)
    before = snapshot(e.db)
    r = edit(e, {"annotation_fields": {key: value}})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["revision"] == 2 and data["base_revision"] == 1
    assert data["catalog_revision"] == before["catalog"] + 1
    assert f"annotation_fields.{key}" in data["changed_fields"]
    after = snapshot(e.db)
    assert after["garment"]["annotation_fields"][key] == {
        "value": value,
        "source_kind": "caller_supplied",
        "verification_state": "unverified",
        "evidence": [],
        "updated_at": T,
    }
    assert after["garment"]["annotation_review_state"] == "published"
    for immutable in ("created_at", "identity_fields", "product_reference"):
        assert before["garment"][immutable] == after["garment"][immutable]
    assert after["change_proposals"] == before["change_proposals"] == 0
    assert after["human_decisions"] == before["human_decisions"] == 0
    with e.db.sessions.begin() as s:
        audit = s.scalar(select(AuditEvent))
        assert (audit.client_id, audit.action, audit.target_id, audit.receipt_id) == (
            "wardrobe-admin",
            "operator_update_garment",
            uid(10),
            data["receipt_id"],
        )
    get = e.client.get(f"/v1/operator/garments/{uid(10)}", headers=headers()).json()[
        "data"
    ]["garment"]
    assert get["annotation_fields"][key]["value"] == value


def test_w_edit_1_scalars_null_and_same_value_write(operator_env):
    e = operator_env
    patch = {
        "name": "合成衣物",
        "category": None,
        "roles": [],
        "availability": "unavailable",
    }
    r = edit(e, patch)
    assert r.status_code == 200, r.text
    assert {key: snapshot(e.db)["garment"][key] for key in patch} == patch
    assert (
        edit(e, patch, revision=2, key="synthetic-same-value").json()["data"][
            "revision"
        ]
        == 3
    )


def test_w_edit_2_merge_and_provenance_distinguish_omitted_null_false_empty(
    operator_env,
):
    e = operator_env
    material = snapshot(e.db)["garment"]["annotation_fields"]["material"]
    assert (
        edit(
            e, {"annotation_fields": {"notes": None, "windproof": False, "pattern": []}}
        ).status_code
        == 200
    )
    fields = snapshot(e.db)["garment"]["annotation_fields"]
    assert fields["material"] == material
    assert fields["notes"]["value"] is None
    assert fields["windproof"]["value"] is False
    assert fields["pattern"]["value"] == []
    assert (
        edit(
            e,
            {"annotation_fields": {"material": None}},
            revision=2,
            key="synthetic-clear",
        ).status_code
        == 200
    )
    fields = snapshot(e.db)["garment"]["annotation_fields"]
    assert (
        fields["material"]["value"] is None
        and fields["material"]["verification_state"] == "unverified"
    )


FORBIDDEN = [
    "asset_ids",
    "image_references",
    "product_reference",
    "product_evidence_summary",
    "product_evidence",
    "identity_fields",
    "article_number",
    "source_kind",
    "verification_state",
    "evidence",
    "verified_fields",
    "annotation_version_id",
    "snapshot_ids",
    "state",
    "revision",
    "created_at",
    "deleted_at",
    "restore_until",
    "owner_id",
    "client_id",
    "id",
    "source_reference",
    "history_kind",
    "annotation_review_state",
]


@pytest.mark.parametrize("key", FORBIDDEN)
@pytest.mark.parametrize("location", ["patch", "top", "annotation"])
def test_w_edit_3_forbidden_fields_atomic(operator_env, key, location):
    e = operator_env
    before = snapshot(e.db)
    payload = {"base_revision": 1, "patch": {"name": "must-not-persist"}}
    if location == "patch":
        payload["patch"][key] = "synthetic"
    elif location == "top":
        payload[key] = "synthetic"
    else:
        payload["patch"]["annotation_fields"] = {key: "synthetic"}
    r = e.client.patch(
        f"/v1/operator/garments/{uid(10)}", headers=headers(), json=payload
    )
    assert r.status_code == 422, r.text
    assert r.headers["cache-control"] == "no-store"
    assert snapshot(e.db) == before


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": None},
        {"category": ""},
        {"category": "  "},
        {"category": "x" * 101},
        {"roles": None},
        {"roles": ["invalid"]},
        {"roles": ["upper"] * 9},
        {"availability": "clean"},
        {"availability": None},
        {"annotation_fields": None},
        {"annotation_fields": {}},
        {"annotation_fields": {"thickness": "huge"}},
        {"annotation_fields": {"windproof": "false"}},
        {"annotation_fields": {"notes": "x" * 1001}},
    ],
)
def test_w_edit_4_invalid_patch(operator_env, patch):
    before = snapshot(operator_env.db)
    assert edit(operator_env, patch).status_code == 422
    assert snapshot(operator_env.db) == before


@pytest.mark.parametrize("revision", [None, 0, -1, True, "1", 1.0])
def test_w_edit_4_base_revision_strict(operator_env, revision):
    before = snapshot(operator_env.db)
    assert edit(operator_env, {"name": "invalid"}, revision=revision).status_code == 422
    assert snapshot(operator_env.db) == before


def test_w_edit_4_media_json_uuid_and_body_limits(operator_env):
    e = operator_env
    before = snapshot(e.db)
    path = f"/v1/operator/garments/{uid(10)}"
    for content, media, status in [
        (
            b'{"base_revision":1,"base_revision":1,"patch":{"name":"x"}}',
            "application/json",
            422,
        ),
        (
            b'{"base_revision":1,"patch":{"name":"x","name":"y"}}',
            "application/json",
            422,
        ),
        (b'{"base_revision":NaN,"patch":{"name":"x"}}', "application/json", 422),
        (b"not JSON", "text/plain", 415),
        (b" " * 65537, "application/json", 413),
    ]:
        r = e.client.patch(
            path, headers={**headers(), "Content-Type": media}, content=content
        )
        assert r.status_code == status, r.text
        assert r.headers["cache-control"] == "no-store"
    assert (
        e.client.patch(
            "/v1/operator/garments/not-a-uuid",
            headers=headers(),
            json={"base_revision": 1, "patch": {"name": "x"}},
        ).status_code
        == 422
    )
    assert snapshot(e.db) == before


@pytest.mark.parametrize("operation", ["update", "delete", "restore"])
def test_w_conc_1_two_connections_one_revision(operator_env, operation):
    from wardrobe.db import Database
    from wardrobe.operator_contracts import OperatorRevision, OperatorUpdate
    from wardrobe.operator_service import OperatorService

    e = operator_env
    target = uid(20) if operation == "restore" else uid(10)
    db2 = Database(e.db.path)
    second = OperatorService(db2, e.key, e.app.state.assets)
    barrier = threading.Barrier(2)

    def invoke(service, name):
        payload = (
            OperatorUpdate(base_revision=1, patch={"name": name})
            if operation == "update"
            else OperatorRevision(base_revision=1)
        )
        barrier.wait(timeout=5)
        try:
            return getattr(service, operation)(
                e.principal, target, payload, "synthetic-concurrent-" + name
            )
        except BusinessError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(invoke, svc, name)
                for svc, name in ((e.app.state.operator, "a"), (second, "b"))
            ]
            result = [f.result(timeout=8) for f in futures]
        assert sum(isinstance(x, dict) for x in result) == 1
        assert result.count("REVISION_CONFLICT") == 1
        with e.db.sessions.begin() as s:
            g = s.get(Garment, target)
            assert g.revision == 2
            assert s.scalar(select(func.count()).select_from(AuditEvent)) == 1
            assert s.get(ServiceState, OWNER).catalog_revision == 1
    finally:
        db2.close()


def test_w_idem_1_serial_parallel_restart_and_lost_response(operator_env):
    from wardrobe.operator_contracts import OperatorUpdate
    from wardrobe.operator_service import OperatorService

    e = operator_env
    p = OperatorUpdate(base_revision=1, patch={"name": "one change"})
    original = e.app.state.operator.update(e.principal, uid(10), p, "synthetic-replay")
    # The first response is deliberately ignored by the client; reconstruct service.
    rebuilt = OperatorService(e.db, e.key, e.app.state.assets)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(
                lambda _: rebuilt.update(e.principal, uid(10), p, "synthetic-replay"),
                range(2),
            )
        )
    assert values == [original, original]
    state = snapshot(e.db)
    assert (
        state["garment"]["revision"] == 2
        and state["audit_events"] == state["idempotency"] == 1
    )
    for target, patch in ((10, {"name": "different"}), (11, {"name": "one change"})):
        assert edit(e, patch, target=target, key="synthetic-replay").status_code == 409
    assert snapshot(e.db) == state


def test_w_idem_2_headers_null_and_action_namespaces(operator_env):
    e = operator_env
    path = f"/v1/operator/garments/{uid(10)}"
    body = {"base_revision": 1, "patch": {"name": "x"}}
    for h in (
        headers(key=None),
        headers(key=""),
        headers(key="x" * 129),
        [*headers().items(), ("Idempotency-Key", "other")],
    ):
        assert e.client.patch(path, headers=h, json=body).status_code == 422
    saved = edit(e, {"name": "x"}, key="same-namespace-key")
    assert saved.status_code == 200
    assert (
        edit(e, {"name": "x", "category": None}, key="same-namespace-key").status_code
        == 409
    )
    assert action(e, "delete", revision=2, key="same-namespace-key").status_code == 200
    assert action(e, "restore", revision=3, key="same-namespace-key").status_code == 200
    with e.db.sessions.begin() as s:
        assert set(s.scalars(select(Idempotency.action))) == {
            "operator_update_garment",
            "operator_delete_garment",
            "operator_restore_garment",
        }


@pytest.mark.parametrize("fault", ["audit", "idempotency", "commit"])
def test_w_atom_1_rollback_and_retry(operator_env, monkeypatch, fault):
    from sqlalchemy.orm import Session

    e = operator_env
    before = snapshot(e.db)
    if fault == "commit":

        def fail_commit(session):
            if any(isinstance(x, Idempotency) for x in session.new):
                raise OperationalError("synthetic", {}, Exception("synthetic failure"))

        hook, target = "before_commit", Session
    else:

        def fail_commit(
            _connection, _cursor, statement, _parameters, _context, _executemany
        ):
            table = "audit_events" if fault == "audit" else "idempotency"
            if statement.lower().startswith("insert into " + table):
                raise OperationalError("synthetic", {}, Exception("synthetic failure"))

        hook, target = "before_cursor_execute", e.db.engine
    event.listen(target, hook, fail_commit)
    try:
        r = edit(e, {"name": "must-rollback"}, key="synthetic-fault")
        assert r.status_code == 503
    finally:
        event.remove(target, hook, fail_commit)
    assert snapshot(e.db) == before
    assert edit(e, {"name": "must-rollback"}, key="synthetic-fault").status_code == 200


def test_w_atom_1_busy_database_error(operator_env, monkeypatch):
    e = operator_env
    before = snapshot(e.db)

    @contextmanager
    def busy():
        raise OperationalError("synthetic busy", {}, Exception("database is locked"))
        yield

    monkeypatch.setattr(e.db, "write", busy)
    assert edit(e, {"name": "not committed"}).status_code == 503
    assert snapshot(e.db) == before


def test_w_del_1_roundtrip_receipts_and_visibility(operator_env):
    e = operator_env
    deleted = action(e, "delete")
    assert deleted.status_code == 200, deleted.text
    d = deleted.json()["data"]
    assert (d["state"], d["deleted_at"], d["restore_until"], d["revision"]) == (
        "deleted",
        T,
        T + 2592000,
        2,
    )
    assert (
        e.client.get(f"/v1/garments/{uid(10)}", headers=headers(READ_TOKEN)).status_code
        == 404
    )
    assert uid(10) not in [
        x["id"]
        for x in e.client.get("/v1/operator/garments", headers=headers()).json()[
            "data"
        ]["items"]
    ]
    trash = e.client.get(
        "/v1/operator/garments?state=deleted", headers=headers()
    ).json()["data"]["items"]
    assert uid(10) in [x["id"] for x in trash]
    restored = action(e, "restore", revision=2)
    assert restored.status_code == 200, restored.text
    assert (
        restored.json()["data"]["state"],
        restored.json()["data"]["deleted_at"],
        restored.json()["data"]["restore_until"],
    ) == ("active", None, None)
    with e.db.sessions.begin() as s:
        assert s.get(Garment, uid(10)).revision == 3
        assert list(
            s.scalars(
                select(AuditEvent.action).order_by(
                    AuditEvent.created_at, AuditEvent.action
                )
            )
        ) == ["operator_delete_garment", "operator_restore_garment"]
    before = snapshot(e.db)
    assert action(e, "restore", revision=2).json() == restored.json()
    assert snapshot(e.db) == before


@pytest.mark.parametrize(
    "target,expected",
    [
        (21, "RESTORE_UNAVAILABLE"),
        (22, "RESTORE_UNAVAILABLE"),
        (23, "RESTORE_UNAVAILABLE"),
        (10, "RESTORE_UNAVAILABLE"),
    ],
)
def test_w_del_2_invalid_window_and_state(operator_env, target, expected):
    e = operator_env
    before = snapshot(e.db, uid(target))
    r = action(e, "restore", target=target)
    assert r.status_code == 409 and r.json()["error"]["code"] == expected
    assert snapshot(e.db, uid(target)) == before


@pytest.mark.parametrize("missing", ["association", "file"])
def test_w_del_2_missing_images(operator_env, missing):
    e = operator_env
    if missing == "association":
        with e.db.write() as s:
            s.delete(s.get(GarmentAsset, (uid(20), uid(500))))
    else:
        e.image_path.unlink()  # only temporary generated image
    before = snapshot(e.db, uid(20))
    r = action(e, "restore", target=20)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESTORE_UNAVAILABLE"
    assert snapshot(e.db, uid(20)) == before


def test_w_del_2_repeated_delete_does_not_extend_window_and_revision_first(
    operator_env,
):
    e = operator_env
    assert action(e, "delete").status_code == 200
    before = snapshot(e.db)
    assert (
        action(e, "delete", revision=1, key="new-delete").json()["error"]["code"]
        == "REVISION_CONFLICT"
    )
    assert (
        action(e, "delete", revision=2, key="new-delete").json()["error"]["code"]
        == "TARGET_REMOVED"
    )
    assert (
        edit(e, {"name": "removed"}, revision=2).json()["error"]["code"]
        == "TARGET_REMOVED"
    )
    assert snapshot(e.db) == before


def test_w_del_3_images_shared_asset_and_source_unchanged(operator_env):
    e = operator_env
    path = f"/v1/operator/garments/{uid(10)}/assets/{uid(500)}/content"
    other = f"/v1/operator/garments/{uid(11)}/assets/{uid(500)}/content"
    assert e.client.get(path, headers=headers()).content == e.image_bytes
    before = snapshot(e.db)["garment"]
    with e.db.sessions.begin() as s:
        associations = list(
            s.execute(select(GarmentAsset.garment_id, GarmentAsset.asset_id))
        )
    assert action(e, "delete").status_code == 200
    assert e.client.get(path, headers=headers()).status_code == 404
    assert e.client.get(other, headers=headers()).content == e.image_bytes
    assert e.image_path.read_bytes() == e.image_bytes
    with e.db.sessions.begin() as s:
        assert (
            list(s.execute(select(GarmentAsset.garment_id, GarmentAsset.asset_id)))
            == associations
        )
        g = s.get(Garment, uid(10))
        assert (g.identity_fields, g.product_reference) == (
            before["identity_fields"],
            before["product_reference"],
        )
    assert action(e, "restore", revision=2).status_code == 200
    assert e.client.get(path, headers=headers()).content == e.image_bytes
    assert (
        e.client.get(
            f"/v1/operator/garments/{uid(12)}/assets/{uid(500)}/content",
            headers=headers(),
        ).status_code
        == 404
    )


def test_w_del_3_preview_rechecks_after_generation(operator_env, monkeypatch):
    from wardrobe.image_previews import PreviewStore

    e = operator_env
    temporary = e.image_path.parent / "synthetic-preview.jpg"
    temporary.write_bytes(b"synthetic-preview")

    def generated(_self, *args, **kwargs):
        with e.db.write() as s:
            s.get(Garment, uid(10)).state = "deleted"
        return temporary

    monkeypatch.setattr(PreviewStore, "get", generated)
    r = e.client.get(
        f"/v1/operator/garments/{uid(10)}/assets/{uid(500)}/preview", headers=headers()
    )
    assert r.status_code == 404
    assert r.content != b"synthetic-preview"


def test_w_del_4_no_permanent_delete_capability(operator_env):
    e = operator_env
    for method, path in [
        ("DELETE", f"/v1/operator/garments/{uid(10)}"),
        ("POST", f"/v1/operator/garments/{uid(10)}/purge"),
        ("POST", f"/v1/operator/garments/{uid(10)}/permanent-delete"),
    ]:
        assert e.client.request(
            method, path, headers=headers(), json={"base_revision": 1}
        ).status_code in (404, 405)
    e.clock[0] = T + 2592001
    assert e.image_path.read_bytes() == e.image_bytes
    with e.db.sessions.begin() as s:
        assert s.get(Garment, uid(22)) is not None


def test_w_del_3_preview_rechecks_grant_revocation(operator_env, monkeypatch):
    from operator_support import TOKEN

    from wardrobe.auth import token_hash
    from wardrobe.image_previews import PreviewStore
    from wardrobe.models import ClientGrant

    e = operator_env
    temporary = e.image_path.parent / "synthetic-revoked-preview.jpg"
    temporary.write_bytes(b"synthetic-preview")

    def generated(_self, *args, **kwargs):
        with e.db.write() as s:
            s.get(ClientGrant, token_hash(TOKEN)).revoked_at = T
        return temporary

    monkeypatch.setattr(PreviewStore, "get", generated)
    r = e.client.get(
        f"/v1/operator/garments/{uid(10)}/assets/{uid(500)}/preview", headers=headers()
    )
    assert r.status_code == 401
    assert r.content != b"synthetic-preview"
