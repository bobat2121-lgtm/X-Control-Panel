"""Shared panel helpers: boot, market strip, badges, queueing agent work."""
from __future__ import annotations

import hmac
import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

from xcp import config, db, gh
from xcp.config import ROOT, env
from xcp.timeutil import fmt_ago, parse_iso

# Every pillar badge uses the same palette; a glyph tells them apart (olive stays reserved for emphasis).
PILLAR_GLYPHS = {"digital_credit": "◆", "bitcoin": "₿", "macro": "◷", "stablecoins": "＄", "legislation": "§",
                 "ai_models": "◈", "ai_benchmarks": "▤", "ai_payments": "⇄", "physical_ai": "⚙"}
STATUS_ICONS = {"new": "🆕", "edited": "✏️", "posted": "✅", "dismissed": "🗑", "banked": "⭐", "snoozed": "⏰"}
TONES = ("ink", "tan", "paper", "olive", "hot", "new")
CSS_PATH = Path(__file__).with_name("theme.css")


def boot() -> None:
    """Copy Streamlit secrets into env (cloud) and make sure the DB exists."""
    try:
        for k, v in st.secrets.items():
            if isinstance(v, (str, int, float, bool)) and not os.environ.get(k):
                os.environ[k] = str(v)
    except Exception:  # no secrets.toml locally is fine
        pass
    db.engine()
    st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def is_owner() -> bool:
    """No PANEL_PASSWORD (local dev) = full control. Otherwise visitors are read-only until unlocked."""
    return not env("PANEL_PASSWORD") or bool(st.session_state.get("_owner"))


def _unlock() -> None:
    entered = st.session_state.get("_pw", "")
    if entered and hmac.compare_digest(entered.encode(), (env("PANEL_PASSWORD") or "").encode()):
        st.session_state["_owner"] = True
    else:
        time.sleep(1.5)  # slow down guessing
        st.session_state["_pw_err"] = True
    st.session_state["_pw"] = ""


def owner_bar() -> None:
    if not env("PANEL_PASSWORD"):
        return
    c = st.columns([8, 1])
    if st.session_state.get("_owner"):
        c[0].caption("🔓 Owner mode: full control")
        if c[1].button("🔒 Lock", width="stretch"):
            st.session_state["_owner"] = False
            st.rerun()
    else:
        c[0].caption("👁 View only")
        with c[1].popover("🔑 Owner", width="stretch"):
            st.text_input("Owner password", type="password", key="_pw", on_change=_unlock)
            if st.session_state.pop("_pw_err", False):
                st.error("Wrong password")


def esc_md(text) -> str:
    """Escape '$' so Streamlit markdown doesn't render '$BTC ... $84k' as LaTeX."""
    return ("" if text is None else str(text)).replace("$", "\\$")


def esc_html(text) -> str:
    """For scanned/LLM text inside unsafe_allow_html blocks: HTML-escape and neutralize '$'."""
    return html.escape("" if text is None else str(text)).replace("$", "&#36;")


def badge(text: str, tone: str = "ink") -> str:
    """tone: ink | tan | paper | olive | hot (olive, pulsing) | new (blinking)."""
    return f'<span class="xcp-badge xcp-b-{tone if tone in TONES else "ink"}">{esc_html(text)}</span>'


def pillar_badge(pillar: str) -> str:
    label = config.pillars().get(pillar, {}).get("label", pillar)
    return badge(f"{PILLAR_GLYPHS.get(pillar, '•')} {label}", "tan")


def hero(app: str, headline: str, sub: str = "", stats: list[tuple] | None = None, kicker: str = "",
         ticker: list[str] | None = None, icon: str = "▣") -> None:
    """Page banner: XP title bar, big headline (wrap words in <em> for olive), stat tiles, scrolling ticker.

    stats: (value, label) or (value, label, hot). ticker items are plain text; prefix "NEW:" to flag one.
    headline may contain <em>; everything else is escaped."""
    who = handle()
    tiles = "".join(f'<div class="xcp-stat{" hot" if len(x) > 2 and x[2] else ""}"><div class="v">{esc_html(x[0])}</div>'
                    f'<div class="l">{esc_html(x[1])}</div></div>' for x in (stats or []))
    items = []
    for t in ticker or []:
        new = t.startswith("NEW:")
        body = esc_html(t[4:].strip() if new else t)
        tag = '<b class="new">NEW!</b>' if new else ""
        items.append(f'<span>{tag}{body}</span><span class="sep">✦</span>')
    marquee = (f'<div class="xcp-marquee"><div class="xcp-track">{"".join(items) * 2}</div></div>' if items else "")
    safe_headline = esc_html(headline).replace("&lt;em&gt;", "<em>").replace("&lt;/em&gt;", "</em>")
    st.markdown(
        f'<section class="xcp-hero"><div class="xcp-tb"><span class="xcp-tb-l">{esc_html(icon)} {esc_html(app)}'
        f'{" — @" + esc_html(who) if who else ""}</span><span class="xcp-tb-r"><i>_</i><i>▢</i><i class="x">✕</i></span></div>'
        f'<div class="xcp-hero-in"><div><div class="xcp-kicker"><span class="dot">●</span> {esc_html(kicker)}'
        f'<span class="caret"></span></div><h1 class="xcp-h1">{safe_headline}</h1>'
        f'<p class="xcp-sub">{esc_html(sub)}</p></div><div class="xcp-stats">{tiles}</div></div>{marquee}</section>',
        unsafe_allow_html=True)


