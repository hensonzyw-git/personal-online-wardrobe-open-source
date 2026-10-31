"""Bounded, offline-only image child. Never imports the application or database."""

import hashlib
import io
import os
import sys
from pathlib import Path

MAX_BYTES = 150 * 1024
MAX_PIXELS = 12_000_000
RECIPE = "preview-jpeg-768-v1"


def transform(source, expected_hash, target):
    from PIL import Image, ImageCms, ImageOps

    with source.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
            raise ValueError("source checksum")
        stream.seek(0)
        with Image.open(stream, formats=["PNG", "JPEG"]) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("source pixels")
            image.load()
            oriented = ImageOps.exif_transpose(image)
            icc = image.info.get("icc_profile")
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = oriented.convert("RGB") if not icc else oriented
            if icc:
                rgb = ImageCms.profileToProfile(
                    rgb,
                    ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGB",
                )
            for edge, quality in (
                (768, 80),
                (768, 70),
                (640, 80),
                (640, 70),
                (512, 80),
                (512, 70),
                (384, 80),
                (256, 75),
            ):
                thumb = rgb.copy()
                thumb.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                thumb.save(output, "JPEG", quality=quality, optimize=True)
                if output.tell() <= MAX_BYTES:
                    with target.open("wb") as out:
                        out.write(output.getvalue())
                        out.flush()
                        os.fsync(out.fileno())
                    return
            raise ValueError("output bytes")


def main():
    # Linux production only: a timeout must kill a real child, not a thread.
    if sys.platform != "linux":
        return 2
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES,) * 2)
    try:
        transform(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
        return 0
    except Exception:  # noqa: BLE001 -- child boundary emits only an exit code
        # No private paths, image metadata or native library error output.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
