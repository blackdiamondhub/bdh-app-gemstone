"""What the views and the worker do with the Gemstone cloud.

Everything here takes controller ids from the UI, checks them against the
account's real controllers, and sends one write per controller through
:func:`cloud.write`, which keeps writes a couple of seconds apart.
"""
from __future__ import annotations

import contextvars
import copy
import datetime as dt
import itertools
import json
import logging
import re
import threading
import time
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
#: A guest's off time must be after tonight's on time; with no location/sunset, after this.
GUEST_FALLBACK = dt.time(17, 0)
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


# ---------------------------------------------------------------- activity log
#
# Every command sent to the lights is remembered here (in app state, so it
# survives restarts and updates), with who sent it, so a night where the
# lights did something odd can be explained the next morning.

#: "admin" or "guest" for a request, set by the views; unset for the worker.
actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("gemstone_actor", default=None)
ACTIVITY_KEEP = 300
#: Repeats of the same command on the same lights this close together are one entry.
ACTIVITY_MERGE = dt.timedelta(minutes=2)
_activity_lock = threading.Lock()


def log_command(action: str, ids, now=None, **detail) -> None:
    """Remember a light command. Never fails the command it describes."""
    ids = list(ids)
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        entry = {"at": now.isoformat(), "action": action, "ids": ids}
        if actor.get():
            entry["by"] = actor.get()
        entry.update({k: v for k, v in detail.items() if v is not None})
        with _activity_lock:
            store = app_state(APP)
            items = list(store.get("activity") or [])
            last = items[-1] if items else None
            if (last and all(last.get(k) == entry.get(k) for k in ("action", "ids", "by"))
                    and now - dt.datetime.fromisoformat(last.get("last") or last["at"]) <= ACTIVITY_MERGE):
                last.update({k: v for k, v in entry.items() if k != "at"},
                            n=int(last.get("n", 1)) + 1, last=now.isoformat())
            else:
                items.append(entry)
            store.set("activity", items[-ACTIVITY_KEEP:])
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't record %s on %s", action, ids)


def activity() -> list[dict]:
    """The activity log, newest first, with controller names."""
    items = list(app_state(APP).get("activity") or [])
    try:
        by_id = {c["id"]: c["name"] for c in controllers()}
    except Exception:  # noqa: BLE001 - the log is still useful without names
        by_id = {}
    return [dict(e, names=[by_id.get(i, i) for i in e.get("ids", [])]) for e in reversed(items)]


# ---------------------------------------------------------------- colours

# LED channel values are linear light, so a secondary channel looks far stronger on
# the lights than the same number on a screen: Gemstone's "Orange" is (255, 30, 0),
# which drawn as-is is red. The UI works in screen colours and these two convert,
# keeping the brightest channel (so brightness and pure colours are unchanged) and
# gamma-correcting the others against it.
LED_GAMMA = 2.8


def _rebalance(rgb: tuple[int, int, int], power: float) -> tuple[int, ...]:
    m = max(rgb)
    if not m:
        return rgb
    return tuple(round(m * (c / m) ** power) for c in rgb)


def int_to_hex(value: int) -> str:
    """A device colour as the screen colour that looks like it."""
    n = int(value)
    r, g, b, w = n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF
    if w and not (r or g or b):
        return WARM_HEX
    r, g, b = _rebalance((r, g, b), 1 / LED_GAMMA)
    if w:  # RGB plus some warm white: lighten towards warm.
        k = w / 255
        r, g, b = (round(c + (t - c) * k * 0.6) for c, t in zip((r, g, b), (255, 241, 224)))
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_int(value: str) -> int:
    """A screen colour as the device colour that looks like it."""
    if not isinstance(value, str) or not _HEX.match(value):
        raise Invalid(f"not a colour: {value!r}")
    if value.lower() == WARM_HEX:
        return WARM_INT
    r, g, b = _rebalance((int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)), LED_GAMMA)
    return r | (g << 8) | (b << 16)


# ---------------------------------------------------------------- pattern parameters

# The ``extraParameters`` the preview draws, per animation, as (cloud key, cloud name,
# UI name). Cloud bodies don't always carry the names, so they're matched by key.
PARAMS = {
    "accent": (("0", "On Length", "on"), ("1", "Off Length", "off")),
    "isofade": (("0", "On Length", "on"), ("1", "Off Length", "off")),
    "motionless": (("1", "Length", "length"),),
    "smooth": (("0", "Length", "length"), ("1", "SECTION_SPACING", "spacing")),
}
PARAM_MAX = 200


def params_json(animation: str, extra) -> dict:
    """The preview's parameters from a cloud ``extraParameters`` body."""
    out = {}
    if not isinstance(extra, dict):
        return out
    for key, _, ui in PARAMS.get(animation, ()):
        entry = extra.get(key)
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[ui] = max(0, min(PARAM_MAX, int(value)))
    return out


def params_api(animation: str, params) -> dict | None:
    """A cloud ``extraParameters`` body from the editor's parameters."""
    if not isinstance(params, dict) or animation not in PARAMS:
        return None
    body: dict = {}
    for key, name, ui in PARAMS[animation]:
        if ui in params:
            body[key] = {"name": name, "value": _clamp(params[ui], 0, PARAM_MAX, ui)}
    return dict(body, version=0) if body else None


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
    if not raw:
        raw = {"id": p.id, "name": p.name, "colors": list(p.colors), "animation": p.animation,
               "brightness": p.brightness, "speed": p.speed, "direction": p.direction,
               "backgroundColor": p.background_color}
        if getattr(p, "extra_parameters", None):
            raw["extraParameters"] = p.extra_parameters
    _remember(raw)
    out = {
        "id": p.id,
        "name": p.name or "Untitled",
        "colors": [int_to_hex(c) for c in (p.colors or [])][:MAX_COLORS] or ["#000000"],
        "animation": p.animation or "motionless",
        "brightness": int(p.brightness),
        "speed": int(p.speed),
        "direction": int(p.direction),
    }
    if p.background_color:
        out["background"] = int_to_hex(p.background_color)
    params = params_json(out["animation"], getattr(p, "extra_parameters", None))
    if params:
        out["params"] = params
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
    background = spec.get("background")
    return _pattern_cls()(
        id=pattern_id,
        name=(name or str(spec.get("name") or "Untitled"))[:60],
        colors=[hex_to_int(c) for c in colors],
        animation=animation,
        brightness=brightness,
        speed=_clamp(spec.get("speed", 128), 0, 255, "speed"),
        direction=_clamp(spec.get("direction", 0), 0, 4, "direction"),
        background_color=hex_to_int(background) if background else 0,
        extra_parameters=params_api(animation, spec.get("params")),
    )


# The cloud only accepts UUID pattern ids. Solid colours get a fixed one built from
# the colour so the UI (solidId in gemstone.js) can tell which swatch is playing.
SOLID_ID_PREFIX = "bd450000-0000-4000-8000-000000"


def solid_id(color: str) -> str:
    hex_to_int(color)  # validates
    # The swatch's own hex, not the device round trip (gamma makes that lossy).
    return SOLID_ID_PREFIX + color[1:].lower()


