"""Background worker: turns lights off at "keep on until", keeps them off on a
"lights off tonight" night, makes sure Autopilot's lights went off at its off
time, and cleans up guest designs daily."""
import datetime as dt
import logging
import os
import sys
import threading

from bdh_sdk.storage import app_state

from . import cloud, service

log = logging.getLogger(__name__)

TICK = 30
CLEANUP_EVERY = dt.timedelta(days=1)
#: After a failed cleanup, try again this much later.
CLEANUP_RETRY = dt.timedelta(hours=1)

_wake = threading.Event()
_started = False
_start_lock = threading.Lock()


def _is_runtime(path):
    """``python -m bdh_sdk.runtime`` (the app's container) runs ``bdh_sdk/runtime/__main__.py``."""
    parts = path.replace("\\", "/").split("/")
    return parts[-3:] == ["bdh_sdk", "runtime", "__main__.py"]


def should_start(argv=None):
    """Start only in the server process (the app's container, Daphne, or runserver's reloaded child)."""
    if os.environ.get("BDH_GEMSTONE_WORKER", "").lower() in ("0", "false", "off", "no"):
        return False
    argv = sys.argv if argv is None else argv
    if argv and _is_runtime(argv[0]):
        return True
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py"):
        if argv[1] == "runserver":
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
        return False
    return False


def cleanup_due(now=None) -> bool:
    now = now or dt.datetime.now(dt.timezone.utc)
    nxt = app_state(service.APP).get("cleanup_next")
    return not nxt or dt.datetime.fromisoformat(nxt) <= now


def run_cleanup(now=None) -> int | None:
    """Delete old guest designs if a day has passed. Returns the count, None if not run."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not cleanup_due(now) or not cloud.configured():
        return None
    store = app_state(service.APP)
    try:
        count = service.cleanup(now)
    except (cloud.CloudError, cloud.NotConfigured) as exc:
        log.warning("Guest design cleanup failed: %s", exc)
        store.set("cleanup_next", (now + CLEANUP_RETRY).isoformat())
        return None
    store.set("cleanup_next", (now + CLEANUP_EVERY).isoformat())
    store.set("cleanup_last", {"at": now.isoformat(), "deleted": count})
    if count:
        log.info("Deleted %d guest design(s) older than %d days", count, service.GUEST_KEEP_DAYS)
    return count


def tick(now=None):
    try:
        service.enforce_until(now)
    except Exception:  # noqa: BLE001
        log.exception("keep-on-until check failed")
    try:
        service.enforce_dark(now)
    except Exception:  # noqa: BLE001
        log.exception("lights-off-tonight check failed")
    try:
        service.enforce_autopilot_off(now)
    except Exception:  # noqa: BLE001
        log.exception("autopilot off-time check failed")
    try:
        run_cleanup(now)
    except Exception:  # noqa: BLE001
        log.exception("guest cleanup failed")
    try:
        if cloud.configured():
            service.note_outside_plays(now)
    except Exception:  # noqa: BLE001
        log.exception("outside play check failed")


def _loop():
    # Let the server finish starting before the first cloud call.
    _wake.wait(20)
    while True:
        _wake.clear()
        tick()
        _wake.wait(TICK)


def poke():
    _wake.set()


def start():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="gemstone-worker", daemon=True).start()
