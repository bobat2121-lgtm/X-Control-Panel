"""Discord alerts via webhook.

Every alert has a kind. Only the kinds listed in settings `alerts.discord` reach Discord (by default: a Digital
Credit Report panel going live, plus test messages). Everything else stays in the panel: the newest alert of each
held kind is kept in kv `notify:held:<kind>` so Control Room can show what was held back.
"""
from __future__ import annotations

import json
import logging

import httpx

from xcp.config import env, panel_url

log = logging.getLogger(__name__)
BTC_ORANGE = 0xF7931A

KINDS = {
    "showcase_ready": "🟢 A Digital Credit Report panel is live for its post time (Mon/Wed/Fri)",
    "showcase_status": "Showcase waits, misses, audit problems and preflight warnings",
    "news_priority": "⚡ Priority news as it breaks",
    "watchlist": "🎙 Your 7 accounts just posted",
    "desk": "🗞 Desk brief digests at slot times",
    "drafts": "✍️ AI drafts and captions (drafts mode only)",
    "weekly": "📅 Weekly review and showcase lineup",
    "style": "📚 Style library refreshed",
    "errors": "❌ A scheduled job failed",
    "test": "Test and connection messages (from a button you press)",
}
DEFAULT_KINDS = ["showcase_ready", "test"]


def allowed(kind: str) -> bool:
    from xcp import config

    kinds = config.settings().get("alerts", {}).get("discord", DEFAULT_KINDS)
    return kind in (kinds if isinstance(kinds, list) else DEFAULT_KINDS)


def _hold(kind: str, title: str, description: str) -> None:
    try:
        from xcp import db
        from xcp.timeutil import utcnow

        db.kv_set(f"notify:held:{kind}", {"title": title[:256], "text": description[:1500], "at": utcnow().isoformat()})
    except Exception as e:  # never let bookkeeping break a job
        log.info("could not record held alert: %s", e)


def discord(title: str, description: str = "", fields: list[tuple[str, str]] | None = None,
            color: int = BTC_ORANGE, content: str = "", image: bytes | None = None,
            image_name: str = "panel.png", kind: str = "general") -> bool:
    """Post one embed if `kind` is switched on. With `image`, the PNG is attached and shown in the embed."""
    if not allowed(kind):
        log.info("Discord alert held (kind %s is off): %s", kind, title)
        _hold(kind, title, description)
        return False
    url = env("DISCORD_WEBHOOK_URL")
    if not url:
        log.info("DISCORD_WEBHOOK_URL not set; skipping alert: %s", title)
        return False
    embed = {"title": title[:256], "description": description[:4000], "color": color}
    if panel_url():
        embed["url"] = panel_url()
    if fields:
        embed["fields"] = [{"name": n[:256], "value": (v or "—")[:1024], "inline": False} for n, v in fields[:10]]
    body = {"username": "X Control Panel", "content": content[:2000], "embeds": [embed]}
    try:
        if image:
            embed["image"] = {"url": f"attachment://{image_name}"}
            r = httpx.post(url, data={"payload_json": json.dumps(body)},
                           files={"files[0]": (image_name, image, "image/png")}, timeout=30)
        else:
            r = httpx.post(url, json=body, timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("Discord alert failed: %s", e)
        return False
