"""Per-controller setup: house photo and the light runs drawn on it.

A layout lives in app state under ``layout:<controller id>``::

    {"photo": bool, "v": int, "runs": [{"name": str, "points": [[x, y], ...]}]}

Points are fractions of the hero's width/height (0..1) so they work at any
size. A run is just where lights show in the picture: the preview spaces dots
evenly along it and the pattern flows through the runs in the order they were
drawn. How many real lights a run holds, or which end the string starts at,
doesn't matter for a preview, so neither is stored.
"""
from __future__ import annotations

import re

from django.core.files.base import ContentFile

from bdh_sdk.storage import app_media, app_state

from . import stylize

APP = "gemstone"
MAX_RUNS = 8
MAX_POINTS = 200
_CID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class Invalid(ValueError):
    pass


def check_cid(cid: str) -> str:
    if not isinstance(cid, str) or not _CID.match(cid):
        raise Invalid("bad controller id")
    return cid


def default_layout() -> dict:
    """No photo: the built-in house drawing with one straight run along the eave."""
    return {"photo": False, "v": 0, "runs": [{"name": "Roofline", "points": [[0.12, 0.42], [0.88, 0.42]]}]}


_OLD_RUN_KEYS = ("leds", "reverse")


def get(cid: str) -> dict:
    data = app_state(APP).get(f"layout:{check_cid(cid)}")
    if not data:
        return default_layout()
    look = data.get("look")
    data = {k: v for k, v in data.items() if k != "look"}
    data["runs"] = [{k: v for k, v in r.items() if k not in _OLD_RUN_KEYS} for r in data.get("runs") or []]
    if look and look != "original" and data.get("photo"):
        # Before 0.1.0b5 photos could be restyled (night / drawing); go back to
        # the photo as uploaded, which is kept alongside.
        _restore_original(cid)
        data["v"] = int(data.get("v", 0)) + 1
        app_state(APP).set(f"layout:{cid}", data)
    return data


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
        name = " ".join(str(run.get("name") or f"Run {i}").split())[:40] or f"Run {i}"
        out.append({"name": name, "points": [_point(p) for p in points]})
    return out


def save(cid: str, runs) -> dict:
    data = dict(get(cid), runs=clean_runs(runs))
    app_state(APP).set(f"layout:{cid}", data)
    return data


# ---------------------------------------------------------------- photos

def _orig_name(cid):
    # Pre-0.1.0b5 copy of the upload, kept so a restyled photo could be redone.
    return f"photos/{cid}.orig.jpg"


def photo_name(cid):
    return f"photos/{cid}.jpg"


def _write(name: str, data: bytes) -> None:
    media = app_media(APP)
    if media.exists(name):
        media.delete(name)
    media.save(name, ContentFile(data))


def _restore_original(cid: str) -> None:
    media = app_media(APP)
    if not media.exists(_orig_name(cid)):
        return
    with media.open(_orig_name(cid), "rb") as fh:
        _write(photo_name(cid), stylize.to_jpeg(stylize.load(fh.read())))
    media.delete(_orig_name(cid))


def upload_photo(cid: str, data: bytes) -> dict:
    check_cid(cid)
    current = get(cid)
    try:
        img = stylize.load(data)
    except stylize.BadImage as exc:
        raise Invalid(str(exc)) from None
    _write(photo_name(cid), stylize.to_jpeg(img))
    if app_media(APP).exists(_orig_name(cid)):
        app_media(APP).delete(_orig_name(cid))
    data = dict(current, photo=True, v=int(current.get("v", 0)) + 1,
                aspect=round(img.width / img.height, 4))
    app_state(APP).set(f"layout:{cid}", data)
    return data


def remove_photo(cid: str) -> dict:
    check_cid(cid)
    media = app_media(APP)
    for name in (_orig_name(cid), photo_name(cid)):
        if media.exists(name):
            media.delete(name)
    data = dict(get(cid), photo=False)
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