def solid_pattern(color: str, name: str = ""):
    return _pattern_cls()(
        id=solid_id(color),
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
        return pattern_from_spec(ref.get("spec"), pattern_id=str(uuid.uuid4()))
    pid = str(ref.get("id") or "")
    if not _ID.match(pid):
        raise Invalid("pattern id missing")
    with _known_lock:
        raw = _known.get(pid)
    if raw is None:  # not seen since start: load the lists once
        ours()
        library()
        try:
            tonight_event()  # Autopilot's patterns for tonight's event
        except (cloud.CloudError, Invalid):
            pass
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
    return _mark_owners(cloud.read("ours", 60, fetch))


def _lib_ids() -> set[str]:
    return set(app_state(APP).get("lib_ids") or [])


def _mark_owners(data: dict) -> dict:
    """Tag each saved pattern ``stock`` (Gemstone's: a managed folder or a hearted
    library copy, which keeps the library id) or ``guest`` (in Guest designs)."""
    folders = {f["id"]: f for f in data["folders"]}
    lib = _lib_ids()
    pats = []
    for p in data["patterns"]:
        f = folders.get(p.get("folder")) or {}
        pats.append(dict(p, stock=bool(f.get("managed")) or p["id"] in lib, guest=bool(f.get("guest"))))
    return dict(data, patterns=pats)


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
                out.append(pattern_json(p, category=(d.category or "More").strip().title(),
                                        downloads=int(getattr(d, "downloads", 0) or 0)))
            if not fresh:
                break
        # Remembered so a hearted library copy (same id, in our folder) counts as stock.
        app_state(APP).set("lib_ids", [p["id"] for p in out])
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
            _mark_playing({cid: item["raw"].get("id")})
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
    _mark_playing({cid: getattr(pattern, "id", None) for cid in ids})


_playing_lock = threading.Lock()


def _mark_playing(by_cid: dict) -> None:
    """Remember what each controller was last seen or told to play (``note_outside_plays``)."""
    with _playing_lock:
        store = app_state(APP)
        playing = dict(store.get("playing") or {})
        playing.update({cid: str(pid) for cid, pid in by_cid.items() if pid})
        store.set("playing", playing)


def note_outside_plays(now=None) -> list[str]:
    """Count designs started somewhere else (the Gemstone app, a timer) as plays, so
    they show in Recently played. A design on several controllers counts once.
    Returns the pattern ids counted."""
    seen = dict(app_state(APP).get("playing") or {})
    now_playing, counted = {}, []
    for c in controllers():
        try:
            s = state(c["id"])
        except cloud.CloudError:
            continue
        pid = (s.get("pattern") or {}).get("id")
        if not s.get("on") or not pid:
            continue
        now_playing[c["id"]] = pid
        if seen.get(c["id"]) != pid and pid not in counted:
            counted.append(pid)
    for pid in counted:
        record_play(pid, now)
    if now_playing:
        _mark_playing(now_playing)
    return counted


def play(ids, ref) -> dict:
    ids = check_ids(ids)
    pattern = _resolve(ref)
    snapshot = _snapshot(ids)
    _play(ids, pattern)
    log_command("play", ids, pattern=pattern.name)
    _end_dark()
    record_play(getattr(pattern, "id", None))
    return {"undo": _keep_undo(snapshot), "name": pattern.name, "on": names(ids)}


PLAYS_KEEP = 500
_plays_lock = threading.Lock()


def record_play(pattern_id, now=None) -> None:
    """Count a play of ``pattern_id`` on this hub (drives "Recently played" and group order)."""
    if not pattern_id:
        return
    now = now or dt.datetime.now(dt.timezone.utc)
    with _plays_lock:
        store = app_state(APP)
        plays = dict(store.get("plays") or {})
        prev = plays.get(str(pattern_id)) or {}
        plays[str(pattern_id)] = {"n": int(prev.get("n", 0)) + 1, "at": now.isoformat()}
        if len(plays) > PLAYS_KEEP:
            keep = sorted(plays.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:PLAYS_KEEP]
            plays = dict(keep)
        store.set("plays", plays)


def plays() -> dict:
    return dict(app_state(APP).get("plays") or {})


def undo(token: str) -> dict:
    with _undo_lock:
        snapshot = _undo.pop(str(token), None)
    if snapshot is None:
        raise Invalid("nothing to undo")
    _restore(snapshot)
    log_command("undo", [s["id"] for s in snapshot])
    return {"restored": [s["id"] for s in snapshot]}


def live(ids, spec, *, start: bool = False) -> dict:
    """Push the design being edited straight to the lights.

    ``start`` marks the first push of a "Show on house" try: what the lights
    were doing is kept so "Back" (``undo``) can put it back, including off.
    """
    ids = check_ids(ids)
    pattern = pattern_from_spec(spec, pattern_id=str(uuid.uuid4()))
    snapshot = _snapshot(ids) if start else None
    _play(ids, pattern)
    log_command("live", ids, pattern=pattern.name or None)
    _end_dark()
    out = {"on": names(ids)}
    if snapshot is not None:
        out["undo"] = _keep_undo(snapshot)
    return out


def power(ids, on: bool, *, admin: bool, now=None) -> dict:
    ids = check_ids(ids)
    for cid in ids:
        cloud.write(lambda c, cid=cid: c.set_on_state(cid, bool(on)), invalidate=(f"state:{cid}",))
    log_command("on" if on else "off", ids)
    if on:
        _end_dark()
    if on and not admin:
        _guest_keep_on(ids, now)
    if not on:
        _drop_until(ids)
    return {"on": bool(on)}


def brightness(ids, pct) -> dict:
    """Replay each controller's current pattern at ``pct`` percent.

    Controllers that are off are left off: replaying a pattern turns a
    controller on, and a brightness nudge mustn't do that.
    """
    ids = check_ids(ids)
    pct = _clamp(pct, 1, 100, "brightness")
    level = round(pct * 255 / 100)
    Pattern = _pattern_cls()
    changed = []
    for cid in ids:
        current = state(cid)
        raw = current["raw"]
        if not raw or not current["on"]:
            continue
        raw = dict(raw, brightness=level)
        cloud.write(lambda c, cid=cid, raw=raw: c.play_pattern(cid, Pattern.from_api(raw)),
                    invalidate=(f"state:{cid}",))
        changed.append(cid)
    if changed:
        log_command("brightness", changed, level=round(pct))
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
    log_command("off", current["ids"], source="keep on until")
    app_state(APP).delete("until")
    return True


# ---------------------------------------------------------------- autopilot and its schedule
#
# Gemstone's "Autopilot" (``/events/settings``) is one object per home group:
# which controllers it drives, which holidays/teams it celebrates, and the
# daily window (on at sunset +/- an offset, off at a clock time) the lights
# run in. There's no on/off flag: the Gemstone app treats "no controllers" as
# off, so that's what the master switch here does too.

#: Sunset offset bounds and step, in minutes.
ON_OFFSET_MIN, ON_OFFSET_MAX, ON_OFFSET_STEP = -120, 120, 5
#: Off times before this belong to the night before ("past midnight").
NIGHT_ENDS = dt.time(6, 0)
EVENT_PAGES = 5

_EMOJI = {
    "maple_leaf": "🍁", "usa": "🇺🇸", "rabbit_face": "🐰", "airplane": "✈️",
    "christmas_tree": "🎄", "candle": "🕎", "diya_lamp": "🪔", "new_moon": "🌙",
    "sparkler": "🎇", "light_bulb": "💡", "pumpkin": "🎃", "heart": "❤️",
    "shamrock": "☘️", "fireworks": "🎆", "turkey": "🦃",
}
_NAME_EMOJI = (("thanksgiving", "🦃"), ("halloween", "🎃"), ("valentine", "❤️"),
               ("patrick", "☘️"), ("new year", "🎆"))


def emoji(icon: str | None, name: str = "") -> str:
    """An emoji for a Gemstone category icon name (``"maple_leaf"``, ``"assets/icons/airplane.svg"``)."""
    low = (name or "").lower()
    for word, mark in _NAME_EMOJI:
        if word in low:
            return mark
    key = (icon or "").lower()
    if "/" in key:
        key = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _EMOJI.get(key, "✨")


def argb_hex(value) -> str | None:
    """``0xFFRRGGBB`` (the Gemstone app's colour ints) as ``#rrggbb``."""
    if value is None:
        return None
    try:
        return f"#{int(value) & 0xFFFFFF:06x}"
    except (TypeError, ValueError):
        return None


def _hhmm(value) -> dt.time:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or ""))
    if not m:
        raise Invalid("time must be HH:MM")
    return dt.time(int(m[1]), int(m[2]))


