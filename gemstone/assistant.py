"""Assistant tools: what the Gemstone lights are doing, and turning them on/off,
to a pattern or a colour, brighter or dimmer.

Guests can do the same from the Tonight page, so the tools are offered in
every mode. Like the page, a guest who turns the lights on has them go off by
10 PM; owner sessions aren't limited. Patterns are played, never created,
edited or deleted, and schedules and Autopilot are only reported.
"""
import datetime as dt
import logging
import threading

from bdh_sdk import ALL_MODES
from bdh_sdk.plugins import OWNER
from bdh_sdk.tools import Tool, ToolError

from . import cloud, service, worker
from .matching import (Ambiguous, NoMatch, Zones, colour_names, find_colour, find_pattern, join, pct, popular,
                       suggest)

log = logging.getLogger(__name__)

LIGHTS_STATUS = "lights_status"
SET_LIGHTS = "set_lights"
STEP = 20
CONTROLLERS_TTL = 300
LIBRARY_TTL = 6 * 3600

DOWN = "Gemstone isn't answering right now. Try again in a minute."
NOT_SET_UP = "The Gemstone lights aren't set up yet."
FAILED = "The Gemstone lights didn't respond."

_warming = threading.Lock()


def _warm():
    """Load the controllers and the pattern library in the background, so listing
    tools stays fast and a pattern only in Gemstone's library can be found."""
    if not cloud.configured() or not _warming.acquire(blocking=False):
        return

    def run():
        try:
            service.controllers()
            service.library()
        except Exception:  # noqa: BLE001 - only a warm-up
            log.debug("gemstone assistant warm-up failed", exc_info=True)
        finally:
            _warming.release()
    threading.Thread(target=run, name="gemstone-assistant-warm", daemon=True).start()


# ---------------------------------------------------------------- zones

def _primary(ctrls):
    return max(ctrls, key=lambda c: c.get("leds", 0))["id"] if len(ctrls) > 1 else None


def _label(c, primary):
    # The main controller is often named after the product ("BDL-Gemstone"): call it the house.
    if c["id"] == primary and "gemstone" in c["name"].casefold():
        return "House"
    return c["name"]


def _zones(ctrls):
    primary = _primary(ctrls)
    return Zones([{"id": c["id"], "name": c["name"], "label": _label(c, primary)} for c in ctrls], primary)


def _controllers():
    try:
        return service.controllers()
    except cloud.NotConfigured:
        raise ToolError(NOT_SET_UP) from None
    except cloud.CloudError:
        raise ToolError(DOWN) from None


def _targets(text):
    ctrls = _controllers()
    try:
        found = _zones(ctrls).resolve(text)
    except (NoMatch, Ambiguous) as exc:
        raise ToolError(str(exc)) from None
    return found


def _the(labels):
    return "the " + join(labels)


def _cap(text):
    return text[:1].upper() + text[1:]


def _are(labels):
    return "are" if len(labels) > 1 else "is"


def _subject(found, total):
    """"The lights" when every zone is meant, else "The House lights" / "The House and Hot Tub lights"."""
    if len(found) == total and total > 1:
        return "The lights", "are"
    return f"The {join([z['label'] for z in found])} lights", "are"


# ---------------------------------------------------------------- times

def _local(value):
    from django.utils import timezone

    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    return timezone.localtime(value) if timezone.is_aware(value) else value


def _clock(value):
    """"4 PM", "4:46 PM" for a datetime, ISO string or "HH:MM"."""
    if isinstance(value, str) and len(value) == 5 and value[2] == ":":
        value = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(value))
    else:
        value = _local(value)
    hour = value.hour % 12 or 12
    ampm = "AM" if value.hour < 12 else "PM"
    return f"{hour} {ampm}" if value.minute == 0 else f"{hour}:{value.minute:02d} {ampm}"


def _until_note(ids=None):
    u = service.until()
    if not u or (ids is not None and not set(ids) & set(u.get("ids") or ())):
        return ""
    return f" They turn off at {_clock(u['at'])}."


# ---------------------------------------------------------------- status

def _state(cid):
    try:
        return service.state(cid)
    except cloud.CloudError:
        return None


