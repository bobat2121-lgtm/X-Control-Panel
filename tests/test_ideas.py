"""Idea feed (ideas grouped under post times), the Discord alert gate, and the settings cache. Offline."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-ideas-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database
os.environ["LLM_BACKEND"] = "mock"

from xcp import config, db, ideas, notify  # noqa: E402
from xcp.agents import collect, monitor  # noqa: E402
from xcp.timeutil import NY  # noqa: E402

FRI_6PM = datetime(2026, 9, 25, 18, 0, tzinfo=NY)
MON_9AM = datetime(2026, 9, 28, 9, 0, tzinfo=NY)
SAT_10AM = datetime(2026, 9, 26, 10, 0, tzinfo=NY)


def _item(s, iid, title, kind="news", at=MON_9AM - timedelta(minutes=30), lane="btc", pillar=None, author="Reuters",
          **meta):
    row, _ = collect.upsert_item(s, id=iid, kind=kind, source="feed", text=title, url=f"https://x.test/{iid}",
                                 author=author, author_name=author, created_at=at.astimezone(timezone.utc),
                                 lane_hint=lane, pillar_hint=pillar, meta={"title": title, **meta})
    return row


class Base(unittest.TestCase):
    def setUp(self):
        db.engine()
        with db.session() as s:
            s.query(db.Item).delete()
            s.query(db.Brief).delete()
            s.commit()
        db.kv_delete("config:settings")


def _stream(now):
    """The monitor's story stream as of `now` (its 72-hour window is measured from the clock)."""
    with mock.patch.object(monitor, "utcnow", return_value=now.astimezone(timezone.utc)):
        return monitor.stream(hours=72)


def _brief(s, title, run_slot, run_date, at, pillar="bitcoin", priority=2, **kw):
    s.add(db.Brief(run_slot=run_slot, run_date=run_date, title=title, pillar=pillar, priority=priority,
                   created_at=at.astimezone(timezone.utc), sources=kw.pop("sources", []), **kw))


class DaySlots(Base):
    def test_todays_desk_runs_per_lane(self):
        btc = ideas.desk_runs("btc", MON_9AM, [{"run_slot": "premarket", "run_date": "2026-09-28"}] * 2)
        self.assertEqual([(r["at"].strftime("%H:%M"), r["done"], r["picks"]) for r in btc],
                         [("07:05", True, 2), ("12:50", False, 0)])  # 4:10 runs on Fridays only
        self.assertEqual([r["at"].strftime("%H:%M") for r in ideas.desk_runs("ai", MON_9AM)], ["11:20"])
        self.assertEqual([r["at"].strftime("%H:%M") for r in ideas.desk_runs("btc", FRI_6PM)], ["07:05", "12:50", "16:10"])
        self.assertEqual(ideas.desk_runs("btc", SAT_10AM), [])  # no BTC desk on weekends

    def test_a_desk_run_github_started_too_late_says_so(self):
        at = lambda hh, mm, ran=(): [(r["slot"], r["state"]) for r in ideas.desk_runs(  # noqa: E731
            "btc", datetime(2026, 9, 28, hh, mm, tzinfo=NY), [], set(ran))]
        self.assertEqual(at(13, 30), [("premarket", "missed"), ("midday", "due")])  # 12:50 still inside its window
        self.assertEqual(at(15, 0), [("premarket", "missed"), ("midday", "missed")])
        self.assertEqual(at(15, 0, ran=["midday"]), [("premarket", "missed"), ("midday", "empty")])

    def test_report_panels_are_this_weeks_then_next_weeks_from_saturday(self):
        week = lambda now: [(o["panel"], o["date"]) for o in ideas.showcase_week(now)]  # noqa: E731
        self.assertEqual(week(MON_9AM), [("monday", "2026-09-28"), ("wednesday", "2026-09-30"), ("friday", "2026-10-02")])
        self.assertEqual(week(datetime(2026, 10, 3, 10, 0, tzinfo=NY)),
                         [("monday", "2026-10-05"), ("wednesday", "2026-10-07"), ("friday", "2026-10-09")])
        self.assertEqual(week(datetime(2026, 9, 9, 9, 0, tzinfo=NY))[0], ("monday", "2026-09-08"))  # Labor Day week