def homegroup_id() -> str:
    """The home group whose autopilot we edit: the only one, or the first with controllers."""
    async def fetch(client):
        groups = await client.homegroups()
        if len(groups) == 1:
            return groups[0].id
        for g in groups:
            if await client.devices(g.id):
                return g.id
        return groups[0].id if groups else ""
    hg = cloud.read("homegroup", 3600, fetch)
    if not hg:
        raise Invalid("this Gemstone account has no home group")
    return hg


def _settings(ttl: float = 120):
    hg = homegroup_id()
    return cloud.read("autopilot:settings", ttl, lambda c: c.events_settings(hg))


def _events() -> list:
    hg = homegroup_id()

    async def fetch(client):
        out, seen = [], set()
        for page in range(EVENT_PAGES):
            batch = await client.subscribed_events(hg, page)
            fresh = [e for e in batch if e.event_id not in seen]
            if not fresh:
                break
            seen.update(e.event_id for e in fresh)
            out.extend(fresh)
        return sorted(out, key=lambda e: (e.start_date, e.name))
    return cloud.read("autopilot:events", 1800, fetch)


def _categories() -> list:
    return cloud.read("autopilot-categories", 6 * 3600, lambda c: c.events_categories())


def _pct(level) -> int | None:
    return None if level is None else max(1, round(int(level) * 100 / 255))


def _schedule_json(s) -> dict:
    w = s.schedule
    return {"on_time": (w.on_time if w else "") or "sunset",
            "on_offset": int(w.on_offset_minutes) if w else 0,
            "off_time": (w.off_time if w else "") or "",
            "off_offset": int(w.off_offset_minutes) if w else 0}


def _settings_json(s) -> dict:
    return {"enabled": bool(s.device_ids), "device_ids": list(s.device_ids),
            "category_ids": list(s.category_ids),
            "allow_static": bool(s.allow_static_patterns),
            "allow_animated": bool(s.allow_animated_patterns),
            "brightness": _pct(s.brightness), "schedule": _schedule_json(s)}


def _event_json(e, *, patterns: bool = False) -> dict:
    raw = e.raw or {}
    out = {"id": e.event_id, "name": e.name, "category_id": e.category_id,
           "category": e.category_name, "emoji": emoji(e.icon, e.name),
           "color": argb_hex(e.background_color), "start": e.start_date, "end": e.end_date,
           "home": bool(raw.get("isHomeTeam")),
           "count": len(e.static_patterns) + len(e.animated_patterns)}
    if patterns:
        out["patterns"] = ([pattern_json(p, kind="static") for p in e.static_patterns]
                           + [pattern_json(p, kind="animated") for p in e.animated_patterns])
    return out


def categories() -> dict:
    """Autopilot's catalogue, split into holidays (``general``) and sports teams by league."""
    general, teams, leagues = [], [], []
    for c in _categories():
        if c.group == "general":
            general.append({"id": c.id, "name": c.name, "emoji": emoji(c.icon, c.name),
                            "color": argb_hex(c.background_color)})
        else:
            league = (c.group or "other").upper()
            if league not in leagues:
                leagues.append(league)
            teams.append({"id": c.id, "name": c.name, "league": league})
    return {"general": general, "teams": teams, "leagues": leagues}


def _event_day(e, day: dt.date) -> bool:
    try:
        start = dt.date.fromisoformat(str(e.start_date)[:10])
        end = dt.date.fromisoformat(str(e.end_date or e.start_date)[:10])
    except ValueError:
        return False
    return start <= day <= end


def tonight_event(now=None) -> dict | None:
    """Tonight's autopilot event, with its patterns, or None on an ordinary night."""
    day = _local_now(now).date()
    for e in _events():
        if _event_day(e, day):
            return _event_json(e, patterns=True)
    return None


def autopilot(now=None) -> dict:
    """Everything the Autopilot settings page shows."""
    day = _local_now(now).date()
    upcoming = []
    for e in _events():
        try:
            end = dt.date.fromisoformat(str(e.end_date or e.start_date)[:10])
        except ValueError:
            continue
        if end >= day:
            upcoming.append(dict(_event_json(e), tonight=_event_day(e, day)))
    return {"settings": _settings_json(_settings()), "categories": categories(),
            "upcoming": upcoming, "controllers": controllers(), "schedule": schedule(now)}


# --- sunset

def _sun(day: dt.date, lat: float, lon: float, tz, *, rise: bool) -> dt.datetime | None:
    """Sunrise or sunset on ``day`` at ``lat``/``lon`` (NOAA's approximation, about a minute out)."""
    import math

    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
              - math.tan(phi) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    minutes = 720 - 4 * (lon + (ha if rise else -ha)) - eqtime
    midnight = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc)
    return (midnight + dt.timedelta(minutes=minutes)).astimezone(tz)


def sunset(day: dt.date, lat: float, lon: float, tz) -> dt.datetime | None:
    """Sunset on ``day`` at ``lat``/``lon``, or None at the poles."""
    return _sun(day, lat, lon, tz, rise=False)


def sunrise(day: dt.date, lat: float, lon: float, tz) -> dt.datetime | None:
    """Sunrise on ``day`` at ``lat``/``lon``, or None at the poles."""
    return _sun(day, lat, lon, tz, rise=True)


def _site():
    try:
        from bdh_sdk.location import location
    except ImportError:  # pragma: no cover - older bdh-sdk
        return None
    try:
        where = location()
    except Exception:  # noqa: BLE001 - no settings store yet
        return None
    return where if where and where.has_coordinates else None


def sunset_today(now=None) -> dt.datetime | None:
    now = _local_now(now)
    where = _site()
    if where is None:
        return None
    return sunset(now.date(), where.latitude, where.longitude, now.tzinfo)


# --- lights off tonight

def dark() -> dict | None:
    """``{"until": iso}`` while the lights are being kept off for the night."""
    return app_state(APP).get("dark")


def _morning(now) -> dt.datetime:
    at = now.replace(hour=NIGHT_ENDS.hour, minute=NIGHT_ENDS.minute, second=0, microsecond=0)
    return at if at > now else at + dt.timedelta(days=1)


def set_dark(on: bool, now=None) -> dict | None:
    """Keep every controller off until morning (``on``), or let the schedule run again."""
    store = app_state(APP)
    if not on:
        store.delete("dark")
        return None
    now = _local_now(now)
    value = {"until": _morning(now).isoformat(), "from": now.isoformat()}
    store.set("dark", value)
    ids = [c["id"] for c in controllers()]
    for cid in ids:
        cloud.write(lambda c, cid=cid: c.set_on_state(cid, False), invalidate=(f"state:{cid}",))
    log_command("off", ids, source="lights off tonight")
    _drop_until(ids)
    return value


def _end_dark() -> None:
    # Someone turned lights on by hand: that beats "lights off tonight".
    if dark():
        app_state(APP).delete("dark")


def enforce_dark(now=None) -> bool:
    """While "lights off tonight" is set, turn off anything that comes on. True if it did."""
    current = dark()
    if not current:
        return False
    now = _local_now(now)
    if dt.datetime.fromisoformat(current["until"]) <= now:
        app_state(APP).delete("dark")
        return False
    did = False
    for c in controllers():
        cid = c["id"]
        try:
            if not state(cid)["on"]:
                continue
            cloud.write(lambda cl, cid=cid: cl.set_on_state(cid, False), invalidate=(f"state:{cid}",))
            log_command("off", [cid], source="lights off tonight")
            did = True
        except cloud.CloudError:
            logger.warning("Couldn't keep %s off for the night; will retry", cid)
    return did