def _schedule_line():
    parts = []
    try:
        now = service.schedule_now()
    except Exception:  # noqa: BLE001 - nice to have
        now = None
    if now:
        if now.get("until") and not now.get("starts"):
            parts.append(f"The {now['name']} schedule runs until {_clock(now['until'])}.")
        elif now.get("starts"):
            parts.append(f"The {now['name']} schedule turns them on at {_clock(now['starts'])}"
                         + (f" until {_clock(now['until'])}." if now.get("until") else "."))
        elif now.get("resumes"):
            parts.append(f"The {now['name']} schedule comes back at {_clock(now['resumes'])}.")
    try:
        s = service.schedule()
    except Exception:  # noqa: BLE001
        s = None
    if s and s.get("autopilot") and not now:
        text = "Autopilot is on"
        if s.get("on_at"):
            text += f" from {_clock(s['on_at'])}"
        if s.get("off_time"):
            text += f" {'to' if s.get('on_at') else 'until'} {_clock(s['off_time'])}"
        try:
            ev = service.tonight_event()
        except Exception:  # noqa: BLE001
            ev = None
        if ev and ev.get("name"):
            text += f", showing {ev['name']}"
        parts.append(text + ".")
    if s and s.get("dark"):
        parts.append("They're being kept off tonight.")
    return " ".join(parts)


def _status(args):
    found = _targets(args.get("zone"))
    lines = []
    for z in found:
        s = _state(z["id"])
        if s is None:
            lines.append(f"{z['label']}: not answering")
            continue
        p = s.get("pattern") or {}
        if s["on"]:
            line = f"{z['label']}: on"
            if p.get("name"):
                line += f", {p['name']}"
            if p.get("brightness") is not None:
                line += f", {pct(p['brightness'])}%"
        else:
            line = f"{z['label']}: off"
        lines.append(line)
    text = ". ".join(lines) + "."
    if any((_state(z["id"]) or {}).get("on") for z in found):
        text += _until_note([z["id"] for z in found])
    extra = _schedule_line()
    return text + (" " + extra if extra else "")


# ---------------------------------------------------------------- set

def _catalogue():
    """Saved patterns (with folder names), the play counts, and the library if it is loaded."""
    data = service.ours()
    folders = {f["id"]: f["name"] for f in data.get("folders") or ()}
    ours = [dict(p, folder_name=folders.get(p.get("folder"), "")) for p in data.get("patterns") or ()]
    lib = cloud.peek("library", LIBRARY_TTL)
    if lib is None:
        _warm()
        lib = []
    return ours, lib, service.plays()


def _pick_pattern(query):
    ours, lib, plays = _catalogue()
    got = find_pattern(query, ours, plays)
    if got is None and lib:
        got = find_pattern(query, lib, plays)
    if got is None:
        close = suggest(query, ours, plays=plays) or suggest(query, lib, plays=plays)
        if close:
            raise ToolError(f"There's no pattern called {query!r}. Close ones: {join(close)}.")
        raise ToolError(f"There's no pattern called {query!r}. Some favourites: {join(popular(ours, plays))}.")
    return got


def _level(args, found):
    """``{cid: percent}`` to set, from brightness or brightness_change (+by)."""
    if args.get("brightness") is not None:
        return {z["id"]: int(args["brightness"]) for z in found}
    change = args.get("brightness_change")
    if not change:
        return {}
    by = int(args.get("by") or STEP)
    out = {}
    for z in found:
        s = _state(z["id"]) or {}
        now = pct((s.get("pattern") or {}).get("brightness", 255))
        out[z["id"]] = max(1, min(100, now + by if change == "up" else now - by))
    return out


