"""Is this Digital Credit Report panel safe to post? Pure checks over one render + audit (no I/O).

Each check is PASS, WARN (post, but you're told), WAIT (not yet: the new data hasn't landed, retry)
or FAIL (something is wrong in the panel itself: block, alert, keep retrying until the deadline).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from xcp.timeutil import fmt_ny, last_nyse_session, nyse_open, parse_iso

BLANK = {"", "—", "–", "-", "none", "nan", "n/a"}
USD_PREFERREDS = {"STRF", "STRC", "STRK", "STRD"}
# Audit sections (panels/extras.py) that feed each X image; a saved-snapshot fallback there means stale numbers.
STALE_MATTERS = {"monday": {"strategy", "strive"}, "wednesday": {"strategy", "strive", "fred", "yahoo"},
                 "friday": {"onchain", "yahoo", "fred"}}
# digital-exposure source checks that must be PASS (fresh) for each panel.
FRESH_REQUIRED = {"monday": {"strategy", "strive"}, "wednesday": {"strategy", "strive", "yahoo.STRC", "yahoo.SATA"},
                  "friday": {"yahoo.BTC-USD", "yahoo.DX-Y.NYB", "onchain"}}
# digital-exposure checks that mean "this tile's data hasn't loaded" (a blank on the X image): wait and
# retry, whether digital-exposure reports them as WARN (before Sep 26) or FAIL (its third audit round).
DATA_NOT_LOADED = {"friday": ("turnover.", "inputs", "MSTR.price_nav", "ASST.price_nav")}
MAX_RATIO = 4 / 3 + 0.002  # X shows up to 3:4 uncropped
WIDTH = 1440


@dataclass
class Check:
    id: str
    status: str  # PASS | WARN | WAIT | FAIL
    detail: str = ""


@dataclass
class Verdict:
    panel: str
    checks: list[Check] = field(default_factory=list)

    def add(self, id_: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(id_, status, detail))

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(c.status in ("PASS", "WARN") for c in self.checks)

    @property
    def failed(self) -> bool:
        return any(c.status == "FAIL" for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.status in ("FAIL", "WAIT")]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "WARN"]

    def as_dicts(self) -> list[dict]:
        return [asdict(c) for c in self.checks]

    def summary(self) -> str:
        n = {s: sum(1 for c in self.checks if c.status == s) for s in ("PASS", "WARN", "WAIT", "FAIL")}
        return " · ".join(f"{v} {k}" for k, v in n.items() if v)


# ------------------------------------------------------------------ parsing the panel's displayed values

NUM_RE = re.compile(r"([+\-−]?)\$?(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB]\b|[kKmMbB](?=[\s/)×x]|$))?")
SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}


def num(text) -> float | None:
    """First number in a displayed value: '−$174.0m' -> -174e6, '846,000 BTC' -> 846000, '$6.09B / …' -> 6.09e9."""
    m = NUM_RE.search(str(text or ""))
    if not m:
        return None
    val = float(m.group(2).replace(",", "")) * SCALE.get((m.group(3) or "").lower(), 1)
    return -val if m.group(1) in ("-", "−") else val


def _tolerance(text: str) -> float:
    """Half a unit of the last displayed digit, plus a hair."""
    m = NUM_RE.search(str(text or ""))
    if not m:
        return 0.5
    decimals = len(m.group(2).split(".")[1]) if "." in m.group(2) else 0
    return 0.51 * 10 ** -decimals * SCALE.get((m.group(3) or "").lower(), 1)


def values_of(audit: dict, panel: str) -> dict[str, str]:
    rows = ((audit.get("panels") or {}).get(panel) or {}).get("values") or []
    return {r.get("metric", ""): str(r.get("value", "")) for r in rows}


def fingerprint(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


def _is_blank(v: str) -> bool:
    return v.strip().lower() in BLANK or v.strip().startswith("—")


def png_size(png: bytes) -> tuple[int, int] | None:
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as im:
            return im.size
    except Exception:  # corrupt image
        return None


# ------------------------------------------------------------------ checks shared by every panel

def structural(v: Verdict, panel: str, audit: dict, checks: dict, png: bytes | None) -> dict[str, str]:
    """Contract with digital-exposure's outputs, its independent audit, blanks, layout and image size."""
    panel_audit = (audit.get("panels") or {}).get(panel)
    if not isinstance(panel_audit, dict) or not panel_audit.get("values"):
        v.add("contract", "FAIL", f"audit.json has no values for the {panel} panel")
        return {}
    if not isinstance(checks.get("checks"), list) or "summary" not in checks:
        v.add("contract", "FAIL", "checks.json is missing its checks or summary")
        return {}
    v.add("contract", "PASS", "render_previews.py and audit_panels.py outputs as expected")
    values = values_of(audit, panel)

    rows = [c for c in checks["checks"] if c.get("panel") in (panel, "sources")]
    loading = DATA_NOT_LOADED.get(panel, ())
    not_loaded = [c for c in rows if c.get("status") in ("WARN", "FAIL") and str(c.get("id", "")).startswith(loading)]
    fails = [c for c in rows if c.get("status") == "FAIL" and c not in not_loaded]
    soft = [c for c in rows if c.get("status") == "WARN" and c not in not_loaded]
    n = checks.get("summary", {})
    v.add("audit", "FAIL" if fails else "PASS",
          "; ".join(f"{c.get('label') or c.get('id')}: {c.get('detail', '')}" for c in fails)[:600]
          or f"digital-exposure audit {n.get('PASS', 0)} PASS · {n.get('WARN', 0)} WARN · {n.get('FAIL', 0)} FAIL")
    if not_loaded:
        v.add("audit_tiles", "WAIT", "not loaded yet: " + "; ".join(
            f"{c.get('label') or c['id']}" + (f" ({c['detail']})" if c.get("detail") else "") for c in not_loaded)[:600])
    for c in soft:
        v.add(f"audit_warn:{c.get('id')}", "WARN", f"{c.get('label')}: {c.get('detail', '')}"[:300])

    fresh = {c.get("id"): c for c in rows if c.get("panel") == "sources"}
    stale_sources = [f"{k} ({fresh[k].get('detail', '')})" for k in sorted(FRESH_REQUIRED.get(panel, ()))
                     if k in fresh and fresh[k].get("status") != "PASS"]
    missing = [k for k in sorted(FRESH_REQUIRED.get(panel, ())) if k not in fresh]
    if stale_sources:
        v.add("sources_fresh", "WAIT", "not fresh yet: " + ", ".join(stale_sources))
    elif missing:
        v.add("sources_fresh", "WARN", "freshness checks not reported for: " + ", ".join(missing))
    else:
        v.add("sources_fresh", "PASS", ", ".join(sorted(FRESH_REQUIRED.get(panel, ()))) + " current")

    stale = set(audit.get("stale_sections") or [])
    bad = sorted(stale & STALE_MATTERS.get(panel, set()))
    if bad:
        v.add("live_data", "WAIT", "saved-snapshot fallback for: " + ", ".join(bad))
    else:
        v.add("live_data", "PASS" if not stale else "WARN",
              "every source this panel uses was fetched live" if not stale
              else "fallback only in sections this image doesn't use: " + ", ".join(sorted(stale)))

    blanks = [k for k, val in values.items() if _is_blank(val)]
    v.add("no_blanks", "WAIT" if blanks else "PASS",
          ("blank on the image: " + ", ".join(blanks)) if blanks else f"all {len(values)} displayed values filled")

    over = panel_audit.get("overflows") or []
    v.add("layout", "FAIL" if over else "PASS", f"text overflow: {over}"[:300] if over else "no text overflows")

    size = png_size(png) if png else None
    if size is None:
        v.add("image", "FAIL", f"{panel}.png missing or unreadable")
    elif size[0] != WIDTH or size[1] / size[0] > MAX_RATIO:
        v.add("image", "FAIL", f"{size[0]}×{size[1]}: X needs {WIDTH} wide and no taller than 3:4")
    else:
        v.add("image", "PASS", f"{size[0]}×{size[1]}")
    return values