def section(title: str, note: str = "") -> None:
    st.markdown(f'<div class="xcp-sec"><span class="bar"></span><h3>{esc_html(title)}</h3>'
                f'<span class="note">{esc_html(note)}</span></div>', unsafe_allow_html=True)


def card_key(prefix: str, raw) -> str:
    """A container key the theme styles: 'card…' (window) or 'hot…' (olive, pulsing)."""
    return prefix + "_" + re.sub(r"[^A-Za-z0-9_-]", "-", str(raw))


def handle() -> str:
    return (config.settings().get("account", {}).get("handle") or "").lstrip("@")


# ------------------------------------------------------------------ market strip

def _fmt_px(v, prefix="$", dec=2):
    return "—" if v is None else f"{prefix}{v:,.{dec}f}"


def _cell(key: str, value: str, chg=None) -> str:
    move = ""
    if chg is not None:
        move = f'<span class="{"up" if chg >= 0 else "dn"}">{"▲" if chg >= 0 else "▼"}{abs(chg):.2f}%</span>'
    return f'<div class="xcp-cell"><span class="k">{esc_html(key)}</span><span class="v">{esc_html(value)}</span>{move}</div>'


def market_strip() -> None:
    """The market tape: an XP-style status bar of sunken cells under the nav."""
    snap = db.latest_snapshot()
    if not snap:
        st.caption("No market snapshot yet. It updates with every agent run, or use Refresh.")
        return
    d = snap.data
    eq, der, btc = d.get("equities", {}), d.get("derived", {}), d.get("btc", {})
    cells = [_cell("BTC", _fmt_px(btc.get("price"), dec=0), btc.get("chg_24h_pct"))]
    for t in ("MSTR", "ASST", "STRC", "SATA"):
        row = eq.get(t, {})
        if row.get("price") is not None:
            cells.append(_cell(t, _fmt_px(row.get("price")), row.get("chg_pct")))
    for t, key in (("MSTR", "mstr_basic_mnav"), ("ASST", "asst_basic_mnav")):
        if der.get(key):
            cells.append(_cell(f"{t} mNAV", f"{der[key]:.2f}x"))
    tnx, dxy = eq.get("^TNX", {}), eq.get("DX-Y.NYB", {})
    if tnx.get("price"):
        cells.append(_cell("US10Y", f"{tnx['price']:.2f}%", tnx.get("chg_pct")))
    if dxy.get("price"):
        cells.append(_cell("DXY", f"{dxy['price']:.2f}", dxy.get("chg_pct")))
    bits = [f"snapshot {d.get('as_of_ny', '')}"]
    for t in ("strc", "sata"):
        if der.get(f"{t}_vs_par") is not None:
            bits.append(f"{t.upper()} vs par {der[f'{t}_vs_par']:+.2f}")
    fg = d.get("sentiment", {}).get("fear_greed", {})
    if fg.get("value") is not None:
        bits.append(f"F&G {fg['value']:.0f} {fg.get('label', '')}")
    cells.append(f'<div class="xcp-cell meta">{esc_html(" · ".join(bits))}</div>')
    c = st.columns([16, 1]) if is_owner() else [st.container()]
    c[0].markdown(f'<div class="xcp-tape">{"".join(cells)}</div>', unsafe_allow_html=True)
    if is_owner() and c[1].button("↻", help=f"Refresh market data (last: {fmt_ago(parse_iso(d.get('as_of')))})",
                                  width="stretch", key="tape_refresh"):
        from xcp.sources import market

        with st.spinner("Refreshing market data…"):
            market.take_snapshot()
        st.rerun()


# ------------------------------------------------------------------ agent queue

def dispatch_agent() -> str:
    """Kick the cloud agent (GitHub Actions workflow_dispatch). Locally, run it in the background."""
    if gh.can_dispatch():
        ok, why = gh.dispatch("agent.yml")
        if ok:
            return "Agent started in the cloud. Results appear in about 1–3 minutes."
        return f"Queued, but the cloud dispatch failed ({why}). It will run on the next schedule."
    if env("LOCAL_AGENT") == "1":
        subprocess.Popen([sys.executable, "-m", "jobs.run", "auto"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Agent started locally. Refresh in a minute."
    return "Queued. It will run on the next scheduled agent run (set GH_DISPATCH_TOKEN to run instantly)."


def enqueue(kind: str, payload: dict, kick: bool = True) -> None:
    if not is_owner():
        st.toast("View only. Unlock with 🔑 Owner.", icon="👁")
        return
    with db.session() as s:
        s.add(db.Request(kind=kind, payload=payload))
        s.commit()
    msg = dispatch_agent() if kick else "Queued."
    st.toast(msg, icon="🛰️")
