"""Check and normalise an uploaded house photo for the hero background."""
from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_WIDTH = 1600
MAX_PIXELS = 40_000_000


class BadImage(ValueError):
    pass


def load(data: bytes) -> Image.Image:
    """Open, check and normalise an upload (orientation, RGB, size)."""
    if not data:
        raise BadImage("empty file")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise BadImage("that photo is too large")
        # Let the JPEG decoder scale down (1/2, 1/4, 1/8) while it reads, so
        # a phone photo never sits in memory at full size.
        img.draft("RGB", (MAX_WIDTH, 1))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except BadImage:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise BadImage("that file isn't a photo Pillow can read") from exc
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    return img


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()