# ------------------------------------------------------------------ Monday: both 8-Ks in, and on the image

def _expect(v: Verdict, id_: str, values: dict, metric: str, expected: float | None, label: str,
            parse=num, status_on_miss: str = "FAIL") -> None:
    shown_text = values.get(metric)
    if shown_text is None:
        v.add(id_, "FAIL", f"contract: the audit has no '{metric}' row")
        return
    if expected is None:
        v.add(id_, "WARN", f"{label}: the 8-K feed has no figure to compare")
        return
    shown = parse(shown_text)
    if shown is not None and abs(shown - expected) <= max(_tolerance(shown_text), 0.5):
        v.add(id_, "PASS", f"{label}: image {shown_text} = 8-K")
    else:
        v.add(id_, status_on_miss, f"{label}: image shows {shown_text}, 8-K says {expected:,.0f}")


def _cash_part(text: str) -> float | None:
    m = re.search(r"\$([\d.,]+)\s*([mMbB])\s*cash", text or "")
    return float(m.group(1).replace(",", "")) * SCALE[m.group(2).lower()] if m else None


def monday(v: Verdict, values: dict, filings: dict[str, dict], notice: str | None, publication: dict | None,
           saved_quotes: bool) -> None:
    have = [t for t in ("MSTR", "ASST") if t in filings]
    if len(have) < 2:
        missing = ", ".join({"MSTR": "Strategy", "ASST": "Strive"}[t] for t in ("MSTR", "ASST") if t not in filings)
        v.add("8k_in_feed", "WAIT", f"waiting for this week's 8-K: {missing}")
        return
    v.add("8k_in_feed", "PASS", "both weekly 8-Ks are in the feed and validated: " + ", ".join(
        f"{t} {filings[t]['accession']}" for t in ("MSTR", "ASST")))

    shown = {t: values.get(f"{t} balance date") for t in ("MSTR", "ASST")}
    want = {t: filings[t]["extracted"]["balanceDate"] for t in ("MSTR", "ASST")}
    if shown != want:
        v.add("new_edition", "WAIT", "image still shows balance dates " + ", ".join(
            f"{t} {shown[t]}" for t in shown) + "; this week's 8-Ks are " + ", ".join(f"{t} {want[t]}" for t in want))
        return  # the rest compares against the new week, so stop here
    v.add("new_edition", "PASS", f"image shows this week's balances (MSTR {want['MSTR']}, ASST {want['ASST']})")

    v.add("complete_edition", "WAIT" if notice else "PASS", notice or "no 'retaining last edition' notice")
    if publication is not None:
        ok = publication.get("status") == "complete"
        v.add("publication_check", "PASS" if ok else "WAIT",
              publication.get("subtitle", "") if ok else f"{publication.get('status')}: {publication.get('reason', '')}"[:300])
    v.add("live_quotes", "WAIT" if saved_quotes else "PASS",
          "price refresh failed; the image used saved quotes" if saved_quotes else "live quotes")

    m, a = filings["MSTR"]["extracted"], filings["ASST"]["extracted"]
    mf, af = m.get("facts") or {}, a.get("facts") or {}
    for t, fx in (("MSTR", mf), ("ASST", af)):
        buys, sells = fx.get("weekly_btc_purchases") or 0, fx.get("weekly_btc_sales") or 0
        text = values.get(f"{t} bitcoin bought")
        got = num(text)
        if text is None:
            v.add(f"{t}.btc_bought", "FAIL", f"contract: the audit has no '{t} bitcoin bought' row")
        elif got is not None and (abs(got - buys) < 0.5 or abs(got - (buys - sells)) < 0.5):
            v.add(f"{t}.btc_bought", "PASS", f"image {text} = 8-K")
        else:
            v.add(f"{t}.btc_bought", "FAIL", f"image shows {text}; 8-K bought {buys:,} sold {sells:,}")
        _expect(v, f"{t}.btc_held", values, f"{t} total BTC held", fx.get("btc_holdings"), "BTC held")

    usd = [mf.get("usd_reserve_usd"), mf.get("usd_cash_usd")]
    _expect(v, "MSTR.cash", values, "MSTR cash balance / change",
            sum(x for x in usd if x is not None) if any(x is not None for x in usd) else None, "USD reserve + cash")
    if "common_issuance_proceeds_usd" in mf:
        _expect(v, "MSTR.common", values, "MSTR net common capital",
                (mf.get("common_issuance_proceeds_usd") or 0) - (mf.get("common_repurchases_cash_usd") or 0),
                "common ATM (net)")
    sec = {k: x for k, x in (m.get("securities") or {}).items() if k != "MSTR"}
    if sec:
        pref = sum((x.get("netIssuanceProceedsUsd") or 0) - (x.get("repurchaseCashUsd") or 0) for x in sec.values())
        other = set(sec) - USD_PREFERREDS  # e.g. euro STRE: the image converts it, the feed doesn't
        _expect(v, "MSTR.preferred", values, "MSTR preferred capital", pref, "preferred ATM (net)",
                status_on_miss="WARN" if other else "FAIL")
    cash_text = values.get("ASST cash balance / change")
    if cash_text is not None and af.get("cash_and_equivalents_usd") is not None:
        got, want_cash = _cash_part(cash_text), af["cash_and_equivalents_usd"]
        ok = got is not None and abs(got - want_cash) <= 0.051e6
        v.add("ASST.cash", "PASS" if ok else "FAIL",
              f"image cash {got / 1e6 if got else '?'}m vs 8-K {want_cash / 1e6:,.1f}m")
    if af.get("net_sata_shares_change") is not None:
        _expect(v, "ASST.preferred", values, "ASST preferred capital", af["net_sata_shares_change"] * 100,
                "SATA net new shares × $100")