# --- autopilot off-time backstop
#
# Gemstone's cloud turns Autopilot's lights off at the off time, but not
# always: a controller sent a command during the evening (a slider nudge, a
# pattern) can be skipped and left on all night. So once the off time has
# passed, anything Autopilot drives that's still on gets turned off here.

#: How long after the off time to check, so Gemstone gets to do it first.
AUTOPILOT_OFF_GRACE = dt.timedelta(minutes=3)


def _autopilot_off_due(s, now) -> tuple[dt.date, dt.datetime] | None:
    """(night, when to check) for tonight's off time, or None if it can't be worked out."""
    w = s.schedule
    try:
        off = _hhmm(getattr(w, "off_time", None))
    except Invalid:
        return None
    night = now.date() if now.time() >= NIGHT_ENDS else now.date() - dt.timedelta(days=1)
    day = night + dt.timedelta(days=1) if off < NIGHT_ENDS else night
    at = dt.datetime.combine(day, off, tzinfo=now.tzinfo)
    return night, at + dt.timedelta(minutes=getattr(w, "off_offset_minutes", 0) or 0) + AUTOPILOT_OFF_GRACE


def enforce_autopilot_off(now=None) -> list[str]:
    """Once a night, after Autopilot's off time, turn off its controllers that are still on.

    Lights under an active "keep on until" are left to that. Returns the ids turned off.
    """
    now = _local_now(now)
    s = _settings()
    known = {c["id"] for c in controllers()}
    ids = [cid for cid in s.device_ids if cid in known]
    due = _autopilot_off_due(s, now) if ids else None
    if due is None:
        return []
    night, at = due
    morning = dt.datetime.combine(night + dt.timedelta(days=1), NIGHT_ENDS, tzinfo=now.tzinfo)
    store = app_state(APP)
    if not at <= now < morning or store.get("autopilot_off_last") == night.isoformat():
        return []
    current = until()
    kept = set(current["ids"]) if current and dt.datetime.fromisoformat(current["at"]) > now else set()
    done = []
    for cid in ids:
        if cid in kept:
            continue
        try:
            if not state(cid)["on"]:
                continue
            cloud.write(lambda c, cid=cid: c.set_on_state(cid, False), invalidate=(f"state:{cid}",))
        except cloud.CloudError:
            logger.warning("Couldn't turn %s off after Autopilot's off time; will retry", cid)
            if done:
                log_command("off", done, source="autopilot backstop")
            return done
        done.append(cid)
    if done:
        logger.info("Autopilot left %s on after its off time; turned off", done)
        log_command("off", done, source="autopilot backstop")
    store.set("autopilot_off_last", night.isoformat())
    return done


# --- the schedule

def schedule(now=None) -> dict:
    """Tonight's schedule as the Tonight screen shows it."""
    now = _local_now(now)
    s = _settings()
    out = dict(_schedule_json(s), autopilot=bool(s.device_ids), dark=dark(),
               sunset=None, on_at=None)
    sun = sunset_today(now)
    if sun is not None:
        out["sunset"] = sun.strftime("%H:%M")
    on_time = out["on_time"]
    if on_time == "sunset" and sun is not None:
        out["on_at"] = (sun + dt.timedelta(minutes=out["on_offset"])).strftime("%H:%M")
    elif on_time not in ("sunset", "sunrise"):
        try:
            out["on_at"] = _hhmm(on_time).strftime("%H:%M")
        except Invalid:
            pass
    return out


