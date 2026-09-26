"""X-specific text helpers: weighted length, intents, threads."""
from __future__ import annotations

import re
from urllib.parse import quote

URL_RE = re.compile(r"https?://\S+", re.I)
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
THREAD_SEP = "\n---\n"
FOLD = 280  # posts longer than this collapse behind "Show more" in the timeline

# twitter-text v3: these ranges weigh 1, everything else 2; URLs count 23.
_LIGHT_RANGES = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]
_ZERO_WIDTH = {0x200D, 0xFE0E, 0xFE0F}


def _char_weight(ch: str) -> int:
    cp = ord(ch)
    if cp in _ZERO_WIDTH or 0x1F3FB <= cp <= 0x1F3FF:  # joiners, variation selectors, skin tones
        return 0
    for lo, hi in _LIGHT_RANGES:
        if lo <= cp <= hi:
            return 1
    return 2


def weighted_len(text: str) -> int:
    total = 0
    last = 0
    for m in URL_RE.finditer(text):
        total += sum(_char_weight(c) for c in text[last:m.start()]) + 23
        last = m.end()
    total += sum(_char_weight(c) for c in text[last:])
    return total


def join_parts(parts: list[str]) -> str:
    return THREAD_SEP.join(p.strip() for p in parts if p is not None)


def split_parts(text: str) -> list[str]:
    parts = re.split(r"\n\s*---\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()] or [""]


def split_into_thread(text: str, limit: int = 270) -> list[str]:
    """Deterministic thread splitter: paragraphs first, then sentences."""
    chunks: list[str] = []
    for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        if weighted_len(para) <= limit:
            chunks.append(para)
            continue
        cur = ""
        for sent in re.split(r"(?<=[.!?])\s+", para):
            cand = f"{cur} {sent}".strip()
            if weighted_len(cand) <= limit:
                cur = cand
            else:
                if cur:
                    chunks.append(cur)
                cur = sent
        if cur:
            chunks.append(cur)
    merged: list[str] = []
    for c in chunks:  # re-merge small neighbours
        if merged and weighted_len(merged[-1] + "\n\n" + c) <= limit:
            merged[-1] = merged[-1] + "\n\n" + c
        else:
            merged.append(c)
    if len(merged) > 1:
        merged = [f"{c} ({i}/{len(merged)})" for i, c in enumerate(merged, 1)]
    return merged or [text]


def strip_decoration(text: str) -> str:
    """Remove hashtags and emoji; keep cashtags."""
    out = HASHTAG_RE.sub("", text)
    out = "".join(ch for ch in out if _char_weight(ch) == 1 or ch.isalnum())
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def tweet_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:x|twitter)\.com/[^/]+/status(?:es)?/(\d+)", url)
    return m.group(1) if m else None


def intent_post(text: str) -> str:
    return "https://x.com/intent/post?text=" + quote(text[:6000])


def intent_reply(tweet_id: str, text: str) -> str:
    return f"https://x.com/intent/post?in_reply_to={tweet_id}&text=" + quote(text[:6000])


def intent_quote(tweet_url: str, text: str) -> str:
    return intent_post(f"{text}\n\n{tweet_url}")


def chatgpt_link(prompt: str) -> str:
    return "https://chatgpt.com/?q=" + quote(prompt[:6000])
