"""Local operator policies renew existing identities; no network renewal surface."""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

POLICY_UID = 0
WINDOW = 604800
THRESHOLD = 259200


class RenewalError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Policy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1] = 1
    policy_id: str
    enabled: bool = True
    database: str
    db_device: int
    db_inode: int
    grant_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_id: str
    client_id: str
    audience: str
    channel: Literal["agent", "human_adapter"]
    scopes: list[str]
    created_at: int
    window: Literal[604800] = WINDOW
    threshold: Literal[259200] = THRESHOLD


class Witness(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    policy_digest: str
    minimum_expires_at: int = Field(ge=0)
    last_checked: int = Field(ge=0)
    last_renewed: int | None = None


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise RenewalError("invalid_json")
        data[key] = value
    return data


def _read(path, uid, modes):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) not in modes
                or info.st_size > 8192
            ):
                raise RenewalError("private_file_refused")
            return json.loads(stream.read(8193), object_pairs_hook=_pairs)
    except (OSError, ValueError):
        raise RenewalError("configuration_unavailable") from None


def _atomic(path, value, *, uid=None, gid=None, mode=0o600, exclusive=False):
    fd, temporary = tempfile.mkstemp(prefix=".renewal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(policy):
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()


def _instance(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise RenewalError("database_instance_changed")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RenewalError("database_instance_changed")
    return info


def _state_paths(policy):
    identifier = str(UUID(policy.policy_id))
    directory = Path(policy.database).parent / "managed-grants"
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _instance(Path(policy.database)).st_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RenewalError("witness_directory_refused")
    return directory / (identifier + ".json"), directory / (identifier + ".lock")


def _binding(row, policy, now):
    if policy.channel == "human_adapter" and policy.scopes != ["human.intake_create"]:
        raise RenewalError("binding_changed")
    if row is None:
        raise RenewalError("grant_missing")
    if row["revoked_at"] is not None:
        raise RenewalError("grant_revoked")
    if row["expires_at"] <= now:
        raise RenewalError("grant_expired")
    if row["expires_at"] > now + WINDOW + 60:
        raise RenewalError("expiry_outside_window")
    if row["created_at"] > now:
        raise RenewalError("clock_invalid")
    if row["renewed_at"] is not None and row["renewed_at"] > now:
        raise RenewalError("clock_invalid")
    for field in ("owner_id", "client_id", "audience", "channel", "created_at"):
        if row[field] != getattr(policy, field):
            raise RenewalError("binding_changed")
    if sorted(json.loads(row["scopes"])) != policy.scopes:
        raise RenewalError("binding_changed")


def enroll(database, token_json, policy_path, *, owner_id, client_id, now=None):
    if os.geteuid() != POLICY_UID:
        raise RenewalError("operator_required")
    now = int(time.time()) if now is None else now
    database, policy_path = Path(database).absolute(), Path(policy_path).absolute()
    info = _instance(database)
    if policy_path.exists() or policy_path.is_symlink():
        raise RenewalError("policy_exists")
    parent = policy_path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != POLICY_UID
        or parent.st_mode & 0o022
    ):
        raise RenewalError("policy_directory_refused")
    token_info = Path(token_json).lstat()
    token = _read(token_json, token_info.st_uid, {0o600, 0o400})
    if (
        not isinstance(token, dict)
        or set(token) != {"token"}
        or not isinstance(token["token"], str)
        or not 32 <= len(token["token"]) <= 512
    ):
        raise RenewalError("credential_refused")
    hashed = hashlib.sha256(token["token"].encode()).hexdigest()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM client_grants WHERE token_hash=?", (hashed,)
        ).fetchone()
    if row is None:
        raise RenewalError("grant_missing")
    policy = Policy(
        policy_id=str(uuid4()),
        database=str(database),
        db_device=info.st_dev,
        db_inode=info.st_ino,
        grant_hash=hashed,
        owner_id=str(UUID(owner_id)),
        client_id=client_id,
        audience=row["audience"],
        channel=row["channel"],
        scopes=sorted(json.loads(row["scopes"])),
        created_at=row["created_at"],
    )
    _binding(row, policy, now)
    directory = database.parent / "managed-grants"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise RenewalError("witness_directory_refused")
    if directory.stat().st_uid != info.st_uid:
        os.chown(directory, info.st_uid, info.st_gid)
    directory.chmod(0o700)
    state, _ = _state_paths(policy)
    witness = Witness(
        policy_digest=_digest(policy),
        minimum_expires_at=row["expires_at"],
        last_checked=now,
    )
    _atomic(state, witness.model_dump(), uid=info.st_uid, gid=info.st_gid)
    _atomic(
        policy_path,
        policy.model_dump(),
        uid=POLICY_UID,
        gid=info.st_gid,
        mode=0o640,
        exclusive=True,
    )
    return {"status": "enrolled", "expires_at": row["expires_at"]}


