"""Configuration: YAML defaults in /config, overridden by edits saved in the DB."""
from __future__ import annotations

import copy
import os
import time
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env", override=False)

YAML_DOCS = ("settings", "pillars", "watchlist")


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@lru_cache(maxsize=None)
def _yaml(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


TEXT_DOCS = {"voice": "voice.md", "guidelines": "guidelines.md"}


def _text_default(name: str) -> str:
    path = CONFIG_DIR / TEXT_DOCS[name]
    return path.read_text(encoding="utf-8") if path.exists() else ""


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            out[k] = deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(override if override is not None else base)


OVERRIDE_TTL = 20.0  # seconds; a page render reads settings hundreds of times, the DB only needs asking once
_overrides: dict[str, tuple[float, object]] = {}


def _override(name: str):
    hit = _overrides.get(name)
    if hit and time.monotonic() - hit[0] < OVERRIDE_TTL:
        return hit[1]
    from xcp import db

    value = db.kv_get(f"config:{name}")
    _overrides[name] = (time.monotonic(), value)
    return value


def forget(key: str | None = None) -> None:
    """Drop cached DB overrides (all, or the one behind kv key 'config:<name>')."""
    if key is None:
        _overrides.clear()
    elif key.startswith("config:"):
        _overrides.pop(key.split(":", 1)[1], None)


def get(name: str):
    """settings | pillars | watchlist -> dict ; voice | guidelines -> str"""
    override = copy.deepcopy(_override(name))
    if name in TEXT_DOCS:
        return override if isinstance(override, str) and override.strip() else _text_default(name)
    default = _yaml(name)
    if name == "watchlist":  # lists replace wholesale
        return override if isinstance(override, dict) else default
    return deep_merge(default, override) if isinstance(override, dict) else copy.deepcopy(default)


def save(name: str, value) -> None:
    from xcp import db

    db.kv_set(f"config:{name}", value)


def reset(name: str) -> None:
    from xcp import db

    db.kv_delete(f"config:{name}")


def settings() -> dict:
    return get("settings")


def pillars() -> dict:
    return get("pillars").get("pillars", {})


def pillar_lane(pillar: str | None) -> str:
    return pillars().get(pillar or "", {}).get("lane", "btc")


def target_group(pillar: str | None) -> str:
    """Map a pillar to its target bucket (all AI pillars share the 'ai' target; a pillar may name its group)."""
    group = pillars().get(pillar or "", {}).get("group")
    if group:
        return group
    return "ai" if pillar_lane(pillar) == "ai" else (pillar or "bitcoin")


def panel_url() -> str:
    return env("PANEL_URL", "") or ""
