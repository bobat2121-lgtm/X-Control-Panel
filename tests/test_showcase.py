"""Showcase gate + watcher tests on real digital-exposure output.

Fixtures (tests/fixtures/showcase):
  feed-2026-09-26.json                 the live 8-K feed (Sep 21 release: MSTR balance Sep 20, ASST Sep 18)
  audit/checks-clean-2026-09-26.json   a clean render of digital-exposure 1490b3d (Sat Sep 26, 12:33 ET)
  audit/checks-runner-...-2007.json    GitHub's Panel audit run, Fri Sep 25 8:07 pm ET: Friday price/NAV blank
  publication-2026-09-26.json          check_monday_publication.py --live on the same data

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database
os.environ.pop("DISCORD_WEBHOOK_URL", None)
os.environ.pop("GH_DISPATCH_TOKEN", None)
os.environ["LLM_BACKEND"] = "mock"

from PIL import Image  # noqa: E402

from xcp import db, showcase, showcase_gate as gate, timeutil  # noqa: E402
from xcp.sources import digital_exposure as de  # noqa: E402
from xcp.timeutil import NY  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "showcase"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def png(w: int = 1440, h: int = 1884) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 40)).save(buf, format="PNG")
    return buf.getvalue()


FEED = load("feed-2026-09-26.json")
AUDIT = load("audit-clean-2026-09-26.json")
CHECKS = load("checks-clean-2026-09-26.json")
PUB = load("publication-2026-09-26.json")
RUNNER_AUDIT = load("audit-runner-2026-09-25-2007.json")
RUNNER_CHECKS = load("checks-runner-2026-09-25-2007.json")
SEP21 = date(2026, 9, 21)


def at(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def statuses(v: gate.Verdict) -> dict[str, str]:
    return {c.id: c.status for c in v.checks}


def monday(audit=AUDIT, checks=CHECKS, filings=None, pub=PUB, image=None, **kw) -> gate.Verdict:
    filings = de.weekly_filings(FEED, SEP21) if filings is None else filings
    return gate.evaluate("monday", audit=audit, checks=checks, png=image or png(), now=at(2026, 9, 21, 8, 12),
                         filings=filings, feed=FEED, publication=pub, **kw)


def set_value(audit: dict, panel: str, metric: str, value: str) -> dict:
    a = copy.deepcopy(audit)
    for row in a["panels"][panel]["values"]:
        if row["metric"] == metric:
            row["value"] = value
    return a


class Calendars(unittest.TestCase):
    def test_release_day_moves_to_tuesday_after_edgar_holiday(self):
        self.assertEqual(timeutil.weekly_release_date(date(2026, 9, 28)), date(2026, 9, 28))
        self.assertEqual(timeutil.weekly_release_date(date(2026, 10, 14)), date(2026, 10, 13))  # Columbus Day
        self.assertEqual(timeutil.weekly_release_date(date(2027, 1, 18)), date(2027, 1, 19))  # MLK Day

    def test_nyse_calendar(self):
        self.assertFalse(timeutil.nyse_open(date(2027, 3, 26)))  # Good Friday
        self.assertTrue(timeutil.nyse_open(date(2026, 10, 12)))  # Columbus Day: stocks trade, EDGAR closed
        self.assertFalse(timeutil.edgar_open(date(2026, 10, 12)))
        self.assertEqual(timeutil.last_nyse_session(date(2027, 3, 26)), date(2027, 3, 25))

    def test_panel_for_dates(self):
        self.assertEqual(showcase.panel_for(date(2026, 9, 28)), "monday")
        self.assertEqual(showcase.panel_for(date(2026, 9, 30)), "wednesday")
        self.assertEqual(showcase.panel_for(date(2026, 10, 2)), "friday")
        self.assertIsNone(showcase.panel_for(date(2026, 10, 12)))  # Columbus Day Monday
        self.assertEqual(showcase.panel_for(date(2026, 10, 13)), "monday")  # ...so Tuesday gets the Ledger
        self.assertIsNone(showcase.panel_for(date(2026, 9, 29)))


class MondayGate(unittest.TestCase):
    def test_feed_finds_both_weekly_8ks(self):
        f = de.weekly_filings(FEED, SEP21)
        self.assertEqual(sorted(f), ["ASST", "MSTR"])
        self.assertEqual(f["MSTR"]["extracted"]["balanceDate"], "2026-09-20")
        self.assertEqual(de.weekly_filings(FEED, date(2026, 9, 22)), {})

    def test_real_sep21_edition_is_ready_and_matches_every_8k_fact(self):
        v = monday()
        self.assertTrue(v.ready, v.blockers)
        s = statuses(v)
        for key in ("MSTR.btc_bought", "MSTR.btc_held", "MSTR.cash", "MSTR.common", "MSTR.preferred",
                    "ASST.btc_bought", "ASST.btc_held", "ASST.cash", "ASST.preferred", "new_edition",
                    "publication_check", "no_blanks", "image", "audit"):
            self.assertEqual(s.get(key), "PASS", key)

    def test_waits_until_both_8ks_are_in_the_feed(self):
        only_strive = {k: v for k, v in de.weekly_filings(FEED, SEP21).items() if k == "ASST"}
        v = monday(filings=only_strive)
        self.assertFalse(v.ready)
        self.assertEqual(statuses(v)["8k_in_feed"], "WAIT")
        self.assertIn("Strategy", v.blockers[0].detail)

    def test_waits_while_the_image_still_shows_last_week(self):
        old = set_value(set_value(AUDIT, "monday", "MSTR balance date", "2026-09-13"),
                        "monday", "ASST balance date", "2026-09-11")
        v = monday(audit=old)
        self.assertEqual(statuses(v)["new_edition"], "WAIT")
        self.assertFalse(v.failed)

    def test_fails_when_the_image_disagrees_with_the_8k(self):
        v = monday(audit=set_value(AUDIT, "monday", "MSTR bitcoin bought", "951 BTC"))
        self.assertTrue(v.failed)
        self.assertEqual(statuses(v)["MSTR.btc_bought"], "FAIL")
        v = monday(audit=set_value(AUDIT, "monday", "MSTR cash balance / change", "$6.19B / −$310.0m"))
        self.assertEqual(statuses(v)["MSTR.cash"], "FAIL")

    def test_waits_on_retained_edition_notice_incomplete_publication_or_saved_quotes(self):
        a = copy.deepcopy(AUDIT)
        a["monday_notice"] = "Retaining the Sep 20 edition until Strive's claims reconcile."
        self.assertEqual(statuses(monday(audit=a))["complete_edition"], "WAIT")
        self.assertEqual(statuses(monday(pub={"status": "reconciliation_required", "reason": "x"}))["publication_check"],
                         "WAIT")
        self.assertEqual(statuses(monday(saved_quotes=True))["live_quotes"], "WAIT")

    def test_stale_source_blocks_only_when_this_panel_uses_it(self):
        a = copy.deepcopy(AUDIT)
        a["stale_sections"] = ["strategy"]
        self.assertEqual(statuses(monday(audit=a))["live_data"], "WAIT")
        a["stale_sections"] = ["markets"]
        v = monday(audit=a)
        self.assertEqual(statuses(v)["live_data"], "WARN")
        self.assertTrue(v.ready)

    def test_contract_and_image_failures(self):
        a = copy.deepcopy(AUDIT)
        del a["panels"]["monday"]
        self.assertEqual(statuses(monday(audit=a))["contract"], "FAIL")
        self.assertEqual(statuses(monday(image=png(1440, 2400)))["image"], "FAIL")  # taller than 3:4
        self.assertEqual(statuses(monday(image=b"not a png"))["image"], "FAIL")

    def test_digital_exposure_fail_blocks(self):
        c = copy.deepcopy(CHECKS)
        row = next(x for x in c["checks"] if x["panel"] == "monday")
        row["status"] = "FAIL"
        self.assertEqual(statuses(monday(checks=c))["audit"], "FAIL")


class FridayGate(unittest.TestCase):
    def test_clean_render_after_the_close_is_ready(self):
        v = gate.evaluate("friday", audit=AUDIT, checks=CHECKS, png=png(1440, 1920), now=at(2026, 9, 25, 16, 15))
        self.assertTrue(v.ready, v.blockers)

    def test_waits_for_the_close_to_settle(self):
        v = gate.evaluate("friday", audit=AUDIT, checks=CHECKS, png=png(1440, 1920), now=at(2026, 9, 25, 16, 5))
        self.assertEqual(statuses(v)["after_close"], "WAIT")

    def test_waits_until_the_panel_rolls_to_this_week(self):
        v = gate.evaluate("friday", audit=AUDIT, checks=CHECKS, png=png(1440, 1920), now=at(2026, 10, 2, 16, 20))
        s = statuses(v)
        self.assertEqual(s["week_rolled"], "WAIT")
        self.assertEqual(s["todays_marks"], "WAIT")

    def test_catches_the_silent_blank_from_githubs_friday_night_run(self):
        """Sep 25, 8:07 pm: digital-exposure's own audit passed (0 FAIL) but price/NAV was '—' on the image."""
        self.assertEqual(RUNNER_CHECKS["summary"]["FAIL"], 0)
        v = gate.evaluate("friday", audit=RUNNER_AUDIT, checks=RUNNER_CHECKS, png=png(1440, 1920),
                          now=at(2026, 9, 25, 20, 7))
        self.assertFalse(v.ready)
        s = statuses(v)
        self.assertEqual(s["no_blanks"], "WAIT")
        self.assertIn("MSTR price / NAV", next(c.detail for c in v.checks if c.id == "no_blanks"))
        self.assertEqual(s["audit_tiles"], "WAIT")  # the four missing turnover tiles

    def test_digital_exposures_new_blank_tile_fails_mean_wait_not_alarm(self):
        """After its Sep 26 audit round, digital-exposure FAILs these; they are data not loaded yet, so retry."""
        c = copy.deepcopy(RUNNER_CHECKS)
        for row in c["checks"]:
            if row["id"].startswith("turnover."):
                row["status"] = "FAIL"
        c["checks"] += [
            {"panel": "friday", "id": "inputs", "label": "Monday balance inputs loaded for Friday", "status": "FAIL",
             "detail": "could not be validated · OSError: quote host down"},
            {"panel": "friday", "id": "MSTR.price_nav", "label": "MSTR price / NAV drawn on the image", "status": "FAIL",
             "detail": "blank tile"}]
        v = gate.evaluate("friday", audit=RUNNER_AUDIT, checks=c, png=png(1440, 1920), now=at(2026, 9, 25, 20, 7))
        s = statuses(v)
        self.assertEqual(s["audit"], "PASS")
        self.assertEqual(s["audit_tiles"], "WAIT")
        self.assertFalse(v.failed)
        self.assertIn("OSError: quote host down", next(x.detail for x in v.checks if x.id == "audit_tiles"))


