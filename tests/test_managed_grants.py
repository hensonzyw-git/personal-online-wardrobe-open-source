import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import OWNER_A, OWNER_B, TOKEN_A

from wardrobe import managed_grants as managed


@pytest.fixture
def registered(store, tmp_path, monkeypatch):
    db, _, _ = store
    now = int(time.time())
    digest = hashlib.sha256(TOKEN_A.encode()).hexdigest()
    with sqlite3.connect(db.path) as c:
        c.execute(
            "UPDATE client_grants SET created_at=?,expires_at=? WHERE token_hash=?",
            (now - 10, now + 86400, digest),
        )
    monkeypatch.setattr(managed, "POLICY_UID", os.geteuid())
    parent = tmp_path / "policy"
    parent.mkdir(mode=0o700)
    policy = parent / "pa.json"
    token = tmp_path / "private.json"
    token.write_text(json.dumps({"token": TOKEN_A}))
    token.chmod(0o600)
    managed.enroll(
        db.path, token, policy, owner_id=OWNER_A, client_id="client-a", now=now
    )
    return db, policy, now, digest


def read(registered):
    db, _, _, digest = registered
    with sqlite3.connect(db.path) as c:
        c.row_factory = sqlite3.Row
        return dict(
            c.execute(
                "SELECT * FROM client_grants WHERE token_hash=?", (digest,)
            ).fetchone()
        )


def audit_count(db):
    with sqlite3.connect(db.path) as c:
        return c.execute(
            "SELECT count(*) FROM audit_events WHERE action='credential_renewed'"
        ).fetchone()[0]


def test_renew_keeps_identity_and_token_repeats_without_duplicate_audit(registered):
    db, policy, now, _ = registered
    before = read(registered)
    result = managed.renew(policy, expected_database=db.path, now=now)
    assert result == {
        "status": "renewed",
        "expires_at": now + managed.WINDOW,
        "checked_at": now,
    }
    after = read(registered)
    assert {
        k: v for k, v in before.items() if k not in {"expires_at", "renewed_at"}
    } == {k: v for k, v in after.items() if k not in {"expires_at", "renewed_at"}}
    assert (
        managed.renew(policy, expected_database=db.path, now=now)["status"]
        == "unchanged"
    )
    assert audit_count(db) == 1
    from wardrobe.auth import Authenticator

    assert (
        Authenticator(db.sessions, "open-wardrobe", now=lambda: now)
        .authenticate(TOKEN_A)
        .owner_id
        == OWNER_A
    )
    assert TOKEN_A not in policy.read_text()


def test_status_is_read_only_and_force_is_bounded(registered):
    db, policy, now, _ = registered
    checked = managed.renew(policy, expected_database=db.path, now=now, check_only=True)
    assert checked["status"] == "due" and audit_count(db) == 0
    managed.renew(policy, expected_database=db.path, now=now)
    assert (
        managed.renew(policy, expected_database=db.path, now=now + 1)["status"]
        == "unchanged"
    )
    forced = managed.renew(
        policy, expected_database=db.path, now=now + 2, force_active=True
    )
    assert forced["expires_at"] == now + 2 + managed.WINDOW
    assert audit_count(db) == 2


def test_renews_across_initial_seven_days_without_changing_creation(registered):
    db, policy, now, _ = registered
    created = read(registered)["created_at"]
    for day in (0, 4, 8, 12):
        result = managed.renew(policy, expected_database=db.path, now=now + day * 86400)
        assert result["status"] == "renewed"
        row = read(registered)
        assert row["created_at"] == created
        assert row["renewed_at"] == now + day * 86400
        assert row["expires_at"] == row["renewed_at"] + managed.WINDOW
    assert audit_count(db) == 4


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("revoked_at", 1, "grant_revoked"),
        ("expires_at", "expired", "grant_expired"),
        ("client_id", "different", "binding_changed"),
        ("owner_id", OWNER_B, "binding_changed"),
        ("audience", "wrong", "binding_changed"),
        ("scopes", '["wardrobe.read","wardrobe.create"]', "binding_changed"),
        ("created_at", "changed", "binding_changed"),
    ],
)
def test_changed_or_revoked_identity_is_never_recreated(
    registered, field, value, reason
):
    db, policy, now, digest = registered
    with sqlite3.connect(db.path) as c:
        if value == "expired":
            c.execute(
                "UPDATE client_grants SET created_at=?,expires_at=? WHERE token_hash=?",
                (now - 100, now - 1, digest),
            )
        else:
            if value == "changed":
                value = now - 20
            c.execute(
                f"UPDATE client_grants SET {field}=? WHERE token_hash=?",
                (value, digest),
            )
    with pytest.raises(managed.RenewalError, match=reason):
        managed.renew(policy, expected_database=db.path, now=now)
    assert audit_count(db) == 0