class Assign(Base):
    def test_lanes_desk_picks_first_and_briefs_absorb_their_items(self):
        with db.session() as s:
            _item(s, "sc", "Circle USDC supply hits a record", pillar="stablecoins")
            _item(s, "ai", "OpenAI ships a new frontier model", lane="ai", pillar="ai_models")
            _item(s, "dc", "Strategy prices a new STRC offering", pillar="digital_credit")
            _item(s, "rnd", "Random account loves AI", kind="x_post", lane="ai", pillar="ai_models", author="rando")
            _brief(s, "STRC offering: what it funds", "premarket", "2026-09-28", MON_9AM - timedelta(hours=2),
                   pillar="digital_credit", priority=3, item_ids=["dc"],
                   sources=[{"title": "t", "url": "https://x.test/dc", "publisher": "Reuters", "kind": "news"}])
            s.commit()
        out = ideas.assign(_stream(MON_9AM), ideas.load_briefs(since_hours=24 * 30), MON_9AM)
        self.assertEqual([x["title"] for x in out["ai_today"]], ["OpenAI ships a new frontier model"])  # low-traction post dropped
        btc = out["btc_today"]
        self.assertEqual((btc[0]["kind"], btc[0]["hot"]), ("brief", True))  # desk picks lead
        titles = [x["title"] for x in btc]
        self.assertIn("Circle USDC supply hits a record", titles)
        self.assertNotIn("Strategy prices a new STRC offering", titles)  # already inside the brief
        self.assertEqual(btc[0]["items"][0]["url"], "https://x.test/dc")

    def test_rolling_today_then_yesterday_then_gone(self):
        with db.session() as s:
            _item(s, "b30", "Senate schedules stablecoin markup vote", at=MON_9AM - timedelta(hours=30), pillar="legislation")
            _item(s, "b50", "Treasury yields climb after jobs report", at=MON_9AM - timedelta(hours=50), pillar="macro")
            _item(s, "a30", "Figure unveils a humanoid for warehouses", at=MON_9AM - timedelta(hours=30), lane="ai",
                  pillar="physical_ai")
            _item(s, "a40", "Anthropic publishes new benchmark results", at=MON_9AM - timedelta(hours=40), lane="ai",
                  pillar="ai_benchmarks")
            s.commit()
        out = {k: [x["title"] for x in v] for k, v in ideas.assign(_stream(MON_9AM), [], MON_9AM).items()}
        self.assertEqual(out["btc_today"], [])
        self.assertEqual(out["btc_yesterday"], ["Senate schedules stablecoin markup vote"])  # 24-48 hours
        self.assertEqual(out["ai_today"], ["Figure unveils a humanoid for warehouses"])  # AI's today is 36 hours
        self.assertEqual(out["ai_yesterday"], ["Anthropic publishes new benchmark results"])
        self.assertNotIn("Treasury yields climb after jobs report", sum(out.values(), []))  # over 48 hours: gone

    def test_friday_afternoon_picks_are_still_today_on_saturday_morning(self):
        with db.session() as s:
            _brief(s, "What Friday's close says about STRC", "friday_close", "2026-09-25",
                   datetime(2026, 9, 25, 16, 15, tzinfo=NY), pillar="digital_credit")
            s.commit()
        briefs = ideas.load_briefs(since_hours=24 * 30)
        titles = lambda now, k: [x["title"] for x in ideas.assign([], briefs, now)[k]]  # noqa: E731
        self.assertEqual(titles(SAT_10AM, "btc_today"), ["What Friday's close says about STRC"])
        self.assertEqual(titles(datetime(2026, 9, 27, 10, 0, tzinfo=NY), "btc_yesterday"),
                         ["What Friday's close says about STRC"])
        self.assertEqual(titles(datetime(2026, 9, 27, 18, 0, tzinfo=NY), "btc_yesterday"), [])

    def test_related_finds_the_same_story_in_other_words(self):
        a = {"key": "a", "title": "Circle and Tether freeze Bitget hack funds", "newest": "1"}
        b = {"key": "b", "title": "Tether, Circle freeze wallets tied to Bitget exploit", "newest": "2"}
        c = {"key": "c", "title": "Waymo expands to Denver", "newest": "3"}
        self.assertEqual([x["key"] for x in ideas.related(a, [a, b, c])], ["b"])


class Surfaced(Base):
    """An idea's time is when its event first surfaced, not when the latest repost landed."""

    def test_a_repost_is_dated_by_the_first_report(self):
        with db.session() as s:
            _item(s, "orig", "Fed proposes reserve and capital rules for stablecoin issuers under GENIUS Act",
                  at=MON_9AM - timedelta(days=3), pillar="stablecoins", author="Reuters")
            _item(s, "repost", "Fed proposes capital and reserve rules for stablecoin issuers, GENIUS Act",
                  at=MON_9AM - timedelta(hours=2), pillar="stablecoins", author="Cryptonews")
            s.commit()
        index = ideas.load_event_index(days=30)
        repost = next(c for c in monitor.stream(hours=24 * 10) if c["lead"].id == "repost")
        idea = ideas.stamp(ideas.from_story(repost), index)
        self.assertEqual(idea["surfaced"][:10], (MON_9AM - timedelta(days=3)).astimezone(timezone.utc).date().isoformat())
        self.assertEqual(idea["surfaced_via"]["publisher"], "Reuters")

    def test_recurring_headlines_with_different_figures_are_different_events(self):
        with db.session() as s:
            _item(s, "w1", "Strategy acquires 1,000 BTC for $85 million in weekly purchase",
                  at=MON_9AM - timedelta(days=7), pillar="digital_credit")
            _item(s, "w2", "Strategy acquires 2,500 BTC for $210 million in weekly purchase",
                  at=MON_9AM - timedelta(hours=1), pillar="digital_credit")
            s.commit()
        index = ideas.load_event_index(days=30)
        this_week = next(c for c in monitor.stream(hours=24 * 10) if c["lead"].id == "w2")
        idea = ideas.stamp(ideas.from_story(this_week), index)
        self.assertIsNone(index.origin("Strategy acquires 2,500 BTC for $210 million in weekly purchase",
                                       MON_9AM - timedelta(hours=1)))
        self.assertEqual(idea["surfaced_via"]["publisher"], "Reuters")  # its own report
        self.assertEqual(idea["surfaced"], idea["items"][0]["at"])

    def test_a_post_first_line_is_not_a_headline(self):
        with db.session() as s:
            _item(s, "t1", "I am a shareholder of both $MSTR and $ASST.", kind="x_post",
                  at=MON_9AM - timedelta(days=9), author="someone")
            s.commit()
        self.assertEqual(ideas.load_event_index(days=30).rows, [])  # too short to stand as a first report


