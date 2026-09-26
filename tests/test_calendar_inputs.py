"""Automatic calendar and market inputs, offline (network calls are mocked; fixtures are real pages).

  fixtures/calendar/fomc-2026-2027.html   the Fed's FOMC calendar page (2026 and 2027 blocks)
  fixtures/calendar/bea-sample.ics        three events from BEA's release-schedule feed
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-cal-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database

from xcp import db  # noqa: E402
from xcp.sources import calendar_feeds as cf, issuers, market  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "calendar"


class Parsing(unittest.TestCase):
    def test_fomc_decision_days(self):
        days = cf.parse_fomc((FIX / "fomc-2026-2027.html").read_text(encoding="utf-8"))
        self.assertIn(date(2026, 10, 28), days)
        self.assertTrue(all(d.year in (2026, 2027) for d in days))
        self.assertGreaterEqual(len([d for d in days if d.year == 2026]), 8)

    def test_bea_feed_times_are_new_york(self):
        with mock.patch.object(cf, "_get", return_value=(FIX / "bea-sample.ics").read_text(encoding="utf-8")):
            events = {e["title"]: e for e in cf.bea()}
        pce = events["PCE inflation (August 2026)"]
        self.assertEqual((pce["date"], pce["time"], pce["importance"]), ("2026-09-30", "08:30", 3))
        self.assertEqual(events["GDP Q3 2026 (advance estimate)"]["importance"], 3)
        self.assertFalse(any("Trade" in t for t in events))  # only GDP and PCE are kept

    def test_ics_unfolds_lines_and_reads_tz_and_all_day(self):
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Consumer Price Index for August\n  2026\n"
               "DTSTART;TZID=America/New_York:20260911T083000\nEND:VEVENT\nBEGIN:VEVENT\n"
               "SUMMARY:Employment Situation for September 2026\nDTSTART;VALUE=DATE:20261002\nEND:VEVENT\nEND:VCALENDAR\n")
        with mock.patch.object(cf, "_get", return_value=ics):
            ev = {e["title"]: e for e in cf.bls("contact")}
        self.assertEqual(ev["CPI (August 2026)"]["time"], "08:30")
        self.assertEqual(ev["Jobs report (September 2026)"]["date"], "2026-10-02")


class Sync(unittest.TestCase):
    def setUp(self):
        db.engine()
        with db.session() as s:
            s.query(db.CalendarEvent).delete()
            s.add(db.CalendarEvent(date="2026-10-01", title="My conference", pillar="bitcoin", notes="mine"))
            s.commit()

    def _sync(self, fed_days, nasdaq=None, curated=None):
        srcs = {"fed": lambda: [cf._event(d, "FOMC rate decision", time="14:00", importance=3) for d in fed_days],
                "nasdaq": lambda: nasdaq or [], "curated": lambda: curated or []}
        with mock.patch.object(cf, "sources", return_value=srcs), \
                mock.patch.object(cf, "today_ny", return_value=date(2026, 9, 26)):
            return cf.sync()

    def rows(self):
        with db.session() as s:
            return {(r.date, r.title): r.notes for r in s.query(db.CalendarEvent)}

    def test_adds_once_keeps_your_rows_and_supersedes_instead_of_deleting(self):
        first = self._sync(["2026-10-28", "2026-11-18"])
        self.assertEqual(first["added"], 2)
        self.assertEqual(self._sync(["2026-10-28", "2026-11-18"])["added"], 0)
        moved = self._sync(["2026-10-29", "2026-11-18"])  # the Fed moves a meeting
        rows = self.rows()
        self.assertEqual(moved["superseded"], 1)
        self.assertIn("superseded", rows[("2026-10-28", "FOMC rate decision")])  # kept, hidden from the writer
        self.assertIn(("2026-10-29", "FOMC rate decision"), rows)
        self.assertEqual(rows[("2026-10-01", "My conference")], "mine")

    def test_a_failing_source_changes_nothing_of_its_own(self):
        self._sync(["2026-10-28"])

        def boom():
            raise OSError("fed down")
        with mock.patch.object(cf, "sources", return_value={"fed": boom}), \
                mock.patch.object(cf, "today_ny", return_value=date(2026, 9, 26)):
            out = cf.sync()
        self.assertIn("fed: OSError: fed down", out["errors"][0])
        self.assertNotIn("superseded", self.rows()[("2026-10-28", "FOMC rate decision")])

    def test_confirmed_curated_earnings_replace_the_nasdaq_estimate(self):
        est = [cf._event("2026-10-29", "MSTR earnings (est.)", importance=3)]
        conf = [{**cf._event("2026-10-30", "MSTR earnings", importance=3), "kind": "earnings", "ticker": "MSTR"}]
        self._sync([], nasdaq=est, curated=conf)
        titles = {t for _, t in self.rows()}
        self.assertIn("MSTR earnings", titles)
        self.assertNotIn("MSTR earnings (est.)", titles)


class Inputs(unittest.TestCase):
    def setUp(self):
        db.engine()
        db.kv_delete(issuers.KV)

    def test_failed_source_keeps_the_last_good_value(self):
        good = {"strc_annual_rate_pct": {"value": 12.0, "as_of": "2026-09-26", "source": "strategy.com STRC KPIs"}}
        with mock.patch.object(issuers, "fetch", return_value=(good, [])):
            issuers.refresh()
        with mock.patch.object(issuers, "fetch", return_value=({}, ["strategy.com STRC: timeout"])):
            out = issuers.refresh()
        v = out["values"]["strc_annual_rate_pct"]
        self.assertEqual((v["value"], v["fresh"]), (12.0, False))
        self.assertEqual(out["errors"], ["strategy.com STRC: timeout"])

    def test_snapshot_uses_auto_values_with_provenance_and_manual_as_fallback(self):
        auto = {"values": {"mstr_btc_holdings": {"value": 846000.0, "as_of": "2026-09-20", "source": "SEC 8-K x"}}}
        with mock.patch.object(issuers, "refresh", return_value=auto):
            m, prov = market._inputs({"auto_inputs": True, "sata_annual_rate_pct": 13.0, "rates_as_of": "2026-05"})
        self.assertEqual(m["mstr_btc_holdings"], 846000.0)
        self.assertEqual(prov["mstr_btc_holdings_as_of"], "2026-09-20 (SEC 8-K x)")
        self.assertEqual(prov["sata_annual_rate_pct_as_of"], "2026-05 (entered by hand)")
        with mock.patch.object(issuers, "refresh") as called:
            m, _ = market._inputs({"auto_inputs": False, "mstr_btc_holdings": 1.0})
        called.assert_not_called()
        self.assertEqual(m["mstr_btc_holdings"], 1.0)


if __name__ == "__main__":
    unittest.main()
