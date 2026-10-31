"""Read-only submitted intake backup inventory; never include pending sources."""

import hashlib
import os
import re
import stat
from pathlib import Path

from sqlalchemy import select

from .models import Asset, IntakeSourceObject, IntakeUpload

SCHEMA = "wardrobe-intake-backup/1"


def _check(root, entry):
    relative = entry["relative_path"]
    sha = entry["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", sha) or relative not in {
        "assets/original/" + sha,
        "intake/objects/" + sha,
    }:
        raise ValueError("Invalid intake backup entry")
    path = Path(root)
    if path.is_symlink():
        return "unsafe"
    for component in Path(relative).parts:
        path = path / component
        if path.is_symlink():
            return "unsafe"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return "unsafe"
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return "changed"
        return (
            "intact" if actual == sha and after.st_size == entry["size"] else "corrupt"
        )
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def submitted_entries(database):
    """Call on a consistent database snapshot paired with the copied media tree."""
    entries = {}
    with database.sessions.begin() as session:
        for source in session.scalars(
            select(IntakeSourceObject).where(IntakeSourceObject.state == "submitted")
        ):
            asset = session.get(Asset, source.asset_id)
            view_size = session.scalar(
                select(IntakeUpload.size)
                .where(
                    IntakeUpload.task_id == source.task_id,
                    IntakeUpload.kind == "view",
                    IntakeUpload.sha256 == source.view_sha256,
                    IntakeUpload.state == "ready",
                )
                .limit(1)
            )
            if asset is None or view_size is None:
                raise ValueError("Submitted intake media metadata is incomplete")
            media = [
                ("assets/original/" + source.sha256, source.sha256, asset.size),
                ("intake/objects/" + source.view_sha256, source.view_sha256, view_size),
            ]
            media.extend(
                ("intake/objects/" + item["sha256"], item["sha256"], item["size"])
                for item in source.report.get("outputs", {}).values()
            )
            for relative, sha, size in media:
                entry = {"relative_path": relative, "sha256": sha, "size": size}
                if relative in entries and entries[relative] != entry:
                    raise ValueError("Conflicting intake media metadata")
                entries[relative] = entry
    return [entries[path] for path in sorted(entries)]


def verify(root, manifest):
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "entries"}
        or manifest["schema_version"] != SCHEMA
        or not isinstance(manifest["entries"], list)
    ):
        raise ValueError("Invalid intake backup manifest")
    results, seen = [], set()
    for entry in manifest["entries"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"relative_path", "sha256", "size"}
            or not isinstance(entry["relative_path"], str)
            or not isinstance(entry["sha256"], str)
            or type(entry["size"]) is not int
            or entry["size"] <= 0
            or entry["relative_path"] in seen
        ):
            raise ValueError("Invalid intake backup entry")
        seen.add(entry["relative_path"])
        results.append({**entry, "state": _check(root, entry)})
    return {
        "schema_version": SCHEMA,
        "entries": results,
        "ok": all(x["state"] == "intact" for x in results),
        "checked": len(results),
        "repaired": False,
    }


def build(database, root):
    manifest = {"schema_version": SCHEMA, "entries": submitted_entries(database)}
    if not verify(root, manifest)["ok"]:
        raise ValueError("Submitted intake media failed integrity verification")
    return manifest
