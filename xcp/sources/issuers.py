"""Issuer inputs the Market Desk needs, filled automatically from free public sources.

  BTC holdings   latest validated weekly 8-K in the capital-report feed (fallback: strategy.com / strive.com)
  STRC rate      strategy.com STRC KPIs (currentDividend, % per year)
  SATA rate      strive.com treasury feed (dividendRate)

Each value keeps its date and source. A failed source keeps the last good value (stored in kv).
"""
from __future__ import annotations

import logging

import httpx

from xcp import db
from xcp.sources import digital_exposure as de
from xcp.timeutil import today_ny, utcnow

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (compatible; XControlPanel/1.0)", "Accept": "application/json"}
KV = "market:auto"
KEYS = ("mstr_btc_holdings", "asst_btc_holdings", "strc_annual_rate_pct", "sata_annual_rate_pct")


def _get(url: str):
    r = httpx.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch() -> tuple[dict, list[str]]:
    """Fresh values only: {key: {value, as_of, source}} plus the errors hit on the way."""
    out: dict[str, dict] = {}
    errors: list[str] = []
    try:
        feed = de.fetch_feed()
        for ticker, key in (("MSTR", "mstr_btc_holdings"), ("ASST", "asst_btc_holdings")):
            f = de.latest_weekly(feed, ticker)
            held = _num(((f or {}).get("extracted") or {}).get("facts", {}).get("btc_holdings"))
            if held:
                out[key] = {"value": held, "as_of": f["extracted"]["balanceDate"],
                            "source": f"SEC 8-K {f.get('accession')}"}
    except Exception as e:  # network or format
        errors.append(f"8-K feed: {e}")
    if "mstr_btc_holdings" not in out:
        try:
            k = _get("https://api.strategy.com/btc/bitcoinKpis")["results"]
            if held := _num(k.get("btcHoldings")):
                out["mstr_btc_holdings"] = {"value": held, "as_of": today_ny().isoformat(), "source": "strategy.com"}
        except Exception as e:
            errors.append(f"strategy.com holdings: {e}")
    try:
        row = _get("https://api.strategy.com/btc/strcKpiData")[0]
        if row.get("company") != "STRC":
            raise ValueError(f"expected STRC, got {row.get('company')}")
        if (rate := _num(row.get("currentDividend"))) is not None:
            out["strc_annual_rate_pct"] = {"value": rate, "as_of": today_ny().isoformat(),
                                           "source": "strategy.com STRC KPIs"}
    except Exception as e:
        errors.append(f"strategy.com STRC: {e}")
    try:
        t = _get("https://strive.com/api/treasury").get("treasury") or {}
        if (rate := _num(t.get("dividendRate"))) is not None:
            out["sata_annual_rate_pct"] = {"value": round(rate * 100, 4), "as_of": t.get("asOf") or today_ny().isoformat(),
                                           "source": "strive.com treasury feed"}
        if "asst_btc_holdings" not in out and (held := _num(t.get("btcHoldings"))):
            out["asst_btc_holdings"] = {"value": round(held, 2), "as_of": t.get("asOf"), "source": "strive.com treasury feed"}
    except Exception as e:
        errors.append(f"strive.com: {e}")
    return out, errors


def refresh() -> dict:
    """Fetch, merge over the last good values, save. Returns {key: {value, as_of, source, fresh}} + errors."""
    saved = db.kv_get(KV) or {}
    fresh, errors = fetch()
    merged = {k: {**v, "fresh": False} for k, v in (saved.get("values") or {}).items()}
    for k, v in fresh.items():
        merged[k] = {**v, "fresh": True}
    db.kv_set(KV, {"values": merged, "checked_at": utcnow().isoformat(), "errors": errors})
    if errors:
        log.warning("issuer inputs: %s", "; ".join(errors))
    return {"values": merged, "errors": errors}


def last() -> dict:
    return db.kv_get(KV) or {}
