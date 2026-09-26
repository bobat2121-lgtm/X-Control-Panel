"""Attachable chart PNGs for drafts (chart_hint)."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from xcp import config  # noqa: E402
from xcp.sources import market  # noqa: E402

CHARTS = {
    "btc_7d": "BTC: last 7 days",
    "strc_vs_par": "STRC vs $100 par (3 months)",
    "sata_vs_par": "SATA vs $100 par (3 months)",
    "mstr_vs_btc": "MSTR vs BTC (3 months, indexed)",
    "digital_credit_yields": "Digital credit effective yields vs 10Y",
}
ORANGE, INK, MUTED = "#F7931A", "#14171A", "#8899A6"


def _style(ax, title: str):
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=INK)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(colors=MUTED)


def render(kind: str, handle: str = "") -> tuple[bytes | None, str]:
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    try:
        if kind == "btc_7d":
            df = market.btc_candles(7)
            if df.empty:
                return None, "No BTC candle data"
            ax.plot(df["time"], df["close"], color=ORANGE, lw=2)
            _style(ax, CHARTS[kind])
            ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
        elif kind in ("strc_vs_par", "sata_vs_par"):
            t = kind.split("_")[0].upper()
            s = market.history(t, "3mo")
            if s.empty:
                return None, f"No {t} history"
            ax.plot(s.index, s.values, color=ORANGE, lw=2, label=t)
            ax.axhline(100, color=INK, ls="--", lw=1, label="$100 par")
            ax.legend(frameon=False)
            _style(ax, CHARTS[kind])
        elif kind == "mstr_vs_btc":
            a, b = market.history("MSTR", "3mo"), market.history("BTC-USD", "3mo")
            if a.empty or b.empty:
                return None, "No history"
            ax.plot(a.index, a / a.iloc[0] * 100, color=INK, lw=2, label="MSTR")
            ax.plot(b.index, b / b.iloc[0] * 100, color=ORANGE, lw=2, label="BTC")
            ax.legend(frameon=False)
            _style(ax, CHARTS[kind])
        elif kind == "digital_credit_yields":
            m = config.settings().get("market", {})
            snap = market.take_snapshot(save=False)
            eq, bars = snap.get("equities", {}), {}
            for t, key in (("STRC", "strc_annual_rate_pct"), ("SATA", "sata_annual_rate_pct")):
                if m.get(key) and eq.get(t, {}).get("price"):
                    bars[t] = m[key] * 100 / eq[t]["price"]
            if "^TNX" in eq:
                bars["US 10Y"] = eq["^TNX"]["price"]
            if not bars:
                return None, "Set STRC/SATA rates in Control Room → Market inputs"
            ax.bar(list(bars), list(bars.values()), color=[ORANGE if k != "US 10Y" else MUTED for k in bars])
            for i, v in enumerate(bars.values()):
                ax.text(i, v, f"{v:.2f}%", ha="center", va="bottom", fontweight="bold")
            _style(ax, CHARTS[kind])
        else:
            return None, "No chart"
        if handle:
            fig.text(0.99, 0.01, f"@{handle}", ha="right", va="bottom", color=MUTED, fontsize=9)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        return buf.getvalue(), CHARTS.get(kind, kind)
    finally:
        plt.close(fig)
