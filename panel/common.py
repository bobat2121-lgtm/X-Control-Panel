"""Shared panel helpers: boot, market strip, badges, queueing agent work."""
from __future__ import annotations

import hmac
import html
import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import streamlit as st

from xcp import config, db, gh
from xcp.config import ROOT, env
from xcp.timeutil import fmt_ago, fmt_ny, parse_iso, utcnow

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
         icon: str = "▣") -> None:
    """Page banner: XP title bar, big headline (wrap words in <em> for olive), stat tiles.

    stats: (value, label) or (value, label, hot). headline may contain <em>; everything else is escaped.
    (The only thing that scrolls in the app is the price tape above.)"""
    who = handle()
    tiles = "".join(f'<div class="xcp-stat{" hot" if len(x) > 2 and x[2] else ""}"><div class="v">{esc_html(x[0])}</div>'
                    f'<div class="l">{esc_html(x[1])}</div></div>' for x in (stats or []))
    safe_headline = esc_html(headline).replace("&lt;em&gt;", "<em>").replace("&lt;/em&gt;", "</em>")
    st.markdown(
        f'<section class="xcp-hero"><div class="xcp-tb"><span class="xcp-tb-l">{esc_html(icon)} {esc_html(app)}'
        f'{" — @" + esc_html(who) if who else ""}</span><span class="xcp-tb-r"><i>_</i><i>▢</i><i class="x">✕</i></span></div>'
        f'<div class="xcp-hero-in"><div><div class="xcp-kicker"><span class="dot">●</span> {esc_html(kicker)}'
        f'<span class="caret"></span></div><h1 class="xcp-h1">{safe_headline}</h1>'
        f'<p class="xcp-sub">{esc_html(sub)}</p></div><div class="xcp-stats">{tiles}</div></div></section>',
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


TAPE_SECONDS = 55  # one lap of the price tape
FRESH_MINUTES = {"Prices": 10, "News": 30}  # older than this and the tray dot goes hollow


@st.cache_data(ttl=120, show_spinner=False)
def _live_quotes() -> dict:
    """Shared by every viewer: at most one quote fetch every two minutes."""
    from xcp.sources import market

    try:
        return market.quotes()
    except Exception:  # the tape falls back to the last saved snapshot
        return {}


def tape_data() -> dict:
    """The last saved snapshot, overlaid with live quotes when they came back."""
    snap = db.latest_snapshot()
    base = snap.data if snap else {}
    live = _live_quotes()
    b_btc, l_btc = base.get("btc") or {}, live.get("btc") or {}
    btc = l_btc if l_btc.get("price") else b_btc
    eq = {**(base.get("equities") or {}), **{k: v for k, v in (live.get("equities") or {}).items() if v.get("price")}}
    der = dict(base.get("derived") or {})
    for t in ("MSTR", "ASST"):  # rescale mNAV: market cap moves with the stock, NAV with bitcoin
        key, was, now = f"{t.lower()}_basic_mnav", (base.get("equities") or {}).get(t, {}), eq.get(t, {})
        if der.get(key) and was.get("price") and now.get("price") and b_btc.get("price") and btc.get("price"):
            der[key] = der[key] * (now["price"] / was["price"]) / (btc["price"] / b_btc["price"])
    for t in ("STRC", "SATA"):
        if eq.get(t, {}).get("price"):
            der[f"{t.lower()}_vs_par"] = eq[t]["price"] - 100
    fg = live.get("fear_greed") or (base.get("sentiment") or {}).get("fear_greed") or {}
    fresh = l_btc.get("price") or live.get("equities")
    return {"btc": btc, "equities": eq, "derived": der, "fear_greed": fg,
            "as_of": parse_iso(live.get("as_of") if fresh else base.get("as_of")), "live": bool(fresh)}


def _tick(key: str, value: str, chg=None) -> str:
    move = ""
    if chg is not None:
        move = f'<span class="{"up" if chg >= 0 else "dn"}">{"▲" if chg >= 0 else "▼"}{abs(chg):.2f}%</span>'
    return (f'<span class="xcp-tick"><span class="k">{esc_html(key)}</span><span class="v">{esc_html(value)}</span>'
            f'{move}</span><span class="sep">◆</span>')


NEWS_STALE_MINUTES, KICK_EVERY_MINUTES = 20, 15


def keep_news_fresh(news_at) -> bool:
    """GitHub's cron can run hours late. While the panel is open, stale news dispatches the monitor right away
    (at most every 15 minutes for everyone). Returns True while such a run is in flight."""
    now = utcnow()
    if news_at and now - news_at < timedelta(minutes=NEWS_STALE_MINUTES):
        return False
    kicked = parse_iso(db.kv_get("monitor:kick"))
    if kicked and now - kicked < timedelta(minutes=KICK_EVERY_MINUTES):
        return True
    if not gh.can_dispatch():
        return False
    db.kv_set("monitor:kick", now.isoformat())
    return gh.dispatch("monitor.yml")[0]


def _fresh(label: str, at, updating: bool = False) -> str:
    if updating:
        return (f'<span class="xcp-fresh busy" title="{label} is being refreshed now (last: '
                f'{fmt_ny(at) + " ET" if at else "none"})"><i></i><b>{label}</b>updating…</span>')
    if at is None:
        return f'<span class="xcp-fresh stale" title="{label}: no update on record"><i></i><b>{label}</b>—</span>'
    mins = (utcnow() - at).total_seconds() / 60
    cls = "ok" if mins <= FRESH_MINUTES[label] else "stale"
    return (f'<span class="xcp-fresh {cls}" title="{label} updated {fmt_ny(at)} ET ({fmt_ago(at)})"><i></i><b>{label}</b>'
            f'{fmt_ny(at, "%I:%M %p").lstrip("0")}</span>')


def market_strip() -> None:
    """The price tape: an XP status bar whose prices scroll, with a tray clock saying how fresh prices and news are."""
    d = tape_data()
    eq, der, btc = d["equities"], d["derived"], d["btc"]
    ticks = [_tick("BTC", _fmt_px(btc.get("price"), dec=0), btc.get("chg_24h_pct"))] if btc.get("price") else []
    for t in ("MSTR", "ASST", "STRC", "SATA"):
        if eq.get(t, {}).get("price") is not None:
            ticks.append(_tick(t, _fmt_px(eq[t]["price"]), eq[t].get("chg_pct")))
    for t in ("STRC", "SATA"):
        if der.get(f"{t.lower()}_vs_par") is not None:
            ticks.append(_tick(f"{t} vs par", f"{der[f'{t.lower()}_vs_par']:+.2f}"))
    for t in ("MSTR", "ASST"):
        if der.get(f"{t.lower()}_basic_mnav"):
            ticks.append(_tick(f"{t} mNAV", f"{der[f'{t.lower()}_basic_mnav']:.2f}x"))
    tnx, dxy = eq.get("^TNX", {}), eq.get("DX-Y.NYB", {})
    if tnx.get("price"):
        ticks.append(_tick("US10Y", f"{tnx['price']:.2f}%", tnx.get("chg_pct")))
    if dxy.get("price"):
        ticks.append(_tick("DXY", f"{dxy['price']:.2f}", dxy.get("chg_pct")))
    if d["fear_greed"].get("value") is not None:
        ticks.append(_tick("Fear & Greed", f"{d['fear_greed']['value']:.0f} {d['fear_greed'].get('label', '')}"))
    if not ticks:
        st.caption("No market data yet. It updates with every agent run, or use ↻.")
        return
    news_at = parse_iso((db.kv_get("monitor:last_run") or {}).get("at"))
    updating = keep_news_fresh(news_at)
    delay = -(time.time() % TAPE_SECONDS)  # keeps the tape's position steady across reruns
    html = (f'<div class="xcp-tape"><div class="xcp-ticker" title="Prices scroll; hover to pause">'
            f'<div class="xcp-ticks" style="animation-duration:{TAPE_SECONDS}s;animation-delay:{delay:.1f}s">'
            f'{"".join(ticks) * 2}</div></div><div class="xcp-tray">{_fresh("Prices", d["as_of"])}'
            f'{_fresh("News", news_at, updating)}</div></div>')
    c = st.columns([16, 1], vertical_alignment="center") if is_owner() else [st.container()]
    c[0].markdown(html, unsafe_allow_html=True)
    if is_owner() and c[1].button("↻", help="Refresh prices now and save a full market snapshot",
                                  width="stretch", key="tape_refresh"):
        from xcp.sources import market

        with st.spinner("Refreshing market data…"):
            _live_quotes.clear()
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
