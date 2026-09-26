from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "ix:header"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):  # hidden inline-XBRL facts
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s{2,}", " ", text)


def fetch_page(url: str, max_chars: int = 8000) -> dict:
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 XControlPanel"}, timeout=25, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url
    return {"title": title, "url": str(r.url), "text": html_to_text(r.text)[:max_chars]}
