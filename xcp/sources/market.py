"""Market Desk: the only place numbers come from. No LLM here."""
from __future__ import annotations

import logging
import math
from datetime import date

import httpx
import pandas as pd

from xcp import config, db
from xcp.config import env
from xcp.timeutil import now_ny, today_ny, utcnow

log = logging.getLogger(__name__)
UA = {"User-Agent": "XControlPanel/1.0"}


def _get(url: str, **params) -> dict | list | None:
    try:
        r = httpx.get(url, params=params or None, headers=UA, timeout=20)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def _num(x):
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ BTC

def btc() -> dict:
    stats = _get("https://api.exchange.coinbase.com/products/BTC-USD/stats") or {}
    last, open_ = _num(stats.get("last")), _num(stats.get("open"))
    if last is None:
        spot = _get("https://api.coinbase.com/v2/prices/BTC-USD/spot") or {}
        last = _num((spot.get("data") or {}).get("amount"))
    out = {"price": last, "source": "coinbase"}
    if last and open_:
        out["chg_24h_pct"] = round((last / open_ - 1) * 100, 2)
        out["high_24h"] = _num(stats.get("high"))
        out["low_24h"] = _num(stats.get("low"))
    return out


def btc_candles(days: int = 7) -> pd.DataFrame:
    data = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles", granularity=3600) or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert("America/New_York")
    df = df.sort_values("time")
    return df[df["time"] >= df["time"].max() - pd.Timedelta(days=days)]


def onchain() -> dict:
    out = {}
    fees = _get("https://mempool.space/api/v1/fees/recommended") or {}
    out["fee_fastest_sat_vb"] = fees.get("fastestFee")
    out["fee_hour_sat_vb"] = fees.get("hourFee")
    diff = _get("https://mempool.space/api/v1/difficulty-adjustment") or {}
    if diff:
        out["difficulty_change_est_pct"] = round(_num(diff.get("difficultyChange")) or 0, 2)
        out["blocks_to_retarget"] = diff.get("remainingBlocks")
    hr = _get("https://mempool.space/api/v1/mining/hashrate/3d") or {}
    if hr.get("currentHashrate"):
        out["hashrate_ehs"] = round(hr["currentHashrate"] / 1e18, 1)
    height = _get("https://mempool.space/api/blocks/tip/height")
    if isinstance(height, int):
        out["block_height"] = height
    return {k: v for k, v in out.items() if v is not None}


def fear_greed() -> dict:
    d = _get("https://api.alternative.me/fng/") or {}
    row = (d.get("data") or [{}])[0]
    if not row:
        return {}
    return {"value": _num(row.get("value")), "label": row.get("value_classification")}


def fred() -> dict:
    key = env("FRED_API_KEY")
    if not key:
        return {}
    series = {"us2y": "DGS2", "us10y": "DGS10", "fed_funds": "DFF", "hy_oas": "BAMLH0A0HYM2"}
    out = {}
    for name, sid in series.items():
        d = _get("https://api.stlouisfed.org/fred/series/observations", series_id=sid, api_key=key,
                 file_type="json", sort_order="desc", limit=5) or {}
        for obs in d.get("observations", []):
            v = _num(obs.get("value"))
            if v is not None:
                out[name] = v
                out[f"{name}_date"] = obs.get("date")
                break
    return out


# ------------------------------------------------------------------ equities

def _session_label() -> str:
    n = now_ny()
    if n.weekday() >= 5:
        return "closed"
    hm = n.hour * 60 + n.minute
    if hm < 9 * 60 + 30:
        return "pre"
    if hm < 16 * 60:
        return "regular"
    if hm < 20 * 60:
        return "post"
    return "closed"


def equities(tickers: list[str]) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}
    if not tickers:
        return {}
    out: dict[str, dict] = {}
    try:
        daily = yf.download(tickers, period="10d", interval="1d", group_by="ticker", auto_adjust=False,
                            progress=False, threads=True)
        intra = yf.download(tickers, period="1d", interval="5m", prepost=True, group_by="ticker",
                            auto_adjust=False, progress=False, threads=True)
    except Exception as e:  # yfinance raises many types (rate limits, parse errors)
        log.warning("yfinance download failed: %s", e)
        return {}
    session = _session_label()
    today = today_ny()
    for t in tickers:
        try:
            closes = daily[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if closes.empty:
            continue
        dates = [pd.Timestamp(i).date() for i in closes.index]
        has_today = dates[-1] == today
        if session in ("post", "closed") and has_today:
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2]) if len(closes) > 1 else None
        else:
            prior = [float(c) for c, d in zip(closes, dates) if d < today]
            prev = prior[-1] if prior else None
            last = None
            try:
                series = intra[t]["Close"].dropna()
                if not series.empty:
                    last = float(series.iloc[-1])
            except (KeyError, TypeError):
                pass
            if last is None:
                last = float(closes.iloc[-1])
        row = {"price": round(last, 4), "session": session}
        if prev:
            row["prev_close"] = round(prev, 4)
            row["chg_pct"] = round((last / prev - 1) * 100, 2)
        out[t] = row
    return out


def market_caps(tickers: list[str]) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    for t in tickers:
        try:
            mc = yf.Ticker(t).fast_info.get("marketCap")
            if mc:
                out[t] = float(mc)
        except Exception as e:
            log.info("market cap %s failed: %s", t, e)
    return out


