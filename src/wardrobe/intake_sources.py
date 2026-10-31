"""Container header checks only; opaque sources retain client-reported decoding."""

import struct

from PIL import Image, UnidentifiedImageError

from .errors import BusinessError


def validate_source_header(path, mime, width, height):
    try:
        if mime in {"image/jpeg", "image/png"}:
            with Image.open(path, formats=["JPEG", "PNG"]) as image:
                if image.format != {"image/jpeg": "JPEG", "image/png": "PNG"}[
                    mime
                ] or image.size != (width, height):
                    raise ValueError()
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError()
        else:
            with path.open("rb") as file:
                head = file.read(4096)
            if len(head) < 20 or head[4:8] != b"ftyp":
                raise ValueError()
            length = struct.unpack(">I", head[:4])[0]
            if length < 20 or length > len(head) or length % 4:
                raise ValueError()
            brands = [head[8:12], *[head[i : i + 4] for i in range(16, length, 4)]]
            if any(x in brands for x in (b"msf1", b"hevc", b"hevx", b"avis")):
                raise ValueError()
            if not any(x in brands for x in (b"heic", b"heix", b"mif1")):
                raise ValueError()
            actual = "image/heif" if head[8:12] == b"mif1" else "image/heic"
            if actual != mime:
                raise ValueError()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise BusinessError(
            "SOURCE_FORMAT_UNSUPPORTED",
            "Source container or reported dimensions are invalid",
            422,
        ) from None


def validate_ocr_geometry(evidence):
    """Bound local OCR evidence; confidence is never authorization."""

    def uncertain():
        raise BusinessError(
            "OCR_UNCERTAIN",
            "OCR evidence geometry or candidate bounds are invalid",
            422,
        )

    lines = evidence.get("lines", [])
    if not isinstance(lines, list) or len(lines) > 200:
        uncertain()
    if (
        evidence.get("coordinate_system", "normalized-top-left-oriented")
        != "normalized-top-left-oriented"
    ):
        uncertain()
    for field in ("width", "height"):
        value = evidence.get(field, 1)
        if type(value) not in (int, float) or not 0 < value <= 3072:
            uncertain()
    for line in lines:
        if (
            not isinstance(line, dict)
            or not isinstance(line.get("text"), str)
            or len(line["text"].encode()) > 16384
        ):
            uncertain()
        candidates = line.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 3:
            uncertain()
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("text"), str)
                or len(candidate["text"].encode()) > 16384
            ):
                uncertain()
            confidence = candidate.get("confidence")
            if type(confidence) not in (float, int) or not 0 <= confidence <= 1:
                uncertain()
        box = line.get("bbox")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in box)
        ):
            uncertain()
        if box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001:
            uncertain()