def _check_offset(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise Invalid("the sunset offset must be a number of minutes") from None
    if not ON_OFFSET_MIN <= n <= ON_OFFSET_MAX:
        raise Invalid(f"the sunset offset must be between {ON_OFFSET_MIN} and {ON_OFFSET_MAX} minutes")
    if n % ON_OFFSET_STEP:
        raise Invalid(f"the sunset offset goes in steps of {ON_OFFSET_STEP} minutes")
    return n


def _night_minutes(t: dt.time) -> int:
    """Minutes into the evening's night: times before ``NIGHT_ENDS`` are after midnight."""
    m = t.hour * 60 + t.minute
    return m + 24 * 60 if t < NIGHT_ENDS else m


def _on_at(w, now=None) -> dt.time | None:
    """When window ``w`` turns the lights on tonight, or None if that can't be worked out."""
    on_time = getattr(w, "on_time", None) or "sunset"
    if on_time == "sunset":
        sun = sunset_today(now)
        if sun is None:
            return None
        return (sun + dt.timedelta(minutes=w.on_offset_minutes or 0)).time().replace(second=0, microsecond=0)
    if on_time == "sunrise":
        return None
    try:
        return _hhmm(on_time)
    except Invalid:
        return None


def guest_off_after(w, now=None) -> dt.time:
    """A guest's off time must be strictly after this: tonight's on time, or ``GUEST_FALLBACK``."""
    return _on_at(w, now) or GUEST_FALLBACK


def _check_off_time(value, *, admin: bool, after: dt.time | None = None) -> str:
    t = _hhmm(value)
    if not admin:
        last = GUEST_LAST.strftime("%H:%M")
        if _night_minutes(t) > _night_minutes(GUEST_LAST):
            raise Invalid(f"Guests can have the lights go off by {last} at the latest")
        if after is not None and _night_minutes(t) <= _night_minutes(after):
            raise Invalid(f"Pick an off time after the lights come on at {after.strftime('%H:%M')} "
                          f"(and by {last})")
    return t.strftime("%H:%M")


def _apply_schedule(s, *, on_offset=None, off_time=None, admin: bool, now=None) -> bool:
    """Change ``s.schedule`` in place. True if anything changed."""
    from pygemstone.models import EventsScheduleWindow

    w = copy.deepcopy(s.schedule) or EventsScheduleWindow(on_time="sunset", off_time="22:00")
    changed = False
    if on_offset is not None:
        n = _check_offset(on_offset)
        if w.on_time not in ("sunset", "sunrise"):
            w.on_time = "sunset"  # only sunset-relative on times are offered here
        changed |= n != w.on_offset_minutes or s.schedule is None
        w.on_offset_minutes = n
    after = None if admin else guest_off_after(w, now)
    if off_time is not None:
        same = False
        try:
            same = _hhmm(off_time) == _hhmm(w.off_time)
        except Invalid:
            pass
        if not same:  # a guest can leave an admin's later time alone
            w.off_time = _check_off_time(off_time, admin=admin, after=after)
            w.off_offset_minutes = 0
            changed = True
    if after is not None:
        # Whatever a guest changed, the lights must still go off after they come on.
        try:
            current = _hhmm(w.off_time)
        except Invalid:
            current = None
        if current is not None and _night_minutes(current) <= _night_minutes(after):
            raise Invalid(f"The lights would go off at {current.strftime('%H:%M')}, before they come on at "
                          f"{after.strftime('%H:%M')} — pick a later off time")
    s.schedule = w
    return changed


def _save(s) -> None:
    cloud.write(lambda c: c.update_events_settings(s), invalidate=("autopilot:",))
    w = s.schedule
    log_command("autopilot", list(s.device_ids), off=getattr(w, "off_time", None),
                on_offset=getattr(w, "on_offset_minutes", None))


def set_schedule(*, on_offset=None, off_time=None, dark_tonight=None, admin: bool, now=None) -> dict:
    """Change tonight's schedule. Anyone can; a guest's off time must be after tonight's on time
    and no later than ``GUEST_LAST``."""
    if on_offset is None and off_time is None and dark_tonight is None:
        raise Invalid("nothing to change")
    if on_offset is not None or off_time is not None:
        s = copy.deepcopy(_settings(ttl=0))
        if _apply_schedule(s, on_offset=on_offset, off_time=off_time, admin=admin, now=now):
            _save(s)
    if dark_tonight is not None:
        set_dark(bool(dark_tonight), now)
    return schedule(now)


def set_autopilot(changes, *, admin: bool) -> dict:
    """Change the Autopilot settings (admins only). Returns the settings as saved."""
    if not admin:
        raise Forbidden("Only a hub admin can change Autopilot")
    if not isinstance(changes, dict) or not changes:
        raise Invalid("nothing to change")
    known = {"enabled", "device_ids", "allow_static", "allow_animated", "brightness",
             "add_categories", "remove_categories", "schedule"}
    unknown = set(changes) - known
    if unknown:
        raise Invalid(f"can't change {', '.join(sorted(unknown))}")
    s = copy.deepcopy(_settings(ttl=0))
    store = app_state(APP)
    if "device_ids" in changes:
        s.device_ids = check_ids(changes["device_ids"])
    if "enabled" in changes:
        if not changes["enabled"]:
            if s.device_ids:
                store.set("autopilot_devices", list(s.device_ids))
            s.device_ids = []
        elif not s.device_ids:
            mine = [c["id"] for c in controllers()]
            kept = [i for i in (store.get("autopilot_devices") or []) if i in mine]
            s.device_ids = kept or mine
            s.setup_yet = True
    if "allow_static" in changes:
        s.allow_static_patterns = bool(changes["allow_static"])
    if "allow_animated" in changes:
        s.allow_animated_patterns = bool(changes["allow_animated"])
    if not (s.allow_static_patterns or s.allow_animated_patterns):
        raise Invalid("Autopilot needs static or animated patterns, or both")
    if "brightness" in changes:
        s.brightness = round(_clamp(changes["brightness"], 1, 100, "brightness") * 255 / 100)
    if "add_categories" in changes or "remove_categories" in changes:
        valid = {c.id for c in _categories()}
        add, remove = changes.get("add_categories") or [], changes.get("remove_categories") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            raise Invalid("categories must be a list")
        bad = [str(i) for i in add if str(i) not in valid]
        if bad:
            raise Invalid(f"unknown category {bad[0]!r}")
        ids = [i for i in s.category_ids if i not in {str(r) for r in remove}]
        ids += [str(i) for i in add if str(i) not in ids]
        s.category_ids = ids
    if "schedule" in changes:
        sch = changes["schedule"]
        if not isinstance(sch, dict):
            raise Invalid("schedule must be an object")
        _apply_schedule(s, on_offset=sch.get("on_offset"), off_time=sch.get("off_time"), admin=True)
    _save(s)
    return _settings_json(s)


# ---------------------------------------------------------------- schedules
#
# A "schedule" is what the Gemstone app calls a timer: a saved design that
# plays between an on and an off time on some days. Timers belong to one
# controller each, so a schedule on two lights is two timers; timers with the
# same name, days, times and design are shown (and edited) as one schedule.
#
# Timer writes land on the controller a few seconds later, and a second write
# to the same controller while one is pending is dropped, so every write here
# waits for the timer list to show it before the next one goes out. That can
# take longer than a web request may, so the views run saves as a background
# job and the page polls it.

#: What wins when several things want the same lights (measured on a real
#: controller, plus Gemstone's docs): the higher tier, then the latest start,
#: then ``TIE``. When the winner ends the controller goes back to whatever
#: else is still inside its window.
TIER = {"yearly": 3, "autopilot": 2, "weekly": 1, "daily": 1}
TIE = {"weekly": 1}
REPEATS = ("daily", "weekly", "yearly")
SUN_TIMES = ("sunset", "sunrise")
#: Coming up: two weeks, or the season.
PLAN_DAYS = (14, 90)
SUNRISE_FALLBACK = dt.time(7, 0)
TIMER_WAIT = 25.0
_MM_DD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _timers() -> list:
    hg = homegroup_id()
    return cloud.read("timers", 120, lambda c: c.timers_by_homegroup(hg))


def _timer_key(t) -> tuple:
    return (t.name, json.dumps(t.timer_data.to_api(), sort_keys=True), t.pattern.id if t.pattern else "")


def _groups(timers, known) -> list[dict]:
    """Timers bundled into schedules: ``[{cid: timer}]``, in a stable order."""
    groups: list[dict] = []
    open_: dict[tuple, dict] = {}
    for t in sorted(timers, key=lambda t: t.id):
        if t.timer_data is None or t.assignee_id not in known:
            continue
        key = _timer_key(t)
        g = open_.get(key)
        if g is None or t.assignee_id in g:
            g = open_[key] = {}
            groups.append(g)
        g[t.assignee_id] = t
    return groups


def _sched_json(members: dict, ctrls: list[dict]) -> dict:
    timers = sorted(members.values(), key=lambda t: t.id)
    first = timers[0]
    td = first.timer_data
    lights = [c["id"] for c in ctrls if c["id"] in members]
    by_id = {c["id"]: c["name"] for c in ctrls}
    repeat = td.timer_type if td.timer_type in REPEATS else "daily"
    return {
        "id": first.id, "name": first.name or "Schedule",
        "enabled": all(t.enabled for t in timers),
        "timers": {cid: members[cid].id for cid in lights},
        "lights": lights, "light_names": [by_id[c] for c in lights],
        "repeat": repeat,
        "days": sorted(int(d) for d in (td.days_of_week or [])) if repeat != "daily" else [],
        "start": td.start_date if repeat == "yearly" else None,
        "end": td.end_date if repeat == "yearly" else None,
        "on": td.on_time, "on_offset": int(td.on_offset_minutes or 0) if td.on_time in SUN_TIMES else 0,
        "off": td.off_time,
        "design": pattern_json(first.pattern) if first.pattern else None,
    }


def schedules() -> list[dict]:
    """The home group's schedules, as the Plan tab lists them."""
    ctrls = controllers()
    known = {c["id"] for c in ctrls}
    return [_sched_json(g, ctrls) for g in _groups(_timers(), known)]


def _members(sid: str) -> tuple[dict, dict]:
    """(schedule json, {cid: timer}) for a schedule id."""
    ctrls = controllers()
    known = {c["id"] for c in ctrls}
    for g in _groups(_timers(), known):
        out = _sched_json(g, ctrls)
        if out["id"] == sid:
            return out, g
    raise NotFound("That schedule is gone — it may have been changed in the Gemstone app")


# --- when things play

def _sun_fn(tz):
    """``sun(day, "sunset"|"sunrise")`` for this site, cached, with a fallback when there's no location."""
    where = _site()
    cache: dict = {}

    def sun(day: dt.date, kind: str) -> dt.datetime:
        key = (day, kind)
        if key not in cache:
            at = None
            if where is not None:
                at = (sunrise if kind == "sunrise" else sunset)(day, where.latitude, where.longitude, tz)
            if at is None:
                at = dt.datetime.combine(day, SUNRISE_FALLBACK if kind == "sunrise" else GUEST_FALLBACK,
                                         tzinfo=tz)
            cache[key] = at.replace(second=0, microsecond=0)
        return cache[key]
    return sun


def _sched_on(on: str, offset, day: dt.date, sun, tz) -> dt.datetime:
    if on in SUN_TIMES:
        return sun(day, on) + dt.timedelta(minutes=int(offset or 0))
    return dt.datetime.combine(day, _hhmm(on), tzinfo=tz)


def _sched_off(off: str, start: dt.datetime, sun, tz, offset=0) -> dt.datetime:
    """The first ``off`` after ``start`` (an off before the on is the next morning)."""
    day = start.date()
    for d in (day, day + dt.timedelta(days=1)):
        if off in SUN_TIMES:
            at = sun(d, off)
        else:
            at = dt.datetime.combine(d, _hhmm(off), tzinfo=tz)
        at += dt.timedelta(minutes=int(offset or 0))
        if at > start:
            return at
    return start + dt.timedelta(days=1)


def _weekday(day: dt.date) -> int:
    """Gemstone's day numbers: 0 = Sunday."""
    return (day.weekday() + 1) % 7


def occurs(s: dict, day: dt.date) -> bool:
    """Does schedule ``s`` start on ``day``?"""
    repeat = s["repeat"]
    if repeat == "weekly":
        return _weekday(day) in s["days"]
    if repeat == "yearly":
        if s["days"] and _weekday(day) not in s["days"]:
            return False
        md, a, b = day.strftime("%m-%d"), s["start"] or "", s["end"] or ""
        return a <= md <= b if a <= b else (md >= a or md <= b)
    return True


def _days(first: dt.date, last: dt.date):
    d = first
    while d <= last:
        yield d
        d += dt.timedelta(days=1)


def _entries(scheds, first: dt.date, last: dt.date, tz, sun) -> list[dict]:
    """Every time an enabled schedule plays between two dates."""
    out = []
    for s in scheds:
        if not s["enabled"]:
            continue
        design = s.get("design") or {}
        for day in _days(first, last):
            if not occurs(s, day):
                continue
            try:
                start = _sched_on(s["on"], s["on_offset"], day, sun, tz)
                end = _sched_off(s["off"], start, sun, tz)
            except Invalid:
                break
            out.append({"key": s["id"], "kind": s["repeat"], "name": s["name"], "lights": set(s["lights"]),
                        "start": start, "end": end, "schedule": s["id"],
                        "colors": design.get("colors"), "design": design.get("name")})
    return out


def _autopilot_entries(first: dt.date, last: dt.date, tz, sun) -> list[dict]:
    """Autopilot's nights (its event days) between two dates."""
    try:
        s = _settings()
        events = _events() if s.device_ids else []
    except (cloud.CloudError, Invalid):
        return []
    w = s.schedule
    if not events or w is None:
        return []
    out = []
    for day in _days(first, last):
        e = next((e for e in events if _event_day(e, day)), None)
        if e is None:
            continue
        try:
            start = _sched_on(w.on_time or "sunset", w.on_offset_minutes, day, sun, tz)
            end = _sched_off(w.off_time or NIGHT_ENDS.strftime("%H:%M"), start, sun, tz, w.off_offset_minutes)
        except Invalid:
            return out
        color = argb_hex(e.background_color)
        out.append({"key": f"autopilot:{e.event_id}", "kind": "autopilot",
                    "name": f"{emoji(e.icon, e.name)} {e.name}", "lights": set(s.device_ids),
                    "start": start, "end": end, "schedule": None,
                    "colors": [color] if color else None, "design": None})
    return out


def rank(entry: dict) -> tuple:
    """How strongly an entry claims the lights. Bigger wins."""
    return (TIER.get(entry["kind"], 0), entry["start"], TIE.get(entry["kind"], 0), entry["key"])


def winner(entries, at: dt.datetime) -> dict | None:
    """What plays at ``at``: the strongest entry whose window holds it."""
    live = [e for e in entries if e["start"] <= at < e["end"]]
    return max(live, key=rank) if live else None


def segments(entries, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Who wins when between ``start`` and ``end``: ``[{entry, start, end}]``."""
    points = {start, end}
    for e in entries:
        for t in (e["start"], e["end"]):
            if start < t < end:
                points.add(t)
    points = sorted(points)
    out: list[dict] = []
    for a, b in itertools.pairwise(points):
        win = winner(entries, a)
        if win is None:
            continue
        if out and out[-1]["entry"] is win and out[-1]["end"] == a:
            out[-1]["end"] = b
        else:
            out.append({"entry": win, "start": a, "end": b})
    return out


def _seg_json(seg) -> dict:
    e = seg["entry"]
    return {"key": e["key"], "kind": e["kind"], "name": e["name"], "design": e["design"],
            "colors": e["colors"], "schedule": e["schedule"],
            "start": seg["start"].isoformat(), "end": seg["end"].isoformat()}


def _loser_json(e) -> dict:
    return {"key": e["key"], "kind": e["kind"], "name": e["name"],
            "start": e["start"].isoformat(), "end": e["end"].isoformat()}


def _night_start(now) -> dt.date:
    return now.date() if now.time() >= NIGHT_ENDS else now.date() - dt.timedelta(days=1)


def plan(days: int = PLAN_DAYS[0], now=None, scheds=None) -> list[dict]:
    """Coming up: what plays each night, per group of lights, with what it beats.

    Nights where the same things win merge into one row:
    ``[{first, last, groups: [{lights, names, segments, losers}]}]``.
    """
    now = _local_now(now)
    tz = now.tzinfo
    sun = _sun_fn(tz)
    ctrls = controllers()
    scheds = schedules() if scheds is None else scheds
    first = _night_start(now)
    last = first + dt.timedelta(days=days - 1)
    entries = (_entries(scheds, first - dt.timedelta(days=1), last + dt.timedelta(days=1), tz, sun)
               + _autopilot_entries(first - dt.timedelta(days=1), last + dt.timedelta(days=1), tz, sun))
    rows: list[dict] = []
    for night in _days(first, last):
        w0 = dt.datetime.combine(night, NIGHT_ENDS, tzinfo=tz)
        w1 = dt.datetime.combine(night + dt.timedelta(days=1), NIGHT_ENDS, tzinfo=tz)
        if night == first:
            w0 = max(w0, now.replace(second=0, microsecond=0))
        here = [e for e in entries if e["start"] < w1 and e["end"] > w0]
        groups: dict[tuple, dict] = {}
        for c in ctrls:
            mine = [e for e in here if c["id"] in e["lights"]]
            segs = segments(mine, w0, w1)
            won = {s["entry"]["key"] for s in segs}
            losers, seen = [], set()
            for e in sorted(mine, key=lambda e: e["start"]):
                if e["key"] not in won and e["key"] not in seen:
                    seen.add(e["key"])
                    losers.append(e)
            if not segs and not losers:
                continue
            sig = (tuple(s["entry"]["key"] for s in segs), tuple(e["key"] for e in losers))
            g = groups.get(sig)
            if g is None:
                g = groups[sig] = {"lights": [], "names": [], "segments": [_seg_json(s) for s in segs],
                                   "losers": [_loser_json(e) for e in losers]}
            g["lights"].append(c["id"])
            g["names"].append(c["name"])
        sig = tuple(sorted((tuple(g["lights"]), k) for k, g in groups.items()))
        if rows and rows[-1]["_sig"] == sig:
            rows[-1]["last"] = night.isoformat()
            continue
        rows.append({"_sig": sig, "first": night.isoformat(), "last": night.isoformat(),
                     "groups": list(groups.values())})
    for r in rows:
        del r["_sig"]
    return rows


def _first_state(lights) -> dict | None:
    for cid in lights:
        try:
            return state(cid)
        except cloud.CloudError:
            continue
    return None


def schedule_now(now=None) -> dict | None:
    """The Tonight line for schedules (shown to everyone, so names and times only).

    ``{"name", "until"}`` while a schedule is playing, ``{"name", "resumes"}``
    once someone has played something else (or turned the lights off) during
    one, ``{"name", "starts", "until"}`` when a schedule wins later today, or
    None when no schedule has the lights tonight.
    """
    now = _local_now(now)
    scheds = [s for s in schedules() if s["enabled"]]
    if not scheds:
        return None
    tz = now.tzinfo
    sun = _sun_fn(tz)
    day = now.date()
    entries = (_entries(scheds, day - dt.timedelta(days=1), day + dt.timedelta(days=8), tz, sun)
               + _autopilot_entries(day - dt.timedelta(days=1), day + dt.timedelta(days=1), tz, sun))
    upcoming = None
    for c in controllers():
        mine = [e for e in entries if c["id"] in e["lights"]]
        win = winner(mine, now)
        if win is None:
            nxt = min((e for e in mine if now < e["start"] and e["start"].date() == day
                       and e["kind"] != "autopilot" and winner(mine, e["start"]) is e),
                      key=lambda e: e["start"], default=None)
            if nxt and (upcoming is None or nxt["start"] < upcoming["start"]):
                upcoming = nxt
            continue
        if win["kind"] == "autopilot":
            continue
        s = _first_state([c["id"]])
        sched = next(x for x in scheds if x["id"] == win["key"])
        playing = (sched.get("design") or {}).get("id")
        if s and s["on"] and (s["pattern"] or {}).get("id") == playing:
            return {"name": win["name"], "until": win["end"].isoformat()}
        nxt = min((e for e in mine if e["start"] > now and e["kind"] != "autopilot"),
                  key=lambda e: e["start"], default=None)
        if nxt is None:
            return None
        return {"name": nxt["name"], "resumes": nxt["start"].isoformat()}
    if upcoming:
        return {"name": upcoming["name"], "starts": upcoming["start"].isoformat(),
                "until": upcoming["end"].isoformat()}
    return None


# --- checking and saving

def _check_mmdd(value, label) -> str:
    value = str(value or "")
    if not _MM_DD.match(value):
        raise Invalid(f"pick a {label} date")
    try:
        dt.date(2024, int(value[:2]), int(value[3:]))
    except ValueError:
        raise Invalid(f"pick a real {label} date") from None
    return value


def _check_time(value, label, allowed) -> str:
    value = str(value or "")
    if value in allowed:
        return value
    if not _HHMM.match(value):
        raise Invalid(f"pick a {label} time")
    return value


def _check_schedule(body: dict) -> dict:
    """The editor's JSON as a schedule (same shape as :func:`schedules`), checked."""
    if not isinstance(body, dict):
        raise Invalid("schedule must be an object")
    lights = check_ids(body.get("lights"))
    pid = str(body.get("design") or "")
    if not pid:
        raise Invalid("pick a design")
    pattern = _resolve({"id": pid})
    repeat = body.get("repeat", "daily")
    if repeat not in REPEATS:
        raise Invalid("pick how it repeats")
    days: list[int] = []
    if repeat != "daily":
        raw = body.get("days")
        if repeat == "yearly" and not raw:
            raw = list(range(7))
        if not isinstance(raw, list) or not raw:
            raise Invalid("pick at least one day")
        try:
            days = sorted({int(d) for d in raw})
        except (TypeError, ValueError):
            raise Invalid("days must be 0 (Sun) to 6 (Sat)") from None
        if any(not 0 <= d <= 6 for d in days):
            raise Invalid("days must be 0 (Sun) to 6 (Sat)")
    start = end = None
    if repeat == "yearly":
        start = _check_mmdd(body.get("start"), "start")
        end = _check_mmdd(body.get("end") or start, "end")
    on = _check_time(body.get("on"), "on", SUN_TIMES)
    off = _check_time(body.get("off"), "off", ("sunrise",))
    offset = 0
    if on in SUN_TIMES:
        offset = _check_offset(body.get("on_offset", 0))
    if on == off:
        raise Invalid("the off time has to be different from the on time")
    name = " ".join(str(body.get("name") or "").split())[:60] or pattern.name or "Schedule"
    return {"id": str(body.get("id") or "") or None, "name": name, "lights": lights, "repeat": repeat,
            "days": days, "start": start, "end": end, "on": on, "on_offset": offset, "off": off,
            "enabled": bool(body.get("enabled", True)), "pattern": pattern,
            "design": {"id": pattern.id, "name": pattern.name}}


def _clash(s: dict, others: list[dict], today: dt.date) -> dict | None:
    """Another enabled schedule that would start at exactly the same moment on the same lights.

    That's the one case the controller doesn't settle predictably, so saving it is refused.
    """
    for o in others:
        if (o["id"] == s["id"] or not o["enabled"] or TIER[o["repeat"]] != TIER[s["repeat"]]
                or (o["on"], o["on_offset"]) != (s["on"], s["on_offset"])
                or not set(o["lights"]) & set(s["lights"])):
            continue
        if any(occurs(s, d) and occurs(o, d) for d in _days(today, today + dt.timedelta(days=366))):
            return o
    return None


def _check_clash(s: dict, others: list[dict], now=None) -> None:
    other = _clash(s, others, _local_now(now).date())
    if other is not None:
        raise Invalid(f"“{other['name']}” already starts at that time on those lights — "
                      "pick a different on time")


def _timer_data(s: dict):
    from pygemstone import TimerData

    td = TimerData(timer_type=s["repeat"], on_time=s["on"], off_time=s["off"],
                   days_of_week=s["days"] or None, start_date=s["start"], end_date=s["end"],
                   on_offset_minutes=s["on_offset"] if s["on"] in SUN_TIMES else None)
    try:
        td.validate()
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    return td


def _settled(hg: str, tid: str, predicate=None):
    async def wait(client):
        return await client.wait_for_timer(hg, tid, predicate, timeout=TIMER_WAIT)
    return wait


async def _listed(client, hg: str) -> dict:
    return {t.id: t for t in await client.timers_by_homegroup(hg)}


def _create(hg: str, cid: str, tid: str, s: dict, td) -> None:
    async def go(client):
        if tid not in await _listed(client, hg):  # a retry after the first try went through
            await client.create_timer(cid, hg, name=s["name"], on_time=td.on_time, off_time=td.off_time,
                                      pattern=s["pattern"], timer_type=td.timer_type,
                                      days_of_week=td.days_of_week, start_date=td.start_date,
                                      end_date=td.end_date, on_offset_minutes=td.on_offset_minutes,
                                      timer_id=tid)
        await _settled(hg, tid)(client)
    cloud.write(go, invalidate=("timers",))


def _update(hg: str, cid: str, timer, s: dict, td) -> None:
    from pygemstone import Timer

    sent = (s["name"], td.to_api(), s["pattern"].id)

    def landed(t):
        return t.timer_data is not None and (t.name, t.timer_data.to_api(),
                                             t.pattern.id if t.pattern else "") == sent

    async def go(client):
        # Sent enabled: the cloud re-enables on every update anyway, and
        # pygemstone's own disable-after-update would be a second, unwaited write.
        await client.update_timer(Timer(id=timer.id, name=s["name"], homegroup_id=hg, assignee_id=cid,
                                        enabled=True, timer_data=td, pattern=s["pattern"]))
        await _settled(hg, timer.id, landed)(client)
    cloud.write(go, invalidate=("timers",))


def _enable(hg: str, cid: str, tid: str, enabled: bool) -> None:
    async def go(client):
        await client.set_timer_enabled(cid, tid, enabled)
        await _settled(hg, tid, lambda t: t.enabled == enabled)(client)
    cloud.write(go, invalidate=("timers",))


def _delete(hg: str, cid: str, tid: str) -> None:
    async def go(client):
        if tid in await _listed(client, hg):
            await client.delete_timer(cid, tid)
        await client.wait_for_timer(hg, tid, present=False, timeout=TIMER_WAIT)
    cloud.write(go, invalidate=("timers",))


def check_save(body: dict, now=None) -> dict:
    """Check an edit before it's saved (fast, so the editor gets errors straight away)."""
    s = _check_schedule(body)
    current = schedules()
    if s["id"] and not any(o["id"] == s["id"] for o in current):
        raise NotFound("That schedule is gone — it may have been changed in the Gemstone app")
    if s["enabled"]:
        _check_clash(s, current, now)
    return s


def save_schedule(s: dict) -> None:
    """Write a checked schedule: one timer per light, one write at a time."""
    hg = homegroup_id()
    members = _members(s["id"])[1] if s["id"] else {}
    td = _timer_data(s)
    want = (s["name"], json.dumps(td.to_api(), sort_keys=True), s["pattern"].id)
    for cid, timer in members.items():
        if cid not in s["lights"]:
            _delete(hg, cid, timer.id)
    for cid in s["lights"]:
        timer = members.get(cid)
        if timer is None:
            tid = str(uuid.uuid4())
            _create(hg, cid, tid, s, td)
            enabled = True
        else:
            tid = timer.id
            enabled = timer.enabled
            if _timer_key(timer) != want:
                _update(hg, cid, timer, s, td)
                enabled = True
        if enabled != s["enabled"]:
            _enable(hg, cid, tid, s["enabled"])
    cloud.forget("timers")


def check_toggle(sid: str, enabled: bool, now=None) -> dict:
    s, _ = _members(str(sid or ""))
    if enabled:
        _check_clash(dict(s, enabled=True), schedules(), now)
    return s


def toggle_schedule(sid: str, enabled: bool) -> None:
    hg = homegroup_id()
    for cid, timer in _members(sid)[1].items():
        if timer.enabled != enabled:
            _enable(hg, cid, timer.id, enabled)
    cloud.forget("timers")


def delete_schedule(sid: str) -> None:
    hg = homegroup_id()
    for cid, timer in _members(sid)[1].items():
        _delete(hg, cid, timer.id)
    cloud.forget("timers")


# --- the background job

_job_lock = threading.Lock()
_job: dict = {}


def _spawn(fn) -> None:
    threading.Thread(target=fn, name="gemstone-schedules", daemon=True).start()


def job() -> dict | None:
    """The last schedule change: ``{id, what, state: running|done|error, error}``."""
    with _job_lock:
        return dict(_job) if _job else None


def start_job(what: str, fn, *args) -> dict:
    """Run a schedule write in the background (it can take longer than a request may)."""
    with _job_lock:
        if _job.get("state") == "running":
            raise Invalid("Still saving the last change — try again in a moment")
        _job.clear()
        _job.update(id=uuid.uuid4().hex, what=what, state="running", error=None)
        jid = _job["id"]

    def run():
        error = None
        try:
            fn(*args)
        except (Invalid, NotFound) as exc:
            error = str(exc)
        except cloud.NotConfigured:
            error = "Gemstone isn't set up yet"
        except cloud.CloudError as exc:
            logger.warning("Schedule %s failed: %s", what, exc)
            error = "Gemstone didn't confirm the change — check the schedule and try again"
        except Exception:
            logger.exception("Schedule %s failed", what)
            error = "Something went wrong saving that"
        finally:
            if threading.current_thread() is not threading.main_thread():
                try:
                    from django.db import connection

                    connection.close()
                except Exception:
                    logger.debug("Couldn't close the job's DB connection", exc_info=True)
        with _job_lock:
            if _job.get("id") == jid:
                _job.update(state="error" if error else "done", error=error)
    _spawn(run)
    return job()


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


def _clean_name(name) -> str:
    return " ".join(str(name or "").split())[:60]


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

    Guests always save into "Guest designs" and can only update their own
    folder's designs. Admins also pick the folder. Everyone names their design.
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
        name = _clean_name(name) or (existing or {}).get("name") or "Untitled"
    else:
        folders = {f.folder_id: f for f in _folder_list()}
        if not folder_id:
            folder_id = existing["folder"] if existing else ""
        if not folder_id:
            folder_id = _house_folder_id()
            folders = {f.folder_id: f for f in _folder_list()}
        folder = folders.get(folder_id)
        if folder is None or folder.gemstone_managed:
            raise Invalid("pick one of your own folders")
        name = _clean_name(name) or (existing or {}).get("name") or "Untitled"
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


def _house_folder_id() -> str:
    """The first of the house's own folders, made if there isn't one."""
    mine = [f for f in _folder_list() if not f.gemstone_managed and f.name != GUEST_FOLDER and not f.hidden]
    return mine[0].folder_id if mine else new_folder("Our designs")["id"]


def set_favorite(pattern_id, fav, *, admin: bool) -> dict:
    """Heart or un-heart a pattern.

    A saved pattern just has its favourite flag changed. Hearting a library
    pattern saves a copy into the house's folder (a guest's into "Guest
    designs") as a favourite.
    """
    pid = str(pattern_id or "")
    if not _ID.match(pid):
        raise Invalid("pattern id missing")
    fav = bool(fav)
    mine = next((p for p in ours()["patterns"] if p["id"] == pid), None)
    if mine is None:
        lib = next((p for p in library() if p["id"] == pid), None)
        if lib is None:
            raise Invalid("that pattern isn't in your folders or the library")
        if not fav:
            return {"id": pid, "fav": False, "saved": False}
        folder_id = _house_folder_id() if admin else guest_folder_id(create=True)
    else:
        folder_id = mine["folder"]
    with _known_lock:
        raw = copy.deepcopy(_known.get(pid))
    if raw is None:
        raise Invalid("that pattern isn't in your folders or the library")
    # The copy keeps the library pattern's id, so the library list can leave out what's saved.
    pattern = _pattern_cls().from_api(raw)
    cloud.write(lambda c: c.save_pattern(folder_id, pattern, is_favorite=fav), invalidate=("ours",))
    return {"id": pid, "fav": fav, "folder": folder_id, "saved": mine is None, "name": pattern.name}


class NotFound(LookupError):
    """The design (or undo token) isn't there any more (404)."""


DELETE_UNDO_SECONDS = 5 * 60
_deleted_undo: dict[str, dict] = {}  # token -> {"raw", "folder", "fav", "at"}
_deleted_lock = threading.Lock()


def delete_design(design_id, *, admin: bool) -> dict:
    """Delete a user-made design, keeping it briefly so it can be restored.

    Gemstone's own patterns (managed folders, hearted library copies) can't be
    deleted. Admins can delete any other design; guests only Guest designs.
    The lights are left alone even if the design is playing.
    """
    pid = str(design_id or "")
    if not _ID.match(pid):
        raise Invalid("design id missing")
    mine = _design(pid)
    if mine is None:
        raise NotFound("that design is already gone")
    if mine.get("stock"):
        raise Forbidden("Gemstone's own patterns can't be deleted")
    folder_id = mine["folder"]
    if not admin and folder_id != guest_folder_id(create=False):
        raise Forbidden("guests can only delete guest designs")
    with _known_lock:
        raw = copy.deepcopy(_known.get(pid)) or {}
    if not raw:
        raise NotFound("that design is already gone")
    name = mine.get("name") or raw.get("name") or "Untitled"
    cloud.write(lambda c: c.delete_pattern(folder_id, pid), invalidate=("ours",))
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _deleted_lock:
        for t in [t for t, v in _deleted_undo.items() if now - v["at"] > DELETE_UNDO_SECONDS]:
            del _deleted_undo[t]
        _deleted_undo[token] = {"raw": raw, "folder": folder_id, "fav": bool(mine.get("fav")), "at": now}
    log_command("delete", [], design=pid, name=name)
    return {"ok": True, "token": token, "name": name}


def restore_design(token) -> dict:
    """Put back a design :func:`delete_design` removed (within a few minutes)."""
    with _deleted_lock:
        item = _deleted_undo.pop(str(token or ""), None)
    if item is None or time.monotonic() - item["at"] > DELETE_UNDO_SECONDS:
        raise NotFound("too late to undo that")
    pattern = _pattern_cls().from_api(copy.deepcopy(item["raw"]))
    folder_id, fav = item["folder"], item["fav"]
    cloud.write(lambda c: c.save_pattern(folder_id, pattern, is_favorite=fav), invalidate=("ours",))
    _remember(item["raw"])
    log_command("restore", [], design=pattern.id, name=pattern.name)
    return {"ok": True, "id": pattern.id, "name": pattern.name}


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