def test_deleted_grant_is_not_reissued(registered):
    db, policy, now, digest = registered
    with sqlite3.connect(db.path) as c:
        c.execute("DELETE FROM client_grants WHERE token_hash=?", (digest,))
    with pytest.raises(managed.RenewalError, match="grant_missing"):
        managed.renew(policy, expected_database=db.path, now=now)
    with sqlite3.connect(db.path) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM client_grants WHERE token_hash=?", (digest,)
            ).fetchone()[0]
            == 0
        )


def test_parallel_renewers_do_not_duplicate_audit(registered):
    db, policy, now, _ = registered
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: managed.renew(policy, expected_database=db.path, now=now),
                range(3),
            )
        )
    assert sum(x["status"] == "renewed" for x in results) == 1
    assert audit_count(db) == 1


def test_concurrent_revocation_remains_effective(registered):
    db, policy, now, digest = registered

    def revoke():
        with sqlite3.connect(db.path) as c:
            c.execute(
                "UPDATE client_grants SET revoked_at=? WHERE token_hash=?",
                (now, digest),
            )

    def renew():
        try:
            return managed.renew(policy, expected_database=db.path, now=now)
        except managed.RenewalError as error:
            assert error.code == "grant_revoked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(renew), pool.submit(revoke)
        a.result()
        b.result()
    assert read(registered)["revoked_at"] == now
    with pytest.raises(managed.RenewalError, match="grant_revoked"):
        managed.renew(policy, expected_database=db.path, now=now + 1, force_active=True)


def test_state_failure_after_commit_can_reconcile_without_second_renewal(
    registered, monkeypatch
):
    db, policy, now, _ = registered
    state, _ = managed._state_paths(
        managed.Policy.model_validate_json(policy.read_text())
    )
    original = managed._atomic

    def fail(path, *args, **kwargs):
        if path == state:
            raise OSError("synthetic disk error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(managed, "_atomic", fail)
    with pytest.raises(managed.RenewalError, match="outcome_unverified"):
        managed.renew(policy, expected_database=db.path, now=now)
    assert read(registered)["expires_at"] == now + managed.WINDOW
    monkeypatch.setattr(managed, "_atomic", original)
    assert (
        managed.renew(policy, expected_database=db.path, now=now + 1)["status"]
        == "unchanged"
    )
    assert audit_count(db) == 1
    assert json.loads(state.read_text())["minimum_expires_at"] == now + managed.WINDOW


@pytest.mark.parametrize(
    "corrupt",
    [
        "missing",
        "permissions",
        "link",
        "digest",
        "disabled",
        "clock",
        "expiry_rollback",
        "instance",
    ],
)
def test_continuity_and_private_file_failures_close(registered, corrupt):
    db, policy, now, digest = registered
    definition = managed.Policy.model_validate_json(policy.read_text())
    state, _ = managed._state_paths(definition)
    if corrupt == "missing":
        state.unlink()
    elif corrupt == "permissions":
        state.chmod(0o644)
    elif corrupt == "link":
        copied = state.with_name("old.json")
        state.rename(copied)
        state.symlink_to(copied)
    elif corrupt in {"digest", "disabled"}:
        value = json.loads(policy.read_text())
        value["client_id" if corrupt == "digest" else "enabled"] = (
            "other" if corrupt == "digest" else False
        )
        policy.write_text(json.dumps(value))
    elif corrupt == "clock":
        now -= 61
    elif corrupt == "expiry_rollback":
        with sqlite3.connect(db.path) as c:
            c.execute(
                "UPDATE client_grants SET expires_at=expires_at-1 WHERE token_hash=?",
                (digest,),
            )
    elif corrupt == "instance":
        value = json.loads(policy.read_text())
        value["db_inode"] += 1
        policy.write_text(json.dumps(value))
    with pytest.raises(managed.RenewalError):
        managed.renew(policy, expected_database=db.path, now=now)
    assert audit_count(db) == 0
