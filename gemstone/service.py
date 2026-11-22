"""What the views and the worker do with the Gemstone cloud.

Everything here takes controller ids from the UI, checks them against the
account's real controllers, and sends one write per controller through
:func:`cloud.write`, which keeps writes a couple of seconds apart.
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import re
import threading
import uuid
from typing import Any

from bdh_sdk.storage import app_state

from . import cloud

APP = cloud.APP
logger = logging.getLogger(__name__)

GUEST_FOLDER = "Guest designs"
GUEST_KEEP_DAYS = 30
#: Latest time of day a guest can keep the lights on until.
GUEST_LAST = dt.time(22, 0)
#: After the guest limit has passed, how long a guest can still turn lights on for.
GUEST_GRACE = dt.timedelta(minutes=15)
DEFAULT_LEDS = 100
MAX_COLORS = 20
WARM_HEX = "#fff1e0"
WARM_INT = 0xFF000000

#: The nine animations on the Create grid, as (label, Gemstone name).
ANIMATIONS = (
    ("Solid", "motionless"), ("Twinkle", "starry"), ("Chase", "chase"), ("Flow", "flow"),
    ("Wave", "wave"), ("Fireworks", "fireworks"), ("Fade", "fade"), ("Pulse", "pulse"),
    ("Marquee", "marquee"),
)
#: Everything else the controllers know, for "More animations".
MORE_ANIMATIONS = (
    "ghost", "pacman", "accent", "isofade", "around", "multipulse", "sway", "glitter",
    "gradient", "stack", "tremor", "stretch", "spectrum", "pyramid_chase", "smooth",
    "gradient_wave", "flicker", "spotlight", "eyeball", "glitch",
)
ANIMATION_NAMES = frozenset(a for _, a in ANIMATIONS) | frozenset(MORE_ANIMATIONS)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class Invalid(ValueError):
    """The request doesn't make sense (400)."""


class Forbidden(PermissionError):
    """Guests can't do this (403)."""


# ---------------------------------------------------------------- colours

def int_to_hex(value: int) -> str:
    n = int(value)
    r, g, b, w = n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF
    if w and not (r or g or b):
        return WARM_HEX
    if w:  # RGB plus some warm white: lighten towards warm.
        k = w / 255
        r, g, b = (round(c + (t - c) * k * 0.6) for c, t in zip((r, g, b), (255, 241, 224)))
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_int(value: str) -> int:
    if not isinstance(value, str) or not _HEX.match(value):
        raise Invalid(f"not a colour: {value!r}")
    if value.lower() == WARM_HEX:
        return WARM_INT
    r, g, b = int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
    return r | (g << 8) | (b << 16)


# ---------------------------------------------------------------- patterns

_known: dict[str, dict] = {}  # pattern id -> raw pattern body, for replay
_known_lock = threading.Lock()


def _remember(raw: dict) -> None:
    if raw and raw.get("id"):
        with _known_lock:
            _known[str(raw["id"])] = copy.deepcopy(raw)


def pattern_json(p, **extra) -> dict:
    """A pattern as the UI sees it."""
    raw = dict(getattr(p, "raw", None) or {})
    _remember(raw or {"id": p.id, "name": p.name, "colors": list(p.colors), "animation": p.animation,
                      "brightness": p.brightness, "speed": p.speed, "direction": p.direction,
                      "backgroundColor": p.background_color})
    out = {
        "id": p.id,
        "name": p.name or "Untitled",
        "colors": [int_to_hex(c) for c in (p.colors or [])][:MAX_COLORS] or ["#000000"],
        "animation": p.animation or "motionless",
        "brightness": int(p.brightness),
        "speed": int(p.speed),
        "direction": int(p.direction),
    }
    out.update(extra)
    return out


def _pattern_cls():
    from pygemstone import Pattern

    return Pattern


def _clamp(value, lo, hi, name) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"{name} must be a number") from None
    return max(lo, min(hi, n))