# ------------------------------------------------------------------ Wednesday: the ledger carries Monday's week

def wednesday(v: Verdict, values: dict, feed: dict | None) -> None:
    weeks = sorted(m.group(1) for k in values if (m := re.match(r"Week of (\d{4}-\d{2}-\d{2})", k)))
    if feed is None:
        v.add("ledger_week", "WARN", "8-K feed unreachable; couldn't confirm the flow ledger has this week")
        return
    from xcp.sources.digital_exposure import latest_weekly

    latest = latest_weekly(feed, "MSTR")
    want = (latest or {}).get("extracted", {}).get("periodStart")
    if not want:
        v.add("ledger_week", "WARN", "no validated Strategy 8-K in the feed")
    elif not weeks or weeks[-1] < want:
        v.add("ledger_week", "WAIT", f"flow ledger ends at {weeks[-1] if weeks else '—'}; latest 8-K week starts {want}")
    else:
        v.add("ledger_week", "PASS", f"flow ledger includes the 8-K week of {want}")


# ------------------------------------------------------------------ Friday: after the 4 pm close, today's marks

def friday(v: Verdict, values: dict, checks: dict, audit: dict, now: datetime, settle_minutes: int) -> None:
    today = now.date()
    session = last_nyse_session(today)
    if nyse_open(today):
        ready_at = now.replace(hour=16, minute=0, second=0, microsecond=0) + timedelta(minutes=settle_minutes)
        if now < ready_at:
            v.add("after_close", "WAIT", f"waiting for the 4:00 PM close to settle (from {ready_at:%I:%M %p})")
            return
        v.add("after_close", "PASS", "after the 4:00 PM close")
    else:
        v.add("after_close", "PASS", f"market closed today; week ends with the {session:%a %b %d} session")
    week = values.get("Week ended")
    if week != session.isoformat():
        v.add("week_rolled", "WAIT", f"image shows week ended {week}; expected {session.isoformat()}")
    else:
        v.add("week_rolled", "PASS", f"week ended {week}")
    if ((audit.get("panels") or {}).get("friday") or {}).get("demo_data"):
        v.add("real_data", "FAIL", "the Friday panel is using demo data")
    dated = {c.get("id"): str(c.get("value")) for c in checks.get("checks", [])
             if c.get("id") in ("yahoo.STRC", "yahoo.SATA", "yahoo.DX-Y.NYB", "macro.dxy", "macro.tnx")}
    behind = [f"{k} {d}" for k, d in sorted(dated.items()) if d < session.isoformat()]
    if behind:
        v.add("todays_marks", "WAIT", "not updated to today's close yet: " + ", ".join(behind))
    else:
        v.add("todays_marks", "PASS" if dated else "WARN",
              f"closes dated {session.isoformat()}" if dated else "no dated close checks reported")
    if _is_blank(values.get("BTC Friday 4 pm mark", "")):
        v.add("btc_mark", "WAIT", "no BTC 4 pm mark yet")