def renew(
    policy_path, *, expected_database, force_active=False, check_only=False, now=None
):
    now = int(time.time()) if now is None else now
    try:
        parent = Path(policy_path).parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != POLICY_UID
            or parent.st_mode & 0o022
        ):
            raise RenewalError("policy_directory_refused")
        policy = Policy.model_validate(_read(policy_path, POLICY_UID, {0o600, 0o640}))
        if not policy.enabled:
            raise RenewalError("policy_disabled")
        database = Path(policy.database)
        if database != Path(expected_database).absolute():
            raise RenewalError("database_instance_changed")
        info = _instance(database)
        policy_info = Path(policy_path).stat()
        if policy_info.st_gid != info.st_gid:
            raise RenewalError("private_file_refused")
        if (info.st_dev, info.st_ino) != (policy.db_device, policy.db_inode):
            raise RenewalError("database_instance_changed")
        if os.geteuid() != info.st_uid:
            raise RenewalError("service_identity_required")
        state, lock = _state_paths(policy)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as guard:
            lock_info = os.fstat(guard.fileno())
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != info.st_uid
                or stat.S_IMODE(lock_info.st_mode) != 0o600
            ):
                raise RenewalError("private_file_refused")
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            witness = Witness.model_validate(_read(state, info.st_uid, {0o600}))
            if witness.policy_digest != _digest(policy):
                raise RenewalError("continuity_lost")
            if now < witness.last_checked - 60:
                raise RenewalError("clock_invalid")
            with sqlite3.connect(
                database.as_uri() + "?mode=rw", uri=True, timeout=2
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM client_grants WHERE token_hash=?",
                    (policy.grant_hash,),
                ).fetchone()
                _binding(row, policy, now)
                if row["expires_at"] < witness.minimum_expires_at:
                    raise RenewalError("continuity_lost")
                expires = row["expires_at"]
                if check_only:
                    return {
                        "status": "due" if expires - now <= THRESHOLD else "valid",
                        "expires_at": expires,
                        "last_checked": witness.last_checked,
                        "last_renewed": witness.last_renewed,
                    }
                changed = False
                if (
                    force_active or expires - now <= THRESHOLD
                ) and now + WINDOW > expires:
                    expires = now + WINDOW
                    connection.execute(
                        "UPDATE client_grants SET expires_at=?,renewed_at=? WHERE token_hash=?",
                        (expires, now, policy.grant_hash),
                    )
                    connection.execute(
                        "INSERT INTO audit_events (id,owner_id,client_id,action,target_id,receipt_id,created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            str(uuid4()),
                            policy.owner_id,
                            policy.client_id,
                            "credential_renewed",
                            policy.policy_id,
                            str(uuid4()),
                            now,
                        ),
                    )
                    changed = True
            after = _instance(database)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise RenewalError("database_instance_changed")
            witness.minimum_expires_at = expires
            witness.last_checked = max(now, witness.last_checked)
            witness.last_renewed = now if changed else row["renewed_at"]
            try:
                _atomic(state, witness.model_dump())
            except OSError:
                raise RenewalError("state_io_error_outcome_unverified") from None
            return {
                "status": "renewed" if changed else "unchanged",
                "expires_at": expires,
                "checked_at": now,
            }
    except (ValidationError, ValueError, OSError, sqlite3.Error):
        raise RenewalError("configuration_or_database_unavailable") from None