def _set(args, mode):
    admin = mode == OWNER
    total = len(_controllers())
    found = _targets(args.get("zone"))
    ids = [z["id"] for z in found]
    who, verb = _subject(found, total)
    on = args.get("on")
    pattern, color = args.get("pattern"), args.get("color")
    if args.get("brightness") == 0 and on is None and not (pattern or color):
        on = False
    if on is False:
        if pattern or color or args.get("brightness") or args.get("brightness_change"):
            raise ToolError("Say off on its own, or what to put on.")
        lit = [z["id"] for z in found if (_state(z["id"]) or {}).get("on")]
        if not lit:
            return f"{who} {verb} already off."
        service.power(lit, False, admin=admin)
        worker.poke()
        return f"{who} {verb} off now."
    if pattern and color:
        raise ToolError("Say a pattern or a colour, not both.")
    if not (pattern or color or on or args.get("brightness") is not None or args.get("brightness_change")):
        raise ToolError("Say on, off, a pattern, a colour or how bright.")

    was_off = [z["id"] for z in found if not (_state(z["id"]) or {}).get("on")]
    if pattern or color:
        others = []
        if color:
            hit = find_colour(color)
            if hit:
                ref, name = {"kind": "solid", "color": hit[1], "name": hit[0]}, hit[0].lower()
            else:
                try:
                    best, others = _pick_pattern(color)
                except ToolError:
                    raise ToolError(f"I don't know the colour {color!r}. I can do "
                                    f"{join(colour_names())}.") from None
                ref, name = {"id": best["id"]}, best["name"]
        else:
            best, others = _pick_pattern(pattern)
            ref, name = {"id": best["id"]}, best["name"]
        level = args.get("brightness")
        service.play(ids, ref, brightness=level if level else None)
        if was_off and not admin:
            service._guest_keep_on(was_off)
            worker.poke()
        text = f"{who} {verb} on {name}"
        if level:
            text += f" at {int(level)}%"
        text += "."
        if others:
            text += f" Others: {join(o['name'] for o in others)}."
        if was_off:
            text += _until_note(was_off)
        return text

    if on and was_off:
        service.power(was_off, True, admin=admin)
        worker.poke()
    levels = _level(args, found)
    if levels:
        lit = [cid for cid in levels if cid not in was_off or on]
        if not lit:
            return f"{who} {verb} off. Turn them on first."
        for cid in lit:
            service.brightness([cid], levels[cid])
        values = sorted({levels[cid] for cid in lit})
        text = f"{who} {verb} {'on, ' if on and was_off else ''}at {join(f'{v}%' for v in values)}."
        skipped = [z["label"] for z in found if z["id"] not in lit]
        if skipped:
            text += f" {_cap(_the(skipped))} {_are(skipped)} off."
        return text + (_until_note(was_off) if on and was_off else "")
    names = sorted({(s.get("pattern") or {}).get("name") or "" for s in (_state(i) or {} for i in ids)} - {""})
    if not was_off:
        return f"{who} {verb} already on" + (f", playing {join(names)}." if names else ".")
    return f"{who} {verb} on" + (f", playing {join(names)}." if names else ".") + _until_note(was_off)


# ---------------------------------------------------------------- tools

def _zone_names():
    ctrls = cloud.peek("controllers", CONTROLLERS_TTL)
    if ctrls is None:
        _warm()
        return []
    primary = _primary(ctrls)
    return [_label(c, primary) for c in ctrls]


def tools(mode=None):
    names = _zone_names()
    extra = f" The lights are: {', '.join(names)}." if names else ""
    zone = {"type": "string",
            "description": "Which lights, in the guest's words. Leave it out (or say outside / all) for every "
                           "Gemstone light; 'house' is the lights on the building." + extra}
    return [
        Tool(
            id=LIGHTS_STATUS,
            title="Outside lights status",
            description="What the Gemstone lights (the permanent coloured outdoor lights on the lodge) are "
                        "doing: on or off, the pattern and brightness, and tonight's schedule.",
            params={"type": "object", "properties": {"zone": zone}},
            modes=ALL_MODES,
            readonly=True,
        ),
        Tool(
            id=SET_LIGHTS,
            title="Set outside lights",
            description="Control the Gemstone lights (permanent coloured outdoor lights on the lodge): turn on "
                        "or off, play a saved pattern (e.g. Christmas, Canucks), a solid colour, or set "
                        "brightness. Dim = brightness_change down. Not for indoor lights.",
            params={"type": "object", "properties": {
                "zone": zone,
                "on": {"type": "boolean", "description": "true to turn on, false to turn off"},
                "pattern": {"type": "string",
                            "description": "A pattern name or theme in the guest's words, e.g. 'christmas', "
                                           "'halloween', 'canucks'. Turns the lights on."},
                "color": {"type": "string",
                          "description": "A solid colour, e.g. blue, red, warm white, purple. Turns the "
                                         "lights on."},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100,
                               "description": "Brightness percent (0 turns them off)"},
                "brightness_change": {"type": "string", "enum": ["up", "down"],
                                      "description": "Brighter or dimmer than now, instead of brightness"},
                "by": {"type": "integer", "minimum": 1, "maximum": 100,
                       "description": f"How much brighter or dimmer, in percent (default {STEP})"},
            }},
            modes=ALL_MODES,
        ),
    ]


RUNNERS = {LIGHTS_STATUS: lambda a, m: _status(a), SET_LIGHTS: _set}


def run_tool(tool_id, args, mode=None):
    runner = RUNNERS.get(tool_id)
    if runner is None:
        raise ToolError(f"Unknown tool {tool_id!r}")
    token = service.actor.set("admin" if mode == OWNER else "guest")
    try:
        return runner(args or {}, mode)
    except ToolError:
        raise
    except cloud.NotConfigured:
        raise ToolError(NOT_SET_UP) from None
    except cloud.CloudError:
        raise ToolError(DOWN) from None
    except service.Invalid as exc:
        raise ToolError(str(exc)) from None
    except Exception:  # noqa: BLE001
        log.exception("assistant tool %s failed", tool_id)
        raise ToolError(FAILED) from None
    finally:
        service.actor.reset(token)
