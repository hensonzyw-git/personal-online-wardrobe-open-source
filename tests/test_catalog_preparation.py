import csv
import hashlib
import json
from uuid import uuid4

import pytest
from PIL import Image, ImageCms

from deploy.prepare_catalog import COLUMNS, analysis_image, prepare
from wardrobe.labels import LabelValues
from wardrobe.manifest import freeze_manifest


def inputs(tmp_path):
    source = tmp_path / "source"
    category = source / "长袖衬衫"
    category.mkdir(parents=True)
    for name in ("one", "ambiguous", "unindexed"):
        Image.new("RGB", (20, 30), "blue").save(category / f"{name}.png")
    manifest = tmp_path / "manifest.json"
    freeze_manifest(source, manifest)
    value = json.loads(manifest.read_text())
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "wardrobe-original-transfer/1",
                "manifest_id": value["manifest_id"],
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "records": {
                    r["source_id"]: dict(r, state="ready", asset_id=str(uuid4()))
                    for r in value["records"]
                },
            }
        )
    )
    index = source / "衣橱索引.csv"
    with index.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=["品类", "文件名", *COLUMNS, "推荐次数", "最近推荐日期"]
        )
        writer.writeheader()
        for name, color in (
            ("one", "蓝色"),
            ("ambiguous", "蓝色"),
            ("ambiguous", "绿色"),
            ("missing", "白色"),
        ):
            writer.writerow(
                {
                    "品类": "长袖衬衫",
                    "文件名": name,
                    "颜色": color,
                    "材质": "棉",
                    "图案": "纯色",
                    "品牌": "杂牌",
                    "风格标签": "日系|休闲",
                    "廓形": "宽松",
                    "适合季节": "春秋",
                    "适合场合": "日常",
                    "推荐次数": "99",
                    "最近推荐日期": "2026-10-01",
                }
            )
    output = tmp_path / "private-draft"
    return source, manifest, receipt, index, output


def test_all_sources_accounted_for_without_trusting_legacy_or_publishing(tmp_path):
    args = inputs(tmp_path)
    source, _, _, index, output = args
    original = {
        p.relative_to(source): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in source.rglob("*")
        if p.is_file()
    }
    summary = prepare(*args)
    assert summary["source_files"] == 3 and summary["legacy_rows"] == 4
    assert summary["unique_index_matches"] == 1
    assert (
        summary["unindexed_images"]
        == summary["duplicate_index_keys"]
        == summary["index_keys_without_image"]
        == 1
    )
    assert summary["visually_evaluated"] == summary["published_garments"] == 0
    batch = json.loads((output / "batch.json").read_text())
    assert batch["published"] is False
    assert len(batch["derivatives"]) == len(batch["drafts"]) == 3
    normal = next(d for d in batch["drafts"] if d["display_name_hint"] == "one")
    fields = normal["annotation_fields"]
    assert set(fields) == set(LabelValues.model_fields)
    assert fields["material"]["value"] == "棉"
    assert fields["material"]["source_kind"] == "legacy_index"
    assert fields["material"]["verification_state"] == "unverified"
    assert fields["brand"]["value"] is None
    assert fields["composition"]["value"] is fields["waterproof"]["value"] is None
    assert fields["style"]["value"] == ["日系", "休闲"]
    ambiguous = next(
        d for d in batch["drafts"] if d["display_name_hint"] == "ambiguous"
    )
    assert len(ambiguous["legacy_rows"]) == 2
    assert ambiguous["annotation_fields"]["primary_color"]["value"] is None
    assert ambiguous["availability"] == "unknown"
    assert (output / "legacy-index.csv").read_bytes() == index.read_bytes()
    assert {
        p.relative_to(source): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in source.rglob("*")
        if p.is_file()
    } == original
    assert output.stat().st_mode & 0o077 == 0
    assert all(p.stat().st_mode & 0o077 == 0 for p in output.rglob("*") if p.is_file())
    before = {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    assert prepare(*args) == summary
    assert {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    } == before
    (output / "batch.json").write_text("{}")
    with pytest.raises(ValueError, match="EXISTING_DRAFT_CHANGED"):
        prepare(*args)


@pytest.mark.parametrize(
    "change,error",
    [
        ("bytes", "SOURCE_CHANGED_AFTER_FREEZE"),
        ("new_image", "SOURCE_CENSUS_CHANGED"),
        ("receipt", "RECEIPT_SOURCE_MISMATCH"),
        ("symlink", "SOURCE_SYMLINK"),
    ],
)
def test_drift_or_tampered_mapping_never_installs_partial_draft(
    tmp_path, change, error
):
    args = inputs(tmp_path)
    source, _, receipt, _, output = args
    original = source / "长袖衬衫/one.png"
    if change == "bytes":
        original.write_bytes(b"different")
    elif change == "new_image":
        Image.new("RGB", (3, 3)).save(source / "extra.png")
    elif change == "receipt":
        value = json.loads(receipt.read_text())
        next(iter(value["records"].values()))["sha256"] = "0" * 64
        receipt.write_text(json.dumps(value))
    else:
        external = tmp_path / "outside.png"
        original.rename(external)
        original.symlink_to(external)
    with pytest.raises(ValueError, match=error):
        prepare(*args)
    assert not output.exists()


def test_analysis_corrects_orientation_strips_metadata_and_caps_pixels(tmp_path):
    path = tmp_path / "source.jpg"
    image = Image.new("RGB", (1600, 800), "blue")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private metadata"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(path, exif=exif, icc_profile=profile)
    raw = path.read_bytes()
    output, dimensions = analysis_image(raw)
    assert dimensions == (640, 1280)
    import io

    with Image.open(io.BytesIO(output)) as derived:
        assert not derived.getexif()
        assert "exif" not in derived.info and "icc_profile" not in derived.info
        assert derived.mode == "RGB"
    assert path.read_bytes() == raw


def test_invalid_color_profile_cannot_produce_unreliable_annotation_image(tmp_path):
    path = tmp_path / "invalid-profile.png"
    Image.new("RGB", (10, 10)).save(path, icc_profile=b"not-a-valid-profile")
    with pytest.raises(OSError):
        analysis_image(path.read_bytes())
