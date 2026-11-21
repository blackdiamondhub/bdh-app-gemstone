"""Bridge from Django's sync world to pygemstone's async client.

One asyncio loop runs on a daemon thread and owns one logged-in
``GemstoneClient``. Writes go out one at a time with a gap between them,
because the Gemstone cloud silently drops a command that overlaps the
previous one. Reads are cached for a short while.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from bdh_sdk import secrets
from bdh_sdk.settings import app_setting

APP = "gemstone"
logger = logging.getLogger(__name__)

#: Seconds between two writes to the cloud.
WRITE_GAP = 2.0
#: Seconds to wait for one cloud call.
CALL_TIMEOUT = 45.0

#: Tests replace this with ``async def factory(email, password) -> client``.
client_factory: Callable[[str, str], Awaitable[Any]] | None = None


class NotConfigured(RuntimeError):
    """No Gemstone email/password has been entered."""


class CloudError(RuntimeError):
    """The Gemstone cloud refused or failed a call."""


def credentials() -> tuple[str, str]:
    email = app_setting(APP, "gemstone_email", "", legacy=("GEMSTONE_EMAIL",)) or ""
    password = secrets.get("gemstone_password", app=APP, legacy=("GEMSTONE_PASSWORD",)) or ""
    return str(email).strip(), password


def configured() -> bool:
    email, password = credentials()
    return bool(email and password)


async def _default_factory(email: str, password: str):
    import aiohttp
    from pygemstone import GemstoneClient

    # trust_env so the container's egress proxy (HTTPS_PROXY) is used.
    session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30))
    client = GemstoneClient(email, password, session=session)
    try:
        await client.__aenter__()
        await client.login()
    except BaseException:
        await session.close()
        raise
    client._bdh_session = session  # closed by _close()
    return client


class _Bridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._creds: tuple[str, str] | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run():
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                threading.Thread(target=run, name="gemstone-cloud", daemon=True).start()
                ready.wait(5)
                self._loop = loop
            return self._loop

    def submit(self, coro) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(CALL_TIMEOUT)
        except TimeoutError:
            future.cancel()
            raise CloudError("The Gemstone cloud didn't answer in time")

    async def _client_for(self, creds):
        if self._client is not None and self._creds == creds:
            return self._client
        await self._close()
        factory = client_factory or _default_factory
        self._client = await factory(*creds)
        self._creds = creds
        return self._client

    async def _close(self):
        client, self._client, self._creds = self._client, None, None
        if client is None:
            return
        session = getattr(client, "_bdh_session", None)
        try:
            if hasattr(client, "__aexit__"):
                await client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        if session is not None:
            await session.close()

    async def _call(self, fn, creds):
        try:
            client = await self._client_for(creds)
            return await fn(client)
        except NotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_value_error(exc):
                raise
            logger.warning("Gemstone call failed (%s); logging in again and retrying once", exc)
            await self._close()
            client = await self._client_for(creds)
            return await fn(client)

    def call(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        creds = credentials()
        if not (creds[0] and creds[1]):
            raise NotConfigured("Gemstone email and password aren't set")
        try:
            return self.submit(self._call(fn, creds))
        except (NotConfigured, CloudError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise CloudError(str(exc) or exc.__class__.__name__) from exc

    def reset(self):
        if self._loop is not None and self._loop.is_running():
            try:
                self.submit(self._close())
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._creds = None


def _is_value_error(exc) -> bool:
    try:
        from pygemstone import GemstoneValueError
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (GemstoneValueError, ValueError))


_bridge = _Bridge()

_write_lock = threading.Lock()
_last_write = 0.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def read(key: str, ttl: float, fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """``fn(client)``'s result, cached under ``key`` for ``ttl`` seconds."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = _bridge.call(fn)
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def write(fn: Callable[[Any], Awaitable[Any]], *, invalidate: tuple[str, ...] = ()) -> Any:
    """Run ``fn(client)`` as the only write in flight, ``WRITE_GAP`` after the last one."""
    global _last_write
    with _write_lock:
        wait = _last_write + WRITE_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return _bridge.call(fn)
        finally:
            _last_write = time.monotonic()
            if invalidate:
                forget(*invalidate)


def forget(*prefixes: str) -> None:
    """Drop cached reads whose key starts with any of ``prefixes`` (all if none)."""
    with _cache_lock:
        if not prefixes:
            _cache.clear()
            return
        for key in [k for k in _cache if k.startswith(prefixes)]:
            del _cache[key]


def reset() -> None:
    """Forget the client, the cache and the write clock (tests, new credentials)."""
    global _last_write
    _bridge.reset()
    forget()
    _last_write = 0.0
