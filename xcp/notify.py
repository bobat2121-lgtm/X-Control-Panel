"""Discord alerts via webhook."""
from __future__ import annotations

import json
import logging

import httpx

from xcp.config import env, panel_url

log = logging.getLogger(__name__)
BTC_ORANGE = 0xF7931A


def discord(title: str, description: str = "", fields: list[tuple[str, str]] | None = None,
            color: int = BTC_ORANGE, content: str = "", image: bytes | None = None,
            image_name: str = "panel.png") -> bool:
    """Post one embed. With `image`, the PNG is attached and shown in the embed (save it from Discord to post)."""
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