class Alerts(Base):
    def test_only_showcase_go_live_reaches_discord_by_default(self):
        resp = mock.Mock(raise_for_status=lambda: None)
        with mock.patch.object(notify.httpx, "post", return_value=resp) as post,                 mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook"}):
            self.assertFalse(notify.discord("🗞 desk digest", "x", kind="desk"))
            self.assertFalse(notify.discord("⚡ breaking", "x", kind="news_priority"))
            self.assertFalse(notify.discord("❌ job failed", "x", kind="errors"))
            self.assertTrue(notify.discord("🟢 The Coupon Sheet ready", "x", kind="showcase_ready"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(db.kv_get("notify:held:desk")["title"], "🗞 desk digest")  # held, and visible in the panel

    def test_kinds_can_be_switched_on(self):
        db.kv_set("config:settings", {"alerts": {"discord": ["showcase_ready", "watchlist"]}})
        self.assertTrue(notify.allowed("watchlist"))
        self.assertFalse(notify.allowed("desk"))


class ShowcaseState(Base):
    OCC = {"panel": "monday", "date": "2026-09-28", "slot": "premarket"}  # the Accretion Ledger, window 7:40-11:30

    def setUp(self):
        super().setUp()
        with db.session() as s:
            s.query(db.ShowcaseRun).delete()
            s.commit()

    def _run(self, status, checked, **kw):
        with db.session() as s:
            s.add(db.ShowcaseRun(run_date="2026-09-28", panel="monday", slot="premarket", title="The Accretion Ledger",
                                 status=status, updated_at=checked.astimezone(timezone.utc), **kw))
            s.commit()

    def at(self, hh, mm):
        return datetime(2026, 9, 28, hh, mm, tzinfo=NY)

    def test_before_the_window_it_is_scheduled_not_waiting(self):
        sc = ideas.showcase_state(self.OCC, self.at(7, 0))
        self.assertEqual((sc["phase"], sc["title"], sc["quiet"]), ("scheduled", "The Accretion Ledger", False))

    def test_window_open_but_no_check_yet(self):
        self.assertEqual(ideas.showcase_state(self.OCC, self.at(7, 50))["phase"], "waiting")
        self.assertFalse(ideas.showcase_state(self.OCC, self.at(7, 50))["quiet"])
        self.assertTrue(ideas.showcase_state(self.OCC, self.at(8, 10))["quiet"])  # 30 min in, the watcher never showed

    def test_waiting_says_why_and_notices_a_silent_watcher(self):
        self._run("waiting", self.at(8, 8), attempts=12,
                  blockers=[{"id": "pregate", "status": "WAIT", "detail": "waiting for this week's 8-K: Strive"}])
        sc = ideas.showcase_state(self.OCC, self.at(8, 10))
        self.assertEqual((sc["phase"], sc["blockers"], sc["attempts"], sc["quiet"]),
                         ("waiting", ["waiting for this week's 8-K: Strive"], 12, False))
        self.assertTrue(ideas.showcase_state(self.OCC, self.at(8, 40))["quiet"])

    def test_ready_and_posted(self):
        self._run("ready", self.at(8, 14), ready_at=self.at(8, 14).astimezone(timezone.utc),
                  audit_summary={"PASS": 41, "WARN": 2, "FAIL": 0})
        sc = ideas.showcase_state(self.OCC, self.at(12, 0))  # past the deadline: still ready until you post it
        self.assertEqual((sc["phase"], sc["audit"]["PASS"]), ("ready", 41))

    def test_a_run_left_waiting_after_the_deadline_is_missed(self):
        self._run("waiting", self.at(9, 0))
        self.assertEqual(ideas.showcase_state(self.OCC, self.at(11, 45))["phase"], "missed")
        self.assertIsNone(ideas.showcase_state({"panel": None, "date": "2026-09-29"}))


class SettingsCache(Base):
    def test_saving_settings_is_seen_immediately(self):
        self.assertEqual(config.settings()["alerts"]["discord"], ["showcase_ready", "test"])
        config.save("settings", {"alerts": {"discord": ["desk"]}})
        self.assertEqual(config.settings()["alerts"]["discord"], ["desk"])  # the cache was dropped on save
        config.settings()["alerts"]["discord"].append("mutated")
        self.assertEqual(config.settings()["alerts"]["discord"], ["desk"])  # callers get their own copy


if __name__ == "__main__":
    unittest.main()
