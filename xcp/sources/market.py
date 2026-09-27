"""Market Desk: the only place numbers come from. No LLM here."""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

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

def coinbase(product: str) -> dict:
    """Last price and 24-hour move for a Coinbase pair (BTC-USD, ETH-USD, ZEC-USD)."""
    stats = _get(f"https://api.exchange.coinbase.com/products/{product}/stats") or {}
    last, open_ = _num(stats.get("last")), _num(stats.get("open"))
    if last is None:
        spot = _get(f"https://api.coinbase.com/v2/prices/{product}/spot") or {}
        last = _num((spot.get("data") or {}).get("amount"))
    out = {"price": last, "source": "coinbase"}
    if last and open_:
        out["chg_24h_pct"] = round((last / open_ - 1) * 100, 2)
        out["high_24h"] = _num(stats.get("high"))
        out["low_24h"] = _num(stats.get("low"))
    return out


def btc() -> dict:
    return coinbase("BTC-USD")


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

def _inputs(m: dict) -> tuple[dict, dict]:
    """Holdings and dividend rates: automatic (8-K feed, strategy.com, strive.com) unless turned off in
    Control Room; the manual values there are the fallback. Returns (market settings to use, provenance)."""
    m = dict(m)
    provenance: dict = {}
    auto = {}
    if m.get("auto_inputs", True):
        from xcp.sources import issuers

        try:
            auto = issuers.refresh()["values"]
        except Exception as e:  # never let this block a snapshot
            log.warning("issuer inputs failed: %s", e)
            auto = (issuers.last().get("values") or {})
    for key in ("mstr_btc_holdings", "asst_btc_holdings", "strc_annual_rate_pct", "sata_annual_rate_pct"):
        a = auto.get(key) or {}
        if a.get("value") is not None:
            m[key] = a["value"]
            provenance[key] = m[key]
            provenance[f"{key}_as_of"] = f"{a.get('as_of')} ({a.get('source')})"
        elif m.get(key) is not None:
            provenance[key] = m[key]
            provenance[f"{key}_as_of"] = f"{m.get('rates_as_of') or 'unknown'} (entered by hand)"
    return m, provenance


TAPE_TICKERS = ["MSTR", "STRC", "ASST", "SATA", "BMNR", "SPCX", "TSLA", "SPY", "QQQ", "^RUT", "^TNX", "DX-Y.NYB"]
TAPE_CRYPTO = ["ETH", "ZEC"]  # Coinbase, 24-hour moves like BTC

# ------------------------------------------------------------------ mNAV, the way each issuer's own dashboard does it
# Strategy (strategy.com): mNAV = enterprise value / bitcoin NAV, published live by its KPI feed.
# Strive (strive.com/treasury, "ev_to_btc_nav"): EV = fully diluted shares x ASST + debt + SATA notional - cash
#   - marketable securities; mNAV = EV / (BTC held x BTC price). Inputs come from Strive's dashboard feed.
JSON_UA = {"User-Agent": "Mozilla/5.0 (compatible; XControlPanel/1.0)", "Accept": "application/json"}


def strategy_mnav() -> dict:
    try:
        r = httpx.get("https://api.strategy.com/btc/bitcoinKpis", headers=JSON_UA, timeout=20)
        r.raise_for_status()
        k = r.json().get("results") or {}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("strategy.com KPIs failed: %s", e)
        return {}
    v = _num(k.get("mNav"))
    if not v:
        return {}
    ext = k.get("extendedSession") or {}
    return {"value": v, "extended": _num(ext.get("mNav")), "extended_session": ext.get("sessionType"),
            "method": "EV / bitcoin NAV (strategy.com)", "source": "strategy.com"}