def pattern_from_spec(spec: dict, *, name: str = "", pattern_id: str = ""):
    """Build a pygemstone ``Pattern`` from the Create editor's JSON."""
    if not isinstance(spec, dict):
        raise Invalid("pattern must be an object")
    colors = spec.get("colors")
    if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
        raise Invalid(f"a pattern has 1 to {MAX_COLORS} colours")
    animation = spec.get("animation", "motionless")
    if animation not in ANIMATION_NAMES:
        raise Invalid(f"unknown animation {animation!r}")
    brightness = round(_clamp(spec.get("brightness", 100), 1, 100, "brightness") * 255 / 100)
    return _pattern_cls()(
        id=pattern_id,
        name=(name or str(spec.get("name") or "Untitled design"))[:60],
        colors=[hex_to_int(c) for c in colors],
        animation=animation,
        brightness=brightness,
        speed=_clamp(spec.get("speed", 128), 0, 255, "speed"),
        direction=_clamp(spec.get("direction", 0), 0, 4, "direction"),
        background_color=0,
    )


def solid_pattern(color: str, name: str = ""):
    return _pattern_cls()(
        id=f"bdh-solid-{color.lstrip('#').lower()}",
        name=name or "Solid colour",
        colors=[hex_to_int(color)],
        animation="motionless",
        brightness=255,
    )


def _resolve(ref: dict):
    """The pattern a play request points at."""
    if not isinstance(ref, dict):
        raise Invalid("pattern missing")
    kind = ref.get("kind")
    if kind == "solid":
        return solid_pattern(str(ref.get("color", "")), str(ref.get("name") or "")[:40])
    if kind == "spec":
        return pattern_from_spec(ref.get("spec"), pattern_id=f"bdh-live-{uuid.uuid4()}")
    pid = str(ref.get("id") or "")
    if not _ID.match(pid):
        raise Invalid("pattern id missing")
    with _known_lock:
        raw = _known.get(pid)
    if raw is None:  # not seen since start: load the lists once
        ours()
        library()
        with _known_lock:
            raw = _known.get(pid)
    if raw is None:
        raise Invalid("that pattern isn't in your folders or the library")
    return _pattern_cls().from_api(copy.deepcopy(raw))


# ---------------------------------------------------------------- reads

def _leds(raw: dict) -> int:
    hub = (raw or {}).get("hub") or {}
    count = hub.get("pixelCount")
    if isinstance(count, list):
        total = sum(int(c or 0) for c in count)
    else:
        try:
            total = int(count or 0)
        except (TypeError, ValueError):
            total = 0
    return total if total > 0 else DEFAULT_LEDS


def controllers() -> list[dict]:
    """The account's controllers: id, name, LED count and whether it's online."""
    async def fetch(client):
        out = []
        for d in await client.devices():
            raw = getattr(getattr(d, "record", None), "raw", None) or getattr(d, "raw", None) or {}
            out.append({"id": str(d.id), "name": d.name or "Lights", "leds": _leds(raw),
                        "online": bool(raw.get("online", True))})
        return out
    return cloud.read("controllers", 300, fetch)


def check_ids(ids) -> list[str]:
    """``ids`` minus duplicates, after checking each is one of ours."""
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        raise Invalid("pick at least one controller")
    known = {c["id"] for c in controllers()}
    out = []
    for cid in ids:
        cid = str(cid)
        if cid not in known:
            raise Invalid(f"unknown controller {cid!r}")
        if cid not in out:
            out.append(cid)
    return out


def names(ids) -> str:
    """"House + Hot Tub" for a list of controller ids."""
    by_id = {c["id"]: c["name"] for c in controllers()}
    return " + ".join(by_id.get(i, i) for i in ids)


def state(cid: str) -> dict:
    async def fetch(client):
        s = await client.device_state(cid)
        return {"on": bool(s.on_state),
                "pattern": pattern_json(s.pattern) if s.pattern else None,
                "raw": copy.deepcopy(s.pattern.raw) if s.pattern and s.pattern.raw else None}
    return cloud.read(f"state:{cid}", 15, fetch)


def public_state(cid: str) -> dict:
    s = state(cid)
    return {"on": s["on"], "pattern": s["pattern"]}


async def _all_folder_patterns(client) -> list:
    seen, out = set(), []
    for page in range(1, 21):
        batch = await client.folder_patterns(page)
        if not batch:
            break
        fresh = 0
        for fp in batch:
            key = (fp.folder_id, fp.pattern.id or fp.id)
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            out.append(fp)
        if not fresh:
            break
    return out


