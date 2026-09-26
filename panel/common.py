"""Shared panel helpers: boot, market strip, badges, queueing agent work."""
from __future__ import annotations

import html
import os
import subprocess
import sys

import httpx
import streamlit as st

from xcp import config, db
from xcp.config import ROOT, env
from xcp.timeutil import fmt_ago, parse_iso

PILLAR_COLORS = {
    "digital_credit": "#F7931A", "bitcoin": "#E8A33D", "macro": "#5B7083",
    "ai_models": "#7C4DFF", "ai_benchmarks": "#3F51B5", "physical_ai": "#00897B",
}
STATUS_ICONS = {"new": "🆕", "edited": "✏️", "posted": "✅", "dismissed": "🗑", "banked": "⭐", "snoozed": "⏰"}

CSS = """
<style>
.block-container {padding-top: 3.2rem; max-width: 1180px;}
.xcp-badge {display:inline-block; padding:1px 8px; border-radius:999px; font-size:0.75rem; font-weight:600;
            margin-right:4px; color:white;}
.xcp-muted {color:#8899A6; font-size:0.8rem;}
.xcp-src {border-left:3px solid #CFD9DE; padding:4px 10px; margin:4px 0; font-size:0.85rem;}
div[data-testid="stMetricValue"] {font-size:1.15rem;}
</style>
"""


def boot() -> None:
    """Copy Streamlit secrets into env (cloud) and make sure the DB exists."""
    try:
        for k, v in st.secrets.items():
            if isinstance(v, (str, int, float, bool)) and not os.environ.get(k):
                os.environ[k] = str(v)
    except Exception:  # no secrets.toml locally is fine
        pass
    db.engine()
    st.markdown(CSS, unsafe_allow_html=True)


def password_gate() -> bool:
    pw = env("PANEL_PASSWORD")
    if not pw or st.session_state.get("_authed"):
        return True
    st.title("🛰️ X Control Panel")
    entered = st.text_input("Password", type="password")
    if entered and entered == pw:
        st.session_state["_authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    return False


def esc_md(text) -> str:
    """Escape '$' so Streamlit markdown doesn't render '$BTC ... $84k' as LaTeX."""
    return str(text or "").replace("$", "\\$")


def esc_html(text) -> str:
    """For scanned/LLM text inside unsafe_allow_html blocks: HTML-escape and neutralize '$'."""
    return html.escape(str(text or "")).replace("$", "&#36;")


def badge(text: str, color: str = "#5B7083") -> str:
    return f'<span class="xcp-badge" style="background:{color}">{esc_html(text)}</span>'


def pillar_badge(pillar: str) -> str:
    label = config.pillars().get(pillar, {}).get("label", pillar)
    return badge(label, PILLAR_COLORS.get(pillar, "#5B7083"))


def handle() -> str:
    return (config.settings().get("account", {}).get("handle") or "").lstrip("@")


# ------------------------------------------------------------------ market strip

def _fmt_px(v, prefix="$", dec=2):
    return "—" if v is None else f"{prefix}{v:,.{dec}f}"


def market_strip() -> None:
    snap = db.latest_snapshot()
    if not snap:
        st.caption("No market snapshot yet. It updates with every agent run, or use Refresh.")
        return
    d = snap.data
    eq, der, btc = d.get("equities", {}), d.get("derived", {}), d.get("btc", {})
    cols = st.columns([1.2, 1, 1, 1, 1, 1, 1, 0.6])
    cols[0].metric("BTC", _fmt_px(btc.get("price"), dec=0),
                   f"{btc.get('chg_24h_pct', 0):+.2f}% 24h" if btc.get("chg_24h_pct") is not None else None)
    for col, t in zip(cols[1:4], ("MSTR", "STRC", "SATA")):
        row = eq.get(t, {})
        col.metric(t, _fmt_px(row.get("price")), f"{row['chg_pct']:+.2f}%" if "chg_pct" in row else None)
    mnav = der.get("mstr_basic_mnav")
    cols[4].metric("mNAV (basic)", f"{mnav:.2f}x" if mnav else "set holdings")
    tnx = eq.get("^TNX", {})
    cols[5].metric("US 10Y", f"{tnx['price']:.2f}%" if tnx.get("price") else "—",
                   f"{tnx['chg_pct']:+.2f}%" if "chg_pct" in tnx else None, delta_color="off")
    dxy = eq.get("DX-Y.NYB", {})
    cols[6].metric("DXY", f"{dxy['price']:.2f}" if dxy.get("price") else "—",
                   f"{dxy['chg_pct']:+.2f}%" if "chg_pct" in dxy else None, delta_color="off")
    if cols[7].button("↻", help=f"Refresh market data (last: {fmt_ago(parse_iso(d.get('as_of')))})"):
        from xcp.sources import market

        with st.spinner("Refreshing market data…"):
            market.take_snapshot()
        st.rerun()
    strc_gap = der.get("strc_vs_par")
    bits = [f"Snapshot {d.get('as_of_ny', '')}"]
    if strc_gap is not None:
        bits.append(f"STRC vs par {strc_gap:+.2f}")
    if der.get("sata_vs_par") is not None:
        bits.append(f"SATA vs par {der['sata_vs_par']:+.2f}")
    fg = d.get("sentiment", {}).get("fear_greed", {})
    if fg.get("value") is not None:
        bits.append(f"Fear & Greed {fg['value']:.0f} ({fg.get('label', '')})")
    oc = d.get("onchain", {})
    if oc.get("hashrate_ehs"):
        bits.append(f"Hashrate {oc['hashrate_ehs']} EH/s")
    st.caption(" · ".join(bits))


# ------------------------------------------------------------------ agent queue

def dispatch_agent() -> str:
    """Kick the cloud agent (GitHub Actions workflow_dispatch). Locally, run it in the background."""
    token, repo = env("GH_DISPATCH_TOKEN"), env("GITHUB_REPO")
    if token and repo:
        try:
            r = httpx.post(f"https://api.github.com/repos/{repo}/actions/workflows/agent.yml/dispatches",
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                           json={"ref": env("GITHUB_BRANCH", "main")}, timeout=15)
            if r.status_code in (201, 204):
                return "Agent started in the cloud. Results appear in about 1–3 minutes."
            return f"Queued, but the cloud dispatch failed ({r.status_code}). It will run on the next schedule."
        except httpx.HTTPError as e:
            return f"Queued, but the cloud dispatch failed ({e}). It will run on the next schedule."
    if env("LOCAL_AGENT") == "1":
        subprocess.Popen([sys.executable, "-m", "jobs.run", "auto"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Agent started locally. Refresh in a minute."
    return "Queued. It will run on the next scheduled agent run (set GH_DISPATCH_TOKEN to run instantly)."


def enqueue(kind: str, payload: dict, kick: bool = True) -> None:
    with db.session() as s:
        s.add(db.Request(kind=kind, payload=payload))
        s.commit()
    msg = dispatch_agent() if kick else "Queued."
    st.toast(msg, icon="🛰️")