class WednesdayGate(unittest.TestCase):
    def test_ready_when_the_ledger_has_the_latest_8k_week(self):
        v = gate.evaluate("wednesday", audit=AUDIT, checks=CHECKS, png=png(1440, 1920), now=at(2026, 9, 23, 12, 35),
                          feed=FEED)
        self.assertTrue(v.ready, v.blockers)
        self.assertEqual(statuses(v)["ledger_week"], "PASS")

    def test_waits_when_a_newer_8k_week_is_missing_from_the_ledger(self):
        feed = copy.deepcopy(FEED)
        newer = copy.deepcopy(de.latest_weekly(feed, "MSTR"))
        newer["accession"] = "test-newer"
        newer["extracted"].update(periodStart="2026-09-21", balanceDate="2026-09-27")
        feed["filings"].append(newer)
        v = gate.evaluate("wednesday", audit=AUDIT, checks=CHECKS, png=png(1440, 1920), now=at(2026, 9, 30, 12, 35),
                          feed=feed)
        self.assertEqual(statuses(v)["ledger_week"], "WAIT")


class Watcher(unittest.TestCase):
    """End to end on SQLite: a real Monday render → audited → Feed draft with the image and a grounded caption."""

    def setUp(self):
        db.engine()

    def _render(self, audit=AUDIT):
        return de.Render(ok=True, commit="1490b3d" + "0" * 33, audit=audit, checks=CHECKS, publication=PUB,
                         pngs={"monday": png(), "wednesday": png(1440, 1920), "friday": png(1440, 1920)})

    def _watch(self, render, day=SEP21, hhmm=(8, 12)):
        from xcp.agents import showcase_watch as w

        sent = []
        with mock.patch.object(w.de, "render", return_value=render), \
                mock.patch.object(w.de, "fetch_feed", return_value=FEED), \
                mock.patch.object(w, "today_ny", return_value=day), \
                mock.patch.object(w, "now_ny", return_value=at(day.year, day.month, day.day, *hhmm)), \
                mock.patch.object(w.notify, "discord", side_effect=lambda *a, **k: sent.append((a, k)) or True):
            out = w.watch("monday", "once", Path(_TMP), "python")
        return out, sent

    def test_ready_run_creates_the_showcase_draft(self):
        out, sent = self._watch(self._render())
        self.assertEqual(out["status"], "ready", out)
        with db.session() as s:
            run = showcase.run_for(s, SEP21.isoformat(), "monday")
            d = s.get(db.Draft, run.draft_id)
            caption = db.current_variants(s, d.id)[0].parts[0]
            req = s.query(db.Request).filter(db.Request.kind == "showcase_captions").first()
        self.assertEqual(run.status, "ready")
        self.assertTrue(run.png)
        self.assertEqual(run.filings["MSTR"]["balance_date"], "2026-09-20")
        self.assertEqual(d.kind, "showcase")
        self.assertIn("$MSTR +950 BTC → 846,000 BTC held", caption)
        self.assertIn("8-K week Sep 14–20", caption)
        self.assertIn("?report=monday", caption)
        self.assertIsNone(req)  # monitor mode (the default): you write the captions, no AI request
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["image_name"], "monday.png")  # the image rides along in Discord
        self.assertEqual(sent[0][1]["kind"], "showcase_ready")  # the one alert that reaches Discord by default

    def test_old_edition_waits_and_records_why(self):
        old = set_value(set_value(AUDIT, "monday", "MSTR balance date", "2026-09-13"),
                        "monday", "ASST balance date", "2026-09-11")
        day = date(2026, 9, 14)  # a Monday with no run yet in this DB
        from xcp.agents import showcase_watch as w

        with mock.patch.object(w.de, "weekly_filings", return_value=de.weekly_filings(FEED, SEP21)):
            out, sent = self._watch(self._render(old), day=day)
        self.assertEqual(out["status"], "waiting")
        self.assertIn("still shows balance dates", out["blockers"][0]["detail"])
        self.assertEqual(sent, [])

    def test_plan_go_no_go(self):
        from xcp.agents import showcase_watch as w

        def plan(day, hhmm, mode="watch", manual=False, panel=None):
            with mock.patch.object(w, "today_ny", return_value=day), \
                    mock.patch.object(w, "now_ny", return_value=at(day.year, day.month, day.day, *hhmm)):
                return w.plan(mode, panel, manual=manual)

        sat, sun, mon = date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)
        self.assertFalse(plan(sat, (20, 13), "preflight")["go"])  # nothing on Sunday
        self.assertTrue(plan(sat, (20, 13), "preflight", manual=True)["go"])  # the Control Room button
        self.assertTrue(plan(sun, (20, 13), "preflight")["go"])  # eve of Monday's Ledger
        self.assertFalse(plan(mon, (6, 41))["go"])  # EST copy of the 07:41 cron: too early
        self.assertTrue(plan(mon, (7, 41))["go"])
        self.assertFalse(plan(date(2026, 9, 29), (7, 41))["go"])  # ordinary Tuesday
        self.assertTrue(plan(date(2026, 10, 13), (7, 41))["go"])  # Tuesday after Columbus Day

    def test_fact_only_captions_pass_the_editor(self):
        from xcp.agents import editor

        values = gate.values_of(AUDIT, "monday")
        filings = {t: de.filing_summary(f) for t, f in de.weekly_filings(FEED, SEP21).items()}
        for panel in ("monday", "wednesday", "friday"):
            text = showcase.default_caption(panel, gate.values_of(AUDIT, panel), filings if panel == "monday" else {})
            flags = [f for f in editor.check_variant([text], {}, [f"{k} {v}" for k, v in
                                                                  gate.values_of(AUDIT, panel).items()])
                     if f.startswith("Unverified")]
            self.assertEqual(flags, [], (panel, text))
        self.assertTrue(values)


if __name__ == "__main__":
    unittest.main()