def _deleted(fp) -> bool:
    raw = getattr(fp, "raw", None) or {}
    return bool(raw.get("deleteAfter") or raw.get("deletedAt") or getattr(fp, "hidden", False))


def ours() -> dict:
    """The account's folders and the patterns in them."""
    async def fetch(client):
        folders = [f for f in await client.folders() if not f.hidden]
        pats = [fp for fp in await _all_folder_patterns(client) if not _deleted(fp)]
        counts: dict[str, int] = {}
        for fp in pats:
            counts[fp.folder_id] = counts.get(fp.folder_id, 0) + 1
        return {
            "folders": [{"id": f.folder_id, "name": f.name, "count": counts.get(f.folder_id, 0),
                         "guest": f.name == GUEST_FOLDER and not f.gemstone_managed,
                         "managed": bool(f.gemstone_managed)} for f in folders],
            "patterns": [pattern_json(fp.pattern, folder=fp.folder_id, fav=bool(fp.is_favorite),
                                      created=fp.created_at.isoformat() if fp.created_at else None)
                         for fp in pats],
        }
    return cloud.read("ours", 60, fetch)


def library() -> list[dict]:
    """Gemstone's curated pattern library, with each pattern's category."""
    async def fetch(client):
        out, seen = [], set()
        for page in range(1, 41):
            batch = await client.downloadable_patterns(page)
            if not batch:
                break
            fresh = 0
            for d in batch:
                if d.pattern.id in seen:
                    continue
                seen.add(d.pattern.id)
                fresh += 1
                p = d.pattern
                if d.pattern_name and not p.name:
                    p.name = d.pattern_name
                out.append(pattern_json(p, category=(d.category or "More").strip().title()))
            if not fresh:
                break
        return out
    return cloud.read("library", 6 * 3600, fetch)


# ---------------------------------------------------------------- writes

_undo: dict[str, list[dict]] = {}
_undo_order: list[str] = []
_undo_lock = threading.Lock()


def _snapshot(ids) -> list[dict]:
    out = []
    for cid in ids:
        try:
            s = state(cid)
        except cloud.CloudError:
            continue
        out.append({"id": cid, "on": s["on"], "raw": s["raw"]})
    return out


def _restore(snapshot) -> None:
    Pattern = _pattern_cls()
    for item in snapshot:
        cid = item["id"]
        if item.get("raw"):
            cloud.write(lambda c, cid=cid, raw=item["raw"]: c.play_pattern(cid, Pattern.from_api(copy.deepcopy(raw))),
                        invalidate=(f"state:{cid}",))
        if not item["on"]:
            cloud.write(lambda c, cid=cid: c.set_on_state(cid, False), invalidate=(f"state:{cid}",))


def _keep_undo(snapshot) -> str:
    token = uuid.uuid4().hex
    with _undo_lock:
        _undo[token] = snapshot
        _undo_order.append(token)
        while len(_undo_order) > 20:
            _undo.pop(_undo_order.pop(0), None)
    return token


def _play(ids, pattern) -> None:
    for cid in ids:
        cloud.write(lambda c, cid=cid: c.play_pattern(cid, pattern), invalidate=(f"state:{cid}",))


def play(ids, ref) -> dict:
    ids = check_ids(ids)
    pattern = _resolve(ref)
    snapshot = _snapshot(ids)
    _play(ids, pattern)
    return {"undo": _keep_undo(snapshot), "name": pattern.name, "on": names(ids)}


def undo(token: str) -> dict:
    with _undo_lock:
        snapshot = _undo.pop(str(token), None)
    if snapshot is None:
        raise Invalid("nothing to undo")
    _restore(snapshot)
    return {"restored": [s["id"] for s in snapshot]}


def live(ids, spec) -> dict:
    """Push the design being edited straight to the lights."""
    ids = check_ids(ids)
    _play(ids, pattern_from_spec(spec, pattern_id="bdh-live"))
    return {"on": names(ids)}


def power(ids, on: bool, *, admin: bool, now=None) -> dict:
    ids = check_ids(ids)
    for cid in ids:
        cloud.write(lambda c, cid=cid: c.set_on_state(cid, bool(on)), invalidate=(f"state:{cid}",))
    if on and not admin:
        _guest_keep_on(ids, now)
    if not on:
        _drop_until(ids)
    return {"on": bool(on)}


