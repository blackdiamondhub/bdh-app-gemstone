"""Turn an uploaded house photo into the hero background.

"Night" is a Pillow port of the look used in the original mockups: smooth
and posterize the photo, re-tint it as a blue night scene, keep warm
wood/window tones glowing, and fade the edges into the page.
"""
from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageFilter, ImageOps

LOOKS = ("night", "original", "drawing")
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


def _vignette(size, margin=0.08) -> Image.Image:
    w, h = size
    mx, my = max(1, int(w * margin)), max(1, int(h * margin))
    xs = Image.new("L", (w, 1))
    ramp_x = [min(i / mx, (w - 1 - i) / mx, 1.0) for i in range(w)]
    ramp_y = [min(j / my, (h - 1 - j) / my, 1.0) for j in range(h)]
    xs.putdata([int(255 * max(0.0, v)) for v in ramp_x])
    ys = Image.new("L", (1, h))
    ys.putdata([int(255 * max(0.0, v)) for v in ramp_y])
    return ImageChops.multiply(xs.resize((w, h)), ys.resize((w, h)))


def night(img: Image.Image) -> Image.Image:
    # Intermediates are dropped as soon as they're used: the app runs in a
    # small container and a 1600 px RGB image is ~8 MB in Pillow.
    sm = img.filter(ImageFilter.MedianFilter(5))
    sm = sm.filter(ImageFilter.SMOOTH_MORE)
    q = sm.quantize(colors=14, method=Image.Quantize.MEDIANCUT)
    del sm
    q = q.convert("RGB")
    lum = q.convert("L")
    # lum*k + c per channel (blue-ish night).
    tint = Image.merge("RGB", [
        lum.point(lambda v: min(255, int(v * 0.52 + 0.045 * 255))),
        lum.point(lambda v: min(255, int(v * 0.64 + 0.05 * 255))),
        lum.point(lambda v: min(255, int(v * 0.80 + 0.06 * 255))),
    ])
    del lum
    h, s, v = q.convert("HSV").split()
    # Pillow's hue is 0..255 for 0..360 degrees; OpenCV's 5..30 is 10..60 degrees.
    warm = ImageChops.multiply(h.point(lambda x: 255 if 7 <= x <= 43 else 0),
                               s.point(lambda x: 255 if x > 90 else 0))
    del h, s
    warm = ImageChops.multiply(warm, v.point(lambda x: 255 if x > 120 else 0))
    del v
    warm = warm.filter(ImageFilter.GaussianBlur(1.5))
    glow = q.point([int(x * 0.80) for x in range(256)] + [int(x * 0.68) for x in range(256)]
                   + [int(x * 0.55) for x in range(256)])
    del q
    tint.paste(glow, (0, 0), warm)
    del glow, warm
    out = Image.new("RGB", tint.size, (11, 13, 16))
    out.paste(tint, (0, 0), _vignette(tint.size))
    return out


def drawing(img: Image.Image) -> Image.Image:
    edges = img.convert("L").filter(ImageFilter.SMOOTH_MORE).filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges, cutoff=2)
    base = Image.new("RGB", img.size, (14, 18, 26))
    line = Image.new("RGB", img.size, (120, 150, 200))
    return Image.composite(line, base, edges)


def render(img: Image.Image, look: str) -> Image.Image:
    if look == "night":
        return night(img)
    if look == "drawing":
        return drawing(img)
    if look == "original":
        return img.copy()
    raise ValueError(f"unknown look {look!r}")


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()
