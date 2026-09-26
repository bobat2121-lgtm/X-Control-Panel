"""Shared caches for the panel: one copy per server process, used by every viewer.

A value older than its TTL is still handed back right away while a background thread fetches a fresh one
(stale-while-revalidate), so a click never waits on the database or a price feed. Only the very first read of a
key waits. `update()` edits cached values in place (after your own Save/Pass), `bust()` drops them when the next
read must come straight from the source (Check now, Refresh).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)
_lock = threading.Lock()
_store: dict[Any, list] = {}  # key -> [value, fetched_at, generation]
_busy: set = set()
_gen: dict[Any, int] = {}


def _group(key) -> Any:
    return key[0] if isinstance(key, tuple) else key


def get(key, fn: Callable[[], Any], ttl: float, wait: bool = True) -> Any:
    """wait=False: on the very first read, start the fetch in the background and return None meanwhile."""
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        stale = hit is not None and now - hit[1] > ttl and key not in _busy
        first_bg = hit is None and not wait and key not in _busy
        if stale or first_bg:
            _busy.add(key)
        elif hit is None and not wait:
            return None  # already being fetched
        gen = _gen.get(_group(key), 0)
    if first_bg:
        threading.Thread(target=_refresh, args=(key, fn, gen), daemon=True).start()
        return None
    if hit is None:
        val = fn()
        with _lock:
            if _gen.get(_group(key), 0) == gen:
                _store[key] = [val, time.monotonic(), gen]
        return val
    if stale:
        threading.Thread(target=_refresh, args=(key, fn, gen), daemon=True).start()
    return hit[0]


def _refresh(key, fn: Callable[[], Any], gen: int) -> None:
    try:
        val = fn()
        with _lock:
            if _gen.get(_group(key), 0) == gen:  # nobody busted this group while we were fetching
                _store[key] = [val, time.monotonic(), gen]
    except Exception as e:  # keep serving the old value
        log.warning("background refresh of %s failed: %s", key, e)
    finally:
        with _lock:
            _busy.discard(key)


def update(group: str, fn: Callable[[Any], None]) -> None:
    """Apply fn to every cached value in a group (e.g. mark one story passed everywhere it's cached)."""
    with _lock:
        _gen[group] = _gen.get(group, 0) + 1  # a fetch already in flight may predate this change: drop its result
        values = [v[0] for k, v in _store.items() if _group(k) == group]
    for v in values:
        fn(v)


def bust(*groups: str) -> None:
    with _lock:
        for g in groups:
            _gen[g] = _gen.get(g, 0) + 1
            for k in [k for k in _store if _group(k) == g]:
                del _store[k]