def brightness(ids, pct) -> dict:
    """Replay each controller's current pattern at ``pct`` percent."""
    ids = check_ids(ids)
    level = round(_clamp(pct, 1, 100, "brightness") * 255 / 100)
    Pattern = _pattern_cls()
    changed = []
    for cid in ids:
        raw = state(cid)["raw"]
        if not raw:
            continue
        raw = dict(raw, brightness=level)
        cloud.write(lambda c, cid=cid, raw=raw: c.play_pattern(cid, Pattern.from_api(raw)),
                    invalidate=(f"state:{cid}",))
        changed.append(cid)
    return {"brightness": level, "changed": changed}


# ---------------------------------------------------------------- keep on until

def _local_now(now=None) -> dt.datetime:
    from django.utils import timezone

    return timezone.localtime(now or timezone.now())


def guest_deadline(now=None) -> dt.datetime:
    """The latest a guest can keep the lights on until."""
    now = _local_now(now)
    limit = now.replace(hour=GUEST_LAST.hour, minute=GUEST_LAST.minute, second=0, microsecond=0)
    return limit if now < limit else now + GUEST_GRACE


def _next_at(hhmm: str, now) -> dt.datetime:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(hhmm or ""))
    if not m:
        raise Invalid("time must be HH:MM")
    at = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    return at if at > now else at + dt.timedelta(days=1)


def until() -> dict | None:
    return app_state(APP).get("until")


def set_until(ids, hhmm, *, admin: bool, now=None) -> dict | None:
    """Keep ``ids`` on until ``hhmm`` (next occurrence), then turn them off.

    Guests are held to :func:`guest_deadline` and can't clear the limit.
    """
    ids = check_ids(ids)
    now = _local_now(now)
    store = app_state(APP)
    if hhmm in (None, ""):
        if not admin:
            raise Forbidden("Guests can't remove the time limit")
        store.delete("until")
        return None
    at = _next_at(hhmm, now)
    if not admin:
        at = min(at, guest_deadline(now))
    value = {"at": at.isoformat(), "from": now.isoformat(), "ids": ids, "guest": not admin}
    store.set("until", value)
    return value


def _guest_keep_on(ids, now=None) -> None:
    """A guest turned lights on: make sure they go off by the guest deadline."""
    now = _local_now(now)
    deadline = guest_deadline(now)
    current = until()
    if current and dt.datetime.fromisoformat(current["at"]) <= deadline:
        merged = sorted(set(current["ids"]) | set(ids))
        if merged != sorted(current["ids"]):
            app_state(APP).set("until", dict(current, ids=merged))
        return
    app_state(APP).set("until", {"at": deadline.isoformat(), "from": now.isoformat(),
                                 "ids": list(ids), "guest": True})


def _drop_until(ids) -> None:
    current = until()
    if not current:
        return
    left = [i for i in current["ids"] if i not in ids]
    if left:
        app_state(APP).set("until", dict(current, ids=left))
    else:
        app_state(APP).delete("until")


def enforce_until(now=None) -> bool:
    """Turn lights off when their time is up. True if it did."""
    current = until()
    if not current:
        return False
    now = _local_now(now)
    if dt.datetime.fromisoformat(current["at"]) > now:
        return False
    for cid in current["ids"]:
        try:
            cloud.write(lambda c, cid=cid: c.set_on_state(cid, False), invalidate=(f"state:{cid}",))
        except cloud.CloudError:
            logger.warning("Couldn't turn %s off at the time limit; will retry", cid)
            return False
    app_state(APP).delete("until")
    return True


# ---------------------------------------------------------------- designs and folders

def _folder_list() -> list:
    return cloud.read("folders", 60, lambda c: c.folders())


def _owner_id() -> str:
    async def fetch(client):
        return (await client.account_profile()).id
    return cloud.read("owner", 3600, fetch)


def _create_folder(name: str):
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    folder_id = str(uuid.uuid4())
    body = {"folderId": folder_id, "ownerId": _owner_id(), "name": name, "icon": "file_folder",
            "backgroundColor": 0, "isSynchronized": True, "createdAt": now_ms, "newFolder": True}
    folder = cloud.write(lambda c: c.save_folder(folder_id, body), invalidate=("folders", "ours"))
    return folder.folder_id or folder_id


