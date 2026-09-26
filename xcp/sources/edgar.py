"""SEC EDGAR filings (Strategy, Strive). Free; SEC asks for a contact in the User-Agent."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import httpx

from xcp.config import env
from xcp.sources.web import html_to_text
from xcp.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)


def _headers() -> dict:
    return {"User-Agent": env("SEC_USER_AGENT", "XControlPanel admin@example.com"), "Accept-Encoding": "gzip"}


def recent_filings(cik: int, name: str, forms: list[str], days: int = 4, excerpt_chars: int = 5000,
                   known: set[str] | None = None) -> list[dict]:
    """Recent filings. `known` item ids (edgar:<accession>) are skipped without downloading the document."""
    try:
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=_headers(), timeout=30)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
    except (httpx.HTTPError, ValueError) as e:
        log.warning("EDGAR %s failed: %s", name, e)
        return []
    cutoff = utcnow() - timedelta(days=days)
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if forms and form not in forms:
            continue
        accepted = parse_iso(recent["acceptanceDateTime"][i])
        if not accepted or accepted < cutoff:
            continue
        acc = recent["accessionNumber"][i]
        if known and f"edgar:{acc}" in known:
            continue
        doc = recent["primaryDocument"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        items = recent.get("items", [""] * (i + 1))[i]
        excerpt = ""
        try:
            d = httpx.get(url, headers=_headers(), timeout=30)
            d.raise_for_status()
            body = html_to_text(d.text)
            m = re.search(r"\bItem\s+\d\.\d{2}\b", body)  # skip the cover page boilerplate
            excerpt = (body[m.start():] if m else body)[:excerpt_chars]
        except httpx.HTTPError as e:
            log.info("EDGAR doc fetch failed %s: %s", url, e)
        out.append({
            "id": f"edgar:{acc}",
            "company": name,
            "form": form,
            "items": items,
            "accepted": accepted,
            "url": url,
            "text": f"{name} filed {form} (items {items}) on {accepted:%Y-%m-%d %H:%M} UTC.\n{excerpt}",
        })
    return out
