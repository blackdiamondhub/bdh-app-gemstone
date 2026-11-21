"""Per-controller setup: house photo and the light runs drawn on it.

A layout lives in app state under ``layout:<controller id>``::

    {"photo": bool, "look": "night"|"original"|"drawing", "v": int,
     "runs": [{"name": str, "points": [[x, y], ...], "leds": int, "reverse": bool}]}

Points are fractions of the hero's width/height (0..1) so they work at any
size. The first point of a run is where its LEDs start, unless ``reverse``.
Runs are wired one after another, so run 2's first LED follows run 1's last.
"""
from __future__ import annotations

import re

from django.core.files.base import ContentFile

from bdh_sdk.storage import app_media, app_state

from . import stylize

APP = "gemstone"
MAX_RUNS = 8
MAX_POINTS = 200
MAX_LEDS = 5000
_CID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class Invalid(ValueError):
    pass


def check_cid(cid: str) -> str:
    if not isinstance(cid, str) or not _CID.match(cid):
        raise Invalid("bad controller id")
    return cid


def default_layout(leds: int = 100) -> dict:
    """No photo: the built-in house drawing with one straight run along the eave."""
    return {"photo": False, "look": "night", "v": 0,
            "runs": [{"name": "Roofline", "points": [[0.12, 0.42], [0.88, 0.42]],
                      "leds": max(1, min(MAX_LEDS, int(leds or 100))), "reverse": False}]}


def get(cid: str, leds: int = 100) -> dict:
    data = app_state(APP).get(f"layout:{check_cid(cid)}")
    return data if data else default_layout(leds)


def _point(p) -> list[float]:
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise Invalid("a point is [x, y]")
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError):
        raise Invalid("a point is two numbers") from None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise Invalid("points must be inside the picture")
    return [round(x, 4), round(y, 4)]


def clean_runs(runs) -> list[dict]:
    if not isinstance(runs, list) or not 1 <= len(runs) <= MAX_RUNS:
        raise Invalid(f"draw 1 to {MAX_RUNS} light runs")
    out = []
    for i, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            raise Invalid("a run is an object")
        points = run.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= MAX_POINTS:
            raise Invalid(f"run {i} needs 2 to {MAX_POINTS} points")
        try:
            leds = int(run.get("leds"))
        except (TypeError, ValueError):
            raise Invalid(f"run {i} needs a number of lights") from None
        if not 1 <= leds <= MAX_LEDS:
            raise Invalid(f"run {i} has 1 to {MAX_LEDS} lights")
        name = " ".join(str(run.get("name") or f"Run {i}").split())[:40] or f"Run {i}"
        out.append({"name": name, "points": [_point(p) for p in points], "leds": leds,
                    "reverse": bool(run.get("reverse"))})
    return out


def save(cid: str, runs=None, look: str | None = None, leds: int = 100) -> dict:
    current = get(cid, leds)
    data = dict(current)
    if runs is not None:
        data["runs"] = clean_runs(runs)
    if look is not None:
        if look not in stylize.LOOKS:
            raise Invalid("look is night, original or drawing")
        if look != current.get("look") and current.get("photo"):
            _render(cid, look)
            data["v"] = int(current.get("v", 0)) + 1
        data["look"] = look
    app_state(APP).set(f"layout:{cid}", data)
    return data


def start_offset(cid: str, run_index: int) -> int:
    """Index of the first LED of run ``run_index`` along the controller's strip."""
    runs = get(cid)["runs"]
    if not 0 <= run_index < len(runs):
        raise Invalid("no such run")
    return sum(r["leds"] for r in runs[:run_index])


# ---------------------------------------------------------------- photos

def _orig_name(cid):
    return f"photos/{cid}.orig.jpg"


def photo_name(cid):
    return f"photos/{cid}.jpg"


def _write(name: str, data: bytes) -> None:
    media = app_media(APP)
    if media.exists(name):
        media.delete(name)
    media.save(name, ContentFile(data))


def _render(cid: str, look: str) -> None:
    media = app_media(APP)
    with media.open(_orig_name(cid), "rb") as fh:
        img = stylize.load(fh.read())
    _write(photo_name(cid), stylize.to_jpeg(stylize.render(img, look)))


def upload_photo(cid: str, data: bytes, look: str | None = None, leds: int = 100) -> dict:
    check_cid(cid)
    current = get(cid, leds)
    look = look or current.get("look") or "night"
    if look not in stylize.LOOKS:
        raise Invalid("look is night, original or drawing")
    try:
        img = stylize.load(data)
    except stylize.BadImage as exc:
        raise Invalid(str(exc)) from None
    _write(_orig_name(cid), stylize.to_jpeg(img, quality=90))
    _write(photo_name(cid), stylize.to_jpeg(stylize.render(img, look)))
    data = dict(current, photo=True, look=look, v=int(current.get("v", 0)) + 1,
                aspect=round(img.width / img.height, 4))
    app_state(APP).set(f"layout:{cid}", data)
    return data


def remove_photo(cid: str, leds: int = 100) -> dict:
    check_cid(cid)
    media = app_media(APP)
    for name in (_orig_name(cid), photo_name(cid)):
        if media.exists(name):
            media.delete(name)
    data = dict(get(cid, leds), photo=False)
    data.pop("aspect", None)
    app_state(APP).set(f"layout:{cid}", data)
    return data


def photo_bytes(cid: str) -> bytes | None:
    check_cid(cid)
    media = app_media(APP)
    if not media.exists(photo_name(cid)):
        return None
    with media.open(photo_name(cid), "rb") as fh:
        return fh.read()