def history(ticker: str, period: str = "3mo") -> pd.Series:
    try:
        import yfinance as yf

        df = yf.download(ticker, period=period, interval="1d", auto_adjust=False, progress=False)
        s = df["Close"]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        return s.dropna()
    except Exception as e:
        log.warning("history %s failed: %s", ticker, e)
        return pd.Series(dtype=float)


# ------------------------------------------------------------------ trends (for analytical posts)

TREND_SYMBOLS = ["BTC-USD", "MSTR", "STRC", "SATA", "ASST", "IBIT", "^GSPC", "GC=F"]
PAR_SYMBOLS = ("STRC", "SATA")


def _pct_since(s: pd.Series, days: int):
    cutoff = s.index[-1] - pd.Timedelta(days=days)
    prior = s[s.index <= cutoff]
    if prior.empty:
        return None
    return round((float(s.iloc[-1]) / float(prior.iloc[-1]) - 1) * 100, 1)


def trends() -> dict:
    """7/30/90-day changes, plus par statistics for the $100-par preferreds."""
    try:
        import yfinance as yf

        df = yf.download(TREND_SYMBOLS + ["^TNX"], period="6mo", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        log.warning("trend download failed: %s", e)
        return {}
    out: dict[str, dict] = {}
    for sym in TREND_SYMBOLS:
        try:
            s = df[sym]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(s) < 15:
            continue
        name = "BTC" if sym == "BTC-USD" else sym.lstrip("^").replace("=F", "")
        row = {f"chg_{d}d_pct": _pct_since(s, d) for d in (7, 30, 90)}
        if sym in PAR_SYMBOLS:
            last30 = s[s.index >= s.index[-1] - pd.Timedelta(days=30)]
            row["days_within_1usd_of_par_30d"] = int(((last30 - 100).abs() <= 1.0).sum())
            row["trading_days_30d"] = int(len(last30))
            row["low_30d"], row["high_30d"] = round(float(last30.min()), 2), round(float(last30.max()), 2)
            row["low_90d"] = round(float(s[s.index >= s.index[-1] - pd.Timedelta(days=90)].min()), 2)
        out[name] = {k: v for k, v in row.items() if v is not None}
    try:  # 10Y yield: change in basis points, not percent
        tnx = df["^TNX"]["Close"].dropna()
        for d in (30, 90):
            prior = tnx[tnx.index <= tnx.index[-1] - pd.Timedelta(days=d)]
            if not prior.empty:
                out.setdefault("US10Y", {})[f"chg_{d}d_bps"] = round((float(tnx.iloc[-1]) - float(prior.iloc[-1])) * 100)
    except (KeyError, TypeError):
        pass
    return out


# ------------------------------------------------------------------ snapshot

def take_snapshot(save: bool = True) -> dict:
    st = config.settings()
    m = st.get("market", {})
    data: dict = {"as_of": utcnow().isoformat(), "as_of_ny": now_ny().strftime("%Y-%m-%d %H:%M ET")}
    data["btc"] = btc()
    data["equities"] = equities(list(m.get("tickers", [])))
    data["onchain"] = onchain()
    data["trends"] = trends()
    data["sentiment"] = {"fear_greed": fear_greed()}
    data["macro"] = fred()
    for sym, key in (("^TNX", "us10y_yahoo"), ("DX-Y.NYB", "dxy")):
        if sym in data["equities"]:
            data["macro"][key] = data["equities"][sym]["price"]

    derived = {}
    eq = data["equities"]
    if (strc := eq.get("STRC")) and strc.get("price"):
        derived["strc_vs_par"] = round(strc["price"] - 100, 2)
        if m.get("strc_annual_rate_pct"):
            derived["strc_rate_pct"] = m["strc_annual_rate_pct"]
            derived["strc_effective_yield_pct"] = round(m["strc_annual_rate_pct"] * 100 / strc["price"], 2)
    if (sata := eq.get("SATA")) and sata.get("price"):
        derived["sata_vs_par"] = round(sata["price"] - 100, 2)
        if m.get("sata_annual_rate_pct"):
            derived["sata_rate_pct"] = m["sata_annual_rate_pct"]
            derived["sata_effective_yield_pct"] = round(m["sata_annual_rate_pct"] * 100 / sata["price"], 2)
    btc_px = data["btc"].get("price")
    holdings = {"MSTR": m.get("mstr_btc_holdings"), "ASST": m.get("asst_btc_holdings")}
    if btc_px and any(holdings.values()):
        caps = market_caps([t for t, h in holdings.items() if h])
        for t, h in holdings.items():
            if h and caps.get(t):
                nav = h * btc_px
                derived[f"{t.lower()}_btc_holdings"] = h
                derived[f"{t.lower()}_btc_nav_usd_bn"] = round(nav / 1e9, 2)
                derived[f"{t.lower()}_basic_mnav"] = round(caps[t] / nav, 2)
    data["derived"] = derived

    if save:
        with db.session() as s:
            s.add(db.Snapshot(data=data))
            s.commit()
    return data


def flatten(data: dict, prefix: str = "") -> dict[str, float | str]:
    """{'btc': {'price': 1}} -> {'btc.price': 1} for fact-checking and prompts."""
    out = {}
    for k, v in (data or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif v is not None:
            out[key] = v
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5
