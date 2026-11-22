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


# The cloud only accepts UUID pattern ids. Solid colours get a fixed one built from
# the colour so the UI (solidId in gemstone.js) can tell which swatch is playing.
SOLID_ID_PREFIX = "bd450000-0000-4000-8000-000000"


def solid_id(color: str) -> str:
    return SOLID_ID_PREFIX + int_to_hex(hex_to_int(color))[1:]


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
    _end_dark()
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
    _play(ids, pattern_from_spec(spec, pattern_id=str(uuid.uuid4())))
    _end_dark()
    return {"on": names(ids)}


def power(ids, on: bool, *, admin: bool, now=None) -> dict:
    ids = check_ids(ids)
    for cid in ids:
        cloud.write(lambda c, cid=cid: c.set_on_state(cid, bool(on)), invalidate=(f"state:{cid}",))
    if on:
        _end_dark()
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

def sunset(day: dt.date, lat: float, lon: float, tz) -> dt.datetime | None:
    """Sunset on ``day`` at ``lat``/``lon`` (NOAA's approximation, about a minute out), or None at the poles."""
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
    minutes = 720 - 4 * (lon - math.degrees(math.acos(cos_ha))) - eqtime
    midnight = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc)
    return (midnight + dt.timedelta(minutes=minutes)).astimezone(tz)


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
            did = True
        except cloud.CloudError:
            logger.warning("Couldn't keep %s off for the night; will retry", cid)
    return did


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