def strive_inputs() -> dict:
    """The latest day on Strive's treasury dashboard: fully diluted shares, debt, cash, securities, SATA notional,
    BTC held, and Strive's own mNAV for that day."""
    to = today_ny()
    try:
        r = httpx.get("https://strive.com/treasury/api/dashboard/calculated", headers=JSON_UA, timeout=25,
                      params={"fromDate": (to - timedelta(days=10)).isoformat(), "toDate": to.isoformat(),
                              "currency": "USD", "stockSymbol": "ASST"})
        r.raise_for_status()
        rows = [x for x in ((r.json().get("data") or {}).get("btcHoldings") or []) if x.get("evMnav")]
    except (httpx.HTTPError, ValueError) as e:
        log.warning("strive.com dashboard failed: %s", e)
        return {}
    if not rows:
        return {}
    x = rows[-1]
    return {"shares": _num(x.get("sharesOutstanding")), "debt": _num(x.get("debt")) or 0.0,
            "cash": _num(x.get("cash")) or 0.0, "securities": _num(x.get("marketableSecurities")) or 0.0,
            "preferred": _num(x.get("preferredStockMarketCap")) or 0.0, "btc": _num(x.get("btcHoldings")),
            "mnav": _num(x.get("evMnav")), "date": x.get("date")}


def strive_mnav(inputs: dict, asst_price: float | None, btc_price: float | None) -> dict:
    """Strive's EV / bitcoin NAV at live prices (falls back to Strive's own number for the day)."""
    if not inputs:
        return {}
    if inputs.get("shares") and inputs.get("btc") and asst_price and btc_price:
        ev = (inputs["shares"] * asst_price + inputs["debt"] + inputs["preferred"] - inputs["cash"]
              - inputs["securities"])
        value = ev / (inputs["btc"] * btc_price)
    elif inputs.get("mnav"):
        value = inputs["mnav"]
    else:
        return {}
    return {"value": value, "method": "EV / bitcoin NAV (strive.com)", "source": "strive.com",
            "inputs_as_of": inputs.get("date")}


def quotes(tickers: list[str] | None = None) -> dict:
    """Live prices for the panel's moving tape: BTC, the tape tickers, ETH / ZEC, both issuers' mNAV, Fear & Greed
    (a couple of seconds, nothing saved)."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(6) as ex:
        fb = ex.submit(btc)
        fe = ex.submit(equities, list(tickers or TAPE_TICKERS))
        fc = {c: ex.submit(coinbase, f"{c}-USD") for c in TAPE_CRYPTO}
        ff = ex.submit(fear_greed)
        fm = ex.submit(strategy_mnav)
        fs = ex.submit(strive_inputs)
        b, eq = fb.result(), fe.result()
        mnav = {"MSTR": fm.result(), "ASST": strive_mnav(fs.result(), (eq.get("ASST") or {}).get("price"), b.get("price"))}
        return {"as_of": utcnow().isoformat(), "btc": b, "equities": eq,
                "crypto": {c: f.result() for c, f in fc.items()}, "mnav": {k: v for k, v in mnav.items() if v},
                "fear_greed": ff.result()}


def take_snapshot(save: bool = True) -> dict:
    st = config.settings()
    m, provenance = _inputs(st.get("market", {}))
    data: dict = {"as_of": utcnow().isoformat(), "as_of_ny": now_ny().strftime("%Y-%m-%d %H:%M ET")}
    data["inputs"] = provenance
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
    if btc_px:
        for t, h in holdings.items():
            if h:
                derived[f"{t.lower()}_btc_holdings"] = h
                derived[f"{t.lower()}_btc_nav_usd_bn"] = round(h * btc_px / 1e9, 2)
    # mNAV as each issuer publishes it (enterprise value / bitcoin NAV), not market cap / NAV
    asst_px = (eq.get("ASST") or {}).get("price") or (equities(["ASST"]).get("ASST") or {}).get("price")
    for t, mv in (("MSTR", strategy_mnav()), ("ASST", strive_mnav(strive_inputs(), asst_px, btc_px))):
        if mv.get("value"):
            derived[f"{t.lower()}_mnav"] = round(mv["value"], 3)
            derived[f"{t.lower()}_mnav_method"] = mv["method"]
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
