import hashlib
import json

import pytest
from PIL import Image

from wardrobe.manifest import freeze_manifest


def test_manifest_preserves_paths_duplicates_and_source_bytes(tmp_path):
    source = tmp_path / "source"
    (source / "shirts").mkdir(parents=True)
    (source / "coats").mkdir()
    one = source / "shirts" / "same.png"
    Image.new("RGB", (4, 4), "red").save(one)
    (source / "coats" / "same.png").write_bytes(one.read_bytes())
    (source / "bad.jpg").write_bytes(b"corrupt")
    (source / ".pending.PNG.icloud").write_bytes(b"synthetic placeholder")
    (source / "link.jpg").symlink_to(one)
    before = hashlib.sha256(one.read_bytes()).hexdigest(), one.stat().st_mtime_ns
    output = tmp_path / "manifest.json"
    summary = freeze_manifest(source, output)
    assert summary["source_files"] == 5
    assert summary["readable"] == 2 and summary["unresolved"] == 3
    assert summary["unique_readable_contents"] == 1
    assert summary["duplicate_content_mappings"] == 1
    data = json.loads(output.read_text())
    assert not data["uploaded"] and not data["published"]
    assert {r["relative_path"] for r in data["records"]} >= {
        "shirts/same.png",
        "coats/same.png",
    }
    assert (
        hashlib.sha256(one.read_bytes()).hexdigest(),
        one.stat().st_mtime_ns,
    ) == before
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="read-only"):
        freeze_manifest(source, source / "manifest.json")
    with pytest.raises(FileExistsError):
        freeze_manifest(source, output)
