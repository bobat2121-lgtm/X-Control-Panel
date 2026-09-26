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


MENTION_RE = re.compile(r"[@$]\w+")
# "My portfolio is 84% $MSTR", "I sold 5% of my stack", "my average entry: $117" -> the owner writes XX themselves.
POSITION_RE = re.compile(r"\b(?:my|i|i'm|i've)\b[^.\n]{0,50}?\b(?:portfolio|position|stack|allocation|entry|average|"
                         r"avg|cost basis|bought|sold|holdings?|own)\b[^.\n]{0,40}?(?<![A-Za-z])\$?\d[\d,.]*%?", re.I)


AI_TELLS = ("the tell", "here's the thing", "here is the thing", "let that sink in", "quietly", "buckle up", "delve",
            "game-changer", "game changer", "in short,", "overall,", "make no mistake", "it's worth noting",
            "the real story", "not just a")
NOT_JUST_RE = re.compile(r"\b(?:it's|this is|that's|isn't)\s+not\s+(?:just\s+)?[^.\n]{1,40}[,;]\s*(?:it's|it is)\b", re.I)


def robot_flags(parts: list[str]) -> list[str]:
    """Things that make a post read like an AI wrote it (voice v2 rules)."""
    flags: list[str] = []
    text = "\n".join(parts)
    if "—" in text:
        flags.append("🤖 Em dash: use a period, comma or '...' instead")
    lowered = text.lower()
    for phrase in AI_TELLS:
        if phrase in lowered:
            flags.append(f"🤖 AI-sounding phrase: '{phrase.strip(',')}'")
    if NOT_JUST_RE.search(text):
        flags.append("🤖 'It's not X, it's Y' construction")
    if len(parts) == 1 and len(text) <= 500:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 3:
            lens = [len(ln) for ln in lines]
            same_shape = max(lens) <= 1.4 * min(lens) and all(ln.endswith(".") for ln in lines)
            if same_shape:
                flags.append("🤖 Every line has the same length and shape: vary the rhythm")
        if "tl;dr" in lowered or "tldr" in lowered:
            flags.append("🤖 TL;DR on a short post")
    return flags


def check_variant(parts: list[str], snapshot_flat: dict, source_texts: list[str], style: str = "",
                  robot: bool | None = None) -> list[str]:
    flags: list[str] = []
    if robot is None:
        from xcp import config

        robot = bool(config.settings().get("voice", {}).get("robot_check", False))
    if robot:
        flags += robot_flags(parts)
    text = "\n".join(parts)
    if style in ("quote", "reply"):  # guideline: quotes and replies must add something of their own
        own = xtext.URL_RE.sub("", MENTION_RE.sub("", text)).strip()
        if xtext.weighted_len(own) < 50:
            flags.append("Quote/reply adds little of its own: add an analytical point, a joke, or an observation")
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
    for phrase in ("not financial advice", "nfa", "price target", "buy now", "🚨"):
        if phrase in lowered:
            flags.append(f"Avoid: '{phrase}'")
    for m in POSITION_RE.finditer(text):  # the owner fills in their own position sizes
        number = re.search(r"\d[\d,.]*%?$", m.group(0))
        if number and re.fullmatch(r"(19|20)\d\d", number.group(0)):  # "sold my car in February 2026" is a date
            continue
        flags.append(f"Your position size is stated ('{m.group(0).strip()[:60]}'): write XX instead")
    if xtext.HASHTAG_RE.search(text):
        flags.append("Contains hashtags")
    return sorted(set(flags))
