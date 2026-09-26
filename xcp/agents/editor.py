"""Editor: code-level checks on drafts. Every number must trace to the snapshot or a source."""
from __future__ import annotations

import re

from xcp import xtext

NUM_RE = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(%|k|K|m|M|b|B|bn|x)?")
SCALE = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9, "bn": 1e9}


def _numbers_in(text: str) -> list[tuple[str, float]]:
    out = []
    for m in NUM_RE.finditer(text or ""):
        whole = m.group(1).replace(",", "")
        val = float(f"{whole}.{m.group(2)}" if m.group(2) else whole)
        suffix = m.group(3) or ""
        val *= SCALE.get(suffix, 1)
        out.append((m.group(0).strip(), val))
    return out


def _known_values(snapshot_flat: dict, sources: list[str]) -> list[float]:
    vals = []
    for v in snapshot_flat.values():
        if isinstance(v, (int, float)):
            vals += [float(v), abs(float(v))]
    for t in sources:
        vals += [v for _, v in _numbers_in(t)]
    return vals


def _matches(value: float, known: list[float]) -> bool:
    for k in known:
        if k == 0:
            if value == 0:
                return True
            continue
        if abs(value - k) / abs(k) <= 0.015:  # rounding tolerance
            return True
        for scale in (1e3, 1e6, 1e9):  # "$1.2B" vs 1_200_000_000 style mismatch
            if abs(value * scale - k) / abs(k) <= 0.015 or abs(value - k * scale) / abs(k * scale) <= 0.015:
                return True
    return False


def check_variant(parts: list[str], snapshot_flat: dict, source_texts: list[str]) -> list[str]:
    flags: list[str] = []
    text = "\n".join(parts)
    known = _known_values(snapshot_flat, source_texts)
    for raw, val in _numbers_in(text):
        if val <= 31 and "%" not in raw and "$" not in raw:  # counts, days, list numbers
            continue
        if 1990 <= val <= 2100 and "." not in raw:  # years
            continue
        if not _matches(val, known):
            flags.append(f"Unverified number: {raw}")
    first = parts[0] if parts else ""
    if xtext.weighted_len(first) > xtext.FOLD:
        flags.append(f"First post is {xtext.weighted_len(first)} chars; the hook must land before 280")
    for i, p in enumerate(parts[1:], 2):
        if len(parts) > 1 and xtext.weighted_len(p) > xtext.FOLD:
            flags.append(f"Thread part {i} is over 280 chars")
    lowered = text.lower()
    for phrase in ("not financial advice", "nfa", "price target", "buy now", "to the moon", "🚨"):
        if phrase in lowered:
            flags.append(f"Avoid: '{phrase}'")
    if xtext.HASHTAG_RE.search(text):
        flags.append("Contains hashtags")
    return sorted(set(flags))
