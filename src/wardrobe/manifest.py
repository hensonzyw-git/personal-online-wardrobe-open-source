"""Read-only source census. This module never uploads or creates garments."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from .assets import MAX_BYTES, MAX_PIXELS
from .models import Asset


def private_json(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def freeze_manifest(source: Path, output: Path):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a real directory")
    root = source.resolve()
    if output.resolve().is_relative_to(root):
        raise ValueError("Manifest output must be outside the read-only source")
    records = []
    for path in sorted(root.rglob("*")):
        name = path.name.lower()
        placeholder = name.endswith(".icloud") and any(
            name.endswith(suffix + ".icloud") for suffix in (".png", ".jpg", ".jpeg")
        )
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"} and not placeholder:
            continue
        record = {
            "source_id": str(uuid4()),
            "relative_path": path.relative_to(root).as_posix(),
            "state": "pending",
            "sha256": None,
            "size": None,
        }
        records.append(record)
        if placeholder or path.is_symlink() or not path.is_file():
            record["state"] = (
                "icloud_placeholder" if placeholder else "unsupported_source"
            )
            continue
        try:
            before = path.stat()
            record["size"] = before.st_size
            if not 0 < before.st_size <= MAX_BYTES:
                record["state"] = "size_limit"
                continue
            with path.open("rb") as file:
                record["sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
            with Image.open(path, formats=["PNG", "JPEG"]) as image:
                if image.width * image.height > MAX_PIXELS:
                    record["state"] = "pixel_limit"
                    continue
                record.update(
                    width=image.width,
                    height=image.height,
                    mime="image/png" if image.format == "PNG" else "image/jpeg",
                )
                image.verify()
            with Image.open(path, formats=["PNG", "JPEG"]) as image:
                image.load()
            after = path.stat()
            record["state"] = (
                "readable"
                if (before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns)
                else "source_changed"
            )
        except (
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            record["state"] = "unreadable_or_invalid_image"
    hashes = [r["sha256"] for r in records if r["state"] == "readable"]
    summary = {
        "source_files": len(records),
        "readable": len(hashes),
        "unresolved": len(records) - len(hashes),
        "unique_readable_contents": len(set(hashes)),
        "duplicate_content_mappings": len(hashes) - len(set(hashes)),
        "readable_bytes": sum(r["size"] for r in records if r["state"] == "readable"),
    }
    value = {
        "schema_version": "wardrobe-manifest/1",
        "manifest_id": str(uuid4()),
        "summary": summary,
        "records": records,
        "uploaded": False,
        "published": False,
    }
    private_json(output, value)
    return summary


def reconcile(database, root):
    """Read-only integrity report; no orphan/temp deletion or DB repair."""
    originals = root / "assets" / "original"
    results = []
    with database.sessions.begin() as session:
        assets = list(session.scalars(select(Asset)))
        expected = {asset.sha256 for asset in assets}
        for asset in assets:
            from .models import IntakeSourceObject

            draft = session.scalar(
                select(IntakeSourceObject).where(
                    IntakeSourceObject.asset_id == asset.id,
                    IntakeSourceObject.state.in_(["draft", "collected"]),
                )
            )
            path = originals / asset.sha256
            if not path.exists() and draft is not None:
                results.append({"asset_id": asset.id, "state": "intake_" + draft.state})
                continue
            state = "missing"
            if path.is_file():
                with path.open("rb") as file:
                    state = (
                        "intact"
                        if path.stat().st_size == asset.size
                        and hashlib.file_digest(file, "sha256").hexdigest()
                        == asset.sha256
                        else "corrupt"
                    )
            results.append({"asset_id": asset.id, "state": state})
    from .intake_backup import SCHEMA, submitted_entries, verify

    try:
        entries = submitted_entries(database)
    except ValueError:
        # A broken intake inventory must not hide the legacy asset report.
        # Keep build() fail-closed; this is a diagnostic, never a backup manifest.
        submitted = {
            "schema_version": SCHEMA,
            "entries": [],
            "ok": False,
            "checked": 0,
            "error": "SUBMITTED_INTAKE_METADATA_INVALID",
            "repaired": False,
        }
    else:
        submitted = verify(root, {"schema_version": SCHEMA, "entries": entries})
    return {
        "schema_version": "wardrobe-reconciliation/1",
        "assets": results,
        "submitted_intake_media": submitted,
        "orphan_contents": sorted(
            p.name
            for p in originals.iterdir()
            if p.is_file() and p.name not in expected
        )
        if originals.exists()
        else [],
        "temporary_files": sum(p.is_file() for p in (root / "tmp").iterdir())
        if (root / "tmp").exists()
        else 0,
        "repaired": False,
    }
