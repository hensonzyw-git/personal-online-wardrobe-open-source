"""Prepare private first-import drafts; never publish or call a model/provider."""

import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5

from PIL import Image, ImageCms, ImageOps

from wardrobe.assets import MAX_BYTES, MAX_PIXELS
from wardrobe.labels import LabelValues
from wardrobe.manifest import private_json

RECIPE = (
    "wardrobe-analysis-jpeg/2:exif-transpose:icc-to-srgb:rgb-white:1280:q90:no-metadata"
)
COLUMNS = {
    "颜色": "primary_color",
    "材质": "material",
    "图案": "pattern",
    "品牌": "brand",
    "风格标签": "style",
    "廓形": "silhouette",
    "适合季节": "seasons",
    "适合场合": "occasions",
}
MULTI = {"pattern", "style", "seasons", "occasions"}
UNKNOWN = {"", "未知", "不详", "杂牌", "无明确品牌"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def source_bytes(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in relative:
        raise ValueError("INVALID_SOURCE_PATH")
    local = root
    for part in path.parts:
        local /= part
        if local.is_symlink():
            raise ValueError("SOURCE_SYMLINK")
    if not local.resolve().is_relative_to(root) or not local.is_file():
        raise ValueError("SOURCE_MISSING_OR_OUTSIDE_ROOT")
    if not 0 < local.stat().st_size <= MAX_BYTES:
        raise ValueError("SOURCE_SIZE_LIMIT")
    return local.read_bytes()


def analysis_image(raw):
    with Image.open(io.BytesIO(raw), formats=["PNG", "JPEG"]) as original:
        if original.width * original.height > MAX_PIXELS:
            raise ValueError("SOURCE_PIXEL_LIMIT")
        original.load()
        oriented = ImageOps.exif_transpose(original)
        rgba = oriented.convert("RGBA")
        if original.info.get("icc_profile"):
            # Preserve appearance before removing private metadata/profile bytes.
            # Invalid profiles fail the batch rather than silently changing colors.
            profile = ImageCms.ImageCmsProfile(io.BytesIO(original.info["icc_profile"]))
            color_input = (
                oriented
                if oriented.mode in {"RGB", "CMYK", "LAB"}
                else rgba.convert("RGB")
            )
            converted = ImageCms.profileToProfile(
                color_input, profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
            ).convert("RGBA")
            converted.putalpha(rgba.getchannel("A"))
            rgba = converted
        flattened = Image.new("RGB", rgba.size, "white")
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        flattened.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        flattened.save(output, format="JPEG", quality=90)
        return output.getvalue(), flattened.size


def fields(row, csv_hash, line):
    result = {
        name: {
            "value": None,
            "source_kind": "unknown",
            "verification_state": "unknown",
            "assessment_state": "not_evaluated",
            "evidence": [],
        }
        for name in LabelValues.model_fields
    }
    if row is None:
        return result
    values = {}
    for column, field in COLUMNS.items():
        value = (row.get(column) or "").strip()
        if value in UNKNOWN:
            continue
        value = (
            [part.strip() for part in value.split("|") if part.strip()]
            if field in MULTI
            else value
        )
        values[field] = value
    # Reuse the service's constraints; invalid legacy values cannot silently truncate.
    LabelValues.model_validate(values)
    for name, value in values.items():
        result[name] = {
            "value": value,
            "source_kind": "legacy_index",
            "verification_state": "unverified",
            "assessment_state": "pending_visual_review",
            "evidence": [{"kind": "legacy_csv", "sha256": csv_hash, "line": line}],
        }
    return result


def cell(value):
    result = (
        str(value or "未知")
        .replace("|", " / ")
        .replace("\n", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    for character in ("\\", "[", "]", "`", "*", "_"):
        result = result.replace(character, "\\" + character)
    return result


def report(batch):
    s = batch["summary"]
    lines = [
        "# 首次衣橱建档草稿",
        "",
        f"图片 {s['source_files']} 张，旧索引 {s['legacy_rows']} 行，来源分类 {len(s['categories'])} 个。",
        "",
        "这是待审核的私人草稿。候选档案只按来源图片占位，尚未确认一图一件、多图同件或多主体关系；没有发布衣物、调用视觉模型或导入推荐历史。",
        "",
        "表中颜色、材质、品牌、图案与风格来自旧索引，均未核实。包括未知值在内的完整字段及证据保存在 batch.json；尚未视觉评估的字段不能计入识别覆盖率或准确率。",
        "",
        "## 需要优先处理的异常",
        "",
    ]
    for item in batch["anomalies"]:
        lines.append(
            f"- {cell(item['code'])}：{cell(item.get('relative_path') or item.get('key'))}；索引行 {item.get('csv_lines', [])}。"
        )
    lines += ["", "## 按来源分类审核", ""]
    for category in sorted(s["categories"]):
        lines += [
            f"### {cell(category)}",
            "",
            "| 候选名称 | 旧颜色 | 旧材质 | 旧品牌 | 旧图案 | 旧风格 | 匹配状态 | 图片副本 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for draft in batch["drafts"]:
            if draft["source_category"] != category:
                continue
            f = draft["annotation_fields"]
            values = [draft["display_name_hint"]] + [
                f[key]["value"]
                for key in ("primary_color", "material", "brand", "pattern", "style")
            ]
            values = [" / ".join(v) if isinstance(v, list) else v for v in values]
            lines.append(
                "| "
                + " | ".join(cell(v) for v in values)
                + f" | {cell(draft['index_match_state'])} | [查看](analysis/{draft['source_id']}.jpg) |"
            )
        lines.append("")
    lines += [
        "## 下一步",
        "",
        "1. 冻结视觉标注服务、模型、端点及费用上限，再对去除 EXIF 的副本生成视觉建议。",
        "2. 处理旧索引异常、多主体及同件多图；比对旧标签与视觉结果。",
        "3. 向用户展示确定的关联与标签变更，按批次人工确认后发布；确认发布不提升未核实材质或品牌的事实等级。",
        "",
    ]
    return "\n".join(lines)


def prepare(source, manifest_file, receipt_file, index_file, output):
    source, manifest_file, receipt_file, index_file, output = map(
        Path, (source, manifest_file, receipt_file, index_file, output)
    )
    if source.is_symlink() or not source.is_dir():
        raise ValueError("INVALID_SOURCE")
    root = source.resolve()
    if output.resolve().is_relative_to(root):
        raise ValueError("OUTPUT_MUST_BE_OUTSIDE_SOURCE")
    manifest_raw, receipt_raw, csv_raw = (
        p.read_bytes() for p in (manifest_file, receipt_file, index_file)
    )
    m, receipt = json.loads(manifest_raw), json.loads(receipt_raw)
    manifest_id = str(UUID(m["manifest_id"]))
    if (
        m["schema_version"] != "wardrobe-manifest/1"
        or receipt["schema_version"] != "wardrobe-original-transfer/1"
    ):
        raise ValueError("INVALID_INPUT_SCHEMA")
    if receipt["manifest_id"] != manifest_id or receipt["manifest_sha256"] != sha(
        manifest_raw
    ):
        raise ValueError("RECEIPT_MANIFEST_MISMATCH")
    ids = [str(UUID(r["source_id"])) for r in m["records"]]
    paths = [r["relative_path"] for r in m["records"]]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        raise ValueError("DUPLICATE_SOURCE_MAPPING")
    if set(ids) != set(receipt["records"]):
        raise ValueError("RECEIPT_SOURCE_SET_MISMATCH")
    current_paths = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        or p.name.lower().endswith((".png.icloud", ".jpg.icloud", ".jpeg.icloud"))
    }
    if current_paths != set(paths):
        raise ValueError("SOURCE_CENSUS_CHANGED")
    csv_hash = sha(csv_raw)
    reader = csv.DictReader(io.StringIO(csv_raw.decode("utf-8-sig"), newline=""))
    if not {"品类", "文件名", *COLUMNS} <= set(reader.fieldnames or []):
        raise ValueError("MISSING_INDEX_COLUMNS")
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("MALFORMED_CSV_ROW")
    lookup = defaultdict(list)
    for line, row in enumerate(rows, 2):
        lookup[(row["品类"].strip(), row["文件名"].strip())].append((line, row))
    csv_matches = set()
    drafts, anomalies, derivatives = [], [], []
    batch_id = str(uuid5(UUID(manifest_id), csv_hash + RECIPE))
    # All work happens in a private staging directory. Only a complete draft is installed.
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".catalog-draft-", dir=output.parent
    ) as temp:
        staging = Path(temp)
        (staging / "analysis").mkdir(mode=0o700)
        for record in m["records"]:
            source_id = str(UUID(record["source_id"]))
            item = receipt["records"][source_id]
            if record["state"] != "readable" or item["state"] != "ready":
                raise ValueError("SOURCE_OR_UPLOAD_NOT_READY")
            if any(item[k] != record[k] for k in ("relative_path", "sha256", "size")):
                raise ValueError("RECEIPT_SOURCE_MISMATCH")
            asset_id = str(UUID(item["asset_id"]))
            raw = source_bytes(root, record["relative_path"])
            if sha(raw) != record["sha256"] or len(raw) != record["size"]:
                raise ValueError("SOURCE_CHANGED_AFTER_FREEZE")
            jpg, dimensions = analysis_image(raw)
            derivative = staging / "analysis" / f"{source_id}.jpg"
            descriptor = os.open(
                derivative, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "wb") as file:
                file.write(jpg)
            derivatives.append(
                {
                    "source_id": source_id,
                    "original_asset_id": asset_id,
                    "original_sha256": record["sha256"],
                    "relative_path": f"analysis/{source_id}.jpg",
                    "sha256": sha(jpg),
                    "size": len(jpg),
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "recipe": RECIPE,
                }
            )
            path = PurePosixPath(record["relative_path"])
            key = (path.parent.name, path.stem)
            matches = lookup.get(key, [])
            csv_matches.add(key)
            state = (
                "unique_index_match"
                if len(matches) == 1
                else "duplicate_index_key"
                if matches
                else "unindexed_image"
            )
            if len(matches) != 1:
                anomalies.append(
                    {
                        "code": state,
                        "source_id": source_id,
                        "relative_path": record["relative_path"],
                        "csv_lines": [line for line, _ in matches],
                    }
                )
            row, line = (
                (matches[0][1], matches[0][0]) if len(matches) == 1 else (None, None)
            )
            drafts.append(
                {
                    "draft_id": str(uuid5(UUID(manifest_id), source_id)),
                    "source_id": source_id,
                    "original_asset_id": asset_id,
                    "original_sha256": record["sha256"],
                    "relative_path": record["relative_path"],
                    "source_category": path.parent.name,
                    "display_name_hint": path.stem,
                    "index_match_state": state,
                    "legacy_rows": [
                        {"line": number, "values": value} for number, value in matches
                    ],
                    "category_hint": {
                        "value": row["品类"] if row else path.parent.name,
                        "source_kind": "legacy_index" if row else "source_directory",
                        "verification_state": "unverified",
                    },
                    "identity_review_state": "pending_visual_and_human_review",
                    "availability": "unknown",
                    "annotation_fields": fields(row, csv_hash, line),
                    "visual_state": "not_started",
                    "published": False,
                }
            )
        for key, matches in lookup.items():
            if key not in csv_matches:
                anomalies.append(
                    {
                        "code": "index_without_image",
                        "key": list(key),
                        "csv_lines": [line for line, _ in matches],
                        "legacy_rows": [
                            {"line": line, "values": row} for line, row in matches
                        ],
                    }
                )
        summary = {
            "source_files": len(drafts),
            "legacy_rows": len(rows),
            "unique_index_matches": sum(
                d["index_match_state"] == "unique_index_match" for d in drafts
            ),
            "unindexed_images": sum(
                d["index_match_state"] == "unindexed_image" for d in drafts
            ),
            "duplicate_index_keys": sum(len(v) > 1 for v in lookup.values()),
            "index_keys_without_image": sum(k not in csv_matches for k in lookup),
            "categories": dict(
                sorted(Counter(d["source_category"] for d in drafts).items())
            ),
            "analysis_images": len(derivatives),
            "analysis_bytes": sum(d["size"] for d in derivatives),
            "visually_evaluated": 0,
            "published_garments": 0,
        }
        batch = {
            "schema_version": "wardrobe-import-draft/1",
            "batch_id": batch_id,
            "manifest_id": manifest_id,
            "manifest_sha256": sha(manifest_raw),
            "transfer_receipt_sha256": sha(receipt_raw),
            "legacy_index_sha256": csv_hash,
            "analysis_recipe": RECIPE,
            "state": "awaiting_visual_annotation",
            "published": False,
            "summary": summary,
            "drafts": drafts,
            "anomalies": anomalies,
            "derivatives": derivatives,
        }
        batch["digest"] = sha(canonical(batch))
        private_json(staging / "batch.json", batch)
        for filename, raw in (
            ("legacy-index.csv", csv_raw),
            ("review.md", report(batch).encode()),
        ):
            descriptor = os.open(
                staging / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "wb") as file:
                file.write(raw)
        if output.exists():
            if (
                output.is_symlink()
                or not output.is_dir()
                or output.stat().st_mode & 0o077
            ):
                raise ValueError("EXISTING_OUTPUT_NOT_PRIVATE")
            # Resume is exact and read-only; a changed/corrupt draft must use a new output.
            for path in staging.rglob("*"):
                if path.is_file():
                    saved = output / path.relative_to(staging)
                    if (
                        saved.is_symlink()
                        or not saved.is_file()
                        or saved.stat().st_mode & 0o077
                        or saved.read_bytes() != path.read_bytes()
                    ):
                        raise ValueError("EXISTING_DRAFT_CHANGED")
        else:
            os.rename(staging, output)
            # TemporaryDirectory cleanup tolerates the renamed directory.
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "manifest", "receipt", "index", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                prepare(
                    args.source, args.manifest, args.receipt, args.index, args.output
                )
            )
        )
    except (ValueError, OSError, KeyError, ImageCms.PyCMSError) as error:
        parser.exit(
            2,
            f"Draft preparation failed ({type(error).__name__}); nothing published.\n",
        )


if __name__ == "__main__":
    main()
