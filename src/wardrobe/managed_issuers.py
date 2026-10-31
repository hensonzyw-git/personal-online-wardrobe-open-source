"""Operator-enrolled exact intake issuer renewal; never revive or widen access."""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from . import managed_grants as g


class Policy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: int = 1
    policy_id: str
    enabled: bool = True
    database: str
    db_device: int
    db_inode: int
    issuer: str
    kid: str
    public_key_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    bindings: list[dict[str, str]]
    audience: str


def _digest(policy):
    return hashlib.sha256(
        json.dumps(policy.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def binding(row, policy, now):
    if row is None:
        raise g.RenewalError("issuer_missing")
    if row["revoked_at"] is not None:
        raise g.RenewalError("issuer_revoked")
    if not now < row["expires_at"] <= now + g.WINDOW + 60:
        raise g.RenewalError("issuer_expired_or_invalid")
    if (
        json.loads(row["purposes"]) != ["intake_create"]
        or json.loads(row["bindings"]) != policy.bindings
        or row["audience"] != policy.audience
        or hashlib.sha256(row["public_key"].encode()).hexdigest()
        != policy.public_key_hash
    ):
        raise g.RenewalError("binding_changed")


def enroll(database, policy_path, *, issuer, kid, now=None):
    if os.geteuid() != g.POLICY_UID:
        raise g.RenewalError("operator_required")
    now = int(time.time()) if now is None else now
    database, policy_path = Path(database).absolute(), Path(policy_path).absolute()
    info = g._instance(database)
    parent = policy_path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != g.POLICY_UID
        or parent.st_mode & 0o022
    ):
        raise g.RenewalError("policy_directory_refused")
    if policy_path.exists() or policy_path.is_symlink():
        raise g.RenewalError("policy_exists")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM trusted_issuers WHERE issuer=? AND kid=?", (issuer, kid)
        ).fetchone()
    if row is None:
        raise g.RenewalError("issuer_missing")
    bindings = json.loads(row["bindings"])
    if len(bindings) != 1 or set(bindings[0]) != {"sub", "owner_id", "client_id"}:
        raise g.RenewalError("binding_changed")
    policy = Policy(
        policy_id=str(uuid4()),
        database=str(database),
        db_device=info.st_dev,
        db_inode=info.st_ino,
        issuer=issuer,
        kid=kid,
        public_key_hash=hashlib.sha256(row["public_key"].encode()).hexdigest(),
        bindings=bindings,
        audience=row["audience"],
    )
    binding(row, policy, now)
    directory = database.parent / "managed-grants"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise g.RenewalError("witness_directory_refused")
    if directory.stat().st_uid != info.st_uid:
        os.chown(directory, info.st_uid, info.st_gid)
    directory.chmod(0o700)
    state, _ = g._state_paths(policy)
    witness = g.Witness(
        policy_digest=_digest(policy),
        minimum_expires_at=row["expires_at"],
        last_checked=now,
    )
    g._atomic(state, witness.model_dump(), uid=info.st_uid, gid=info.st_gid)
    g._atomic(
        policy_path,
        policy.model_dump(),
        uid=g.POLICY_UID,
        gid=info.st_gid,
        mode=0o640,
        exclusive=True,
    )
    return {"status": "enrolled", "expires_at": row["expires_at"]}


def renew(policy_path, *, now=None, check_only=False, expected_database=None):
    now = int(time.time()) if now is None else now
    try:
        policy_path = Path(policy_path)
        parent = policy_path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != g.POLICY_UID
            or parent.st_mode & 0o022
        ):
            raise g.RenewalError("policy_directory_refused")
        policy = Policy.model_validate(
            g._read(policy_path, g.POLICY_UID, {0o600, 0o640})
        )
        if policy.version != 1 or not policy.enabled:
            raise g.RenewalError("policy_disabled")
        database = Path(policy.database)
        info = g._instance(database)
        if (
            expected_database is not None
            and database != Path(expected_database).absolute()
        ):
            raise g.RenewalError("database_instance_changed")
        if (info.st_dev, info.st_ino) != (policy.db_device, policy.db_inode):
            raise g.RenewalError("database_instance_changed")
        if os.geteuid() != info.st_uid or policy_path.stat().st_gid != info.st_gid:
            raise g.RenewalError("service_identity_required")
        state, lock = g._state_paths(policy)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as guard:
            li = os.fstat(guard.fileno())
            if (
                not stat.S_ISREG(li.st_mode)
                or li.st_uid != info.st_uid
                or stat.S_IMODE(li.st_mode) != 0o600
            ):
                raise g.RenewalError("private_file_refused")
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            witness = g.Witness.model_validate(g._read(state, info.st_uid, {0o600}))
            if (
                witness.policy_digest != _digest(policy)
                or now < witness.last_checked - 60
            ):
                raise g.RenewalError("continuity_lost")
            with sqlite3.connect(
                database.as_uri() + "?mode=rw", uri=True, timeout=2
            ) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM trusted_issuers WHERE issuer=? AND kid=?",
                    (policy.issuer, policy.kid),
                ).fetchone()
                binding(row, policy, now)
                expires = row["expires_at"]
                if expires < witness.minimum_expires_at:
                    raise g.RenewalError("continuity_lost")
                if check_only:
                    return {
                        "status": "due" if expires - now <= g.THRESHOLD else "valid",
                        "expires_at": expires,
                    }
                changed = expires - now <= g.THRESHOLD and now + g.WINDOW > expires
                if changed:
                    expires = now + g.WINDOW
                    db.execute(
                        "UPDATE trusted_issuers SET expires_at=? WHERE issuer=? AND kid=?",
                        (expires, policy.issuer, policy.kid),
                    )
                    b = policy.bindings[0]
                    db.execute(
                        "INSERT INTO audit_events (id,owner_id,client_id,action,target_id,receipt_id,created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            str(uuid4()),
                            b["owner_id"],
                            b["client_id"],
                            "intake_issuer_renewed",
                            policy.policy_id,
                            str(uuid4()),
                            now,
                        ),
                    )
            after = g._instance(database)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise g.RenewalError("database_instance_changed")
            witness.minimum_expires_at = expires
            witness.last_checked = max(now, witness.last_checked)
            if changed:
                witness.last_renewed = now
            try:
                g._atomic(state, witness.model_dump())
            except OSError:
                raise g.RenewalError("state_io_error_outcome_unverified") from None
            return {
                "status": "renewed" if changed else "unchanged",
                "expires_at": expires,
            }
    except (ValueError, OSError, sqlite3.Error):
        raise g.RenewalError("configuration_or_database_unavailable") from None