# ------------------------------------------------------------------ entry point

def evaluate(panel: str, *, audit: dict, checks: dict, png: bytes | None, now: datetime,
             filings: dict[str, dict] | None = None, feed: dict | None = None, publication: dict | None = None,
             saved_quotes: bool = False, previous_fingerprint: str = "", settle_minutes: int = 10) -> Verdict:
    v = Verdict(panel)
    values = structural(v, panel, audit, checks, png)
    if not values:
        return v
    if panel == "monday":
        monday(v, values, filings or {}, audit.get("monday_notice"), publication, saved_quotes)
    elif panel == "wednesday":
        wednesday(v, values, feed)
    elif panel == "friday":
        friday(v, values, checks, audit, now, settle_minutes)
    if previous_fingerprint and fingerprint(values) == previous_fingerprint:
        v.add("changed", "WARN", "identical numbers to the last showcase of this panel")
    return v


def structural_only(panel: str, audit: dict, checks: dict, png: bytes | None) -> Verdict:
    """Preflight: would the code render a clean image right now (no timing or new-data gates)?"""
    v = Verdict(panel)
    structural(v, panel, audit, checks, png)
    return v


def release_filings_label(filings: dict[str, dict]) -> str:
    return " · ".join(f"{t} accepted {fmt_ny(parse_iso(f.get('acceptedAt')), '%I:%M %p')} (balance "
                      f"{f['extracted']['balanceDate']})" for t, f in sorted(filings.items()))