def guest_folder_id(create: bool = True) -> str | None:
    for f in _folder_list():
        if f.name == GUEST_FOLDER and not f.gemstone_managed and not f.hidden:
            return f.folder_id
    if not create:
        return None
    return _create_folder(GUEST_FOLDER)


def new_folder(name: str) -> dict:
    name = " ".join(str(name or "").split())[:40]
    if not name:
        raise Invalid("give the folder a name")
    if name == GUEST_FOLDER:
        raise Invalid("that name is kept for guest designs")
    for f in _folder_list():
        if f.name.lower() == name.lower() and not f.gemstone_managed:
            return {"id": f.folder_id, "name": f.name}
    return {"id": _create_folder(name), "name": name}


def guest_design_name(now=None) -> str:
    now = _local_now(now)
    return f"Guest design · {now:%Y-%m-%d %H:%M}"


def _design(design_id) -> dict | None:
    if not design_id:
        return None
    for p in ours()["patterns"]:
        if p["id"] == design_id:
            return p
    return None


def save_design(spec, *, admin: bool, design_id: str = "", folder_id: str = "",
                name: str = "", now=None) -> dict:
    """Save the design being edited; called on every change (auto-save).

    Guests always save into "Guest designs" under a date-coded name and can
    only update their own folder's designs. Admins pick the folder and name.
    """
    design_id = str(design_id or "")
    if design_id and not _ID.match(design_id):
        raise Invalid("bad design id")
    existing = _design(design_id)
    guest_id = guest_folder_id(create=not admin)
    if not admin:
        folder_id = guest_id
        if existing and existing["folder"] != guest_id:
            existing, design_id = None, ""  # a guest can't edit the house's own designs
        name = existing["name"] if existing else guest_design_name(now)
    else:
        folders = {f.folder_id: f for f in _folder_list()}
        if not folder_id:
            folder_id = existing["folder"] if existing else ""
        if not folder_id:
            mine = [f for f in folders.values() if not f.gemstone_managed and f.name != GUEST_FOLDER]
            folder_id = mine[0].folder_id if mine else new_folder("Our designs")["id"]
            folders = {f.folder_id: f for f in _folder_list()}
        folder = folders.get(folder_id)
        if folder is None or folder.gemstone_managed:
            raise Invalid("pick one of your own folders")
        name = " ".join(str(name or (existing or {}).get("name") or "Untitled design").split())[:60]
    if not existing:
        design_id = ""
    moved_from = existing["folder"] if existing and existing["folder"] != folder_id else None
    if moved_from:
        design_id = ""
    pattern = pattern_from_spec(spec, name=name, pattern_id=design_id or str(uuid.uuid4()))
    cloud.write(lambda c: c.save_pattern(folder_id, pattern), invalidate=("ours",))
    if moved_from:
        old = existing["id"]
        cloud.write(lambda c: c.delete_pattern(moved_from, old), invalidate=("ours",))
    folder_name = next((f.name for f in _folder_list() if f.folder_id == folder_id), "")
    _remember({"id": pattern.id, "name": pattern.name, "colors": pattern.colors,
               "animation": pattern.animation, "brightness": pattern.brightness,
               "speed": pattern.speed, "direction": pattern.direction, "backgroundColor": 0})
    return {"id": pattern.id, "name": pattern.name, "folder": {"id": folder_id, "name": folder_name},
            "locked": not admin}


def cleanup(now=None) -> int:
    """Delete guest designs older than ``GUEST_KEEP_DAYS``. Returns how many."""
    folder_id = guest_folder_id(create=False)
    if not folder_id:
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=GUEST_KEEP_DAYS)
    cloud.forget("ours")
    pats = cloud.read("cleanup-scan", 0, _all_folder_patterns)
    old = [fp for fp in pats
           if fp.folder_id == folder_id and fp.created_at and fp.created_at < cutoff and not _deleted(fp)]
    for fp in old:
        pid = fp.pattern.id or fp.id
        cloud.write(lambda c, pid=pid: c.delete_pattern(folder_id, pid), invalidate=("ours",))
    return len(old)
