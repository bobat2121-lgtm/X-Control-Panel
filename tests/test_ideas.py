"""Idea feed (ideas grouped under post times), the Discord alert gate, and the settings cache. Offline."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-ideas-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database
os.environ["LLM_BACKEND"] = "mock"

from xcp import config, db, ideas, notify  # noqa: E402
from xcp.agents import collect, monitor  # noqa: E402
from xcp.timeutil import NY, utcnow  # noqa: E402

FRI_6PM = datetime(2026, 9, 25, 18, 0, tzinfo=NY)
MON_9AM = datetime(2026, 9, 28, 9, 0, tzinfo=NY)


def _item(s, iid, title, kind="news", at=MON_9AM - timedelta(minutes=30), lane="btc", pillar=None, author="Reuters",
          **meta):
    row, _ = collect.upsert_item(s, id=iid, kind=kind, source="feed", text=title, url=f"https://x.test/{iid}",
                                 author=author, author_name=author, created_at=at,
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


class Occurrences(Base):
    def test_friday_evening_looks_to_saturday_ai_and_monday_premarket(self):
        occ = ideas.occurrences(FRI_6PM)
        up = [(o["slot"], o["post_at"].strftime("%a %H:%M")) for o in occ if not o["passed"]]
        self.assertEqual(up[0], ("ai_noon", "Sat 12:00"))
        self.assertIn(("premarket", "Mon 08:00"), up)  # the next BTC slot, however far away
        passed = [o["slot"] for o in occ if o["passed"]]
        self.assertEqual(passed[0], "friday_close")  # latest first
        mon = next(o for o in occ if o["slot"] == "premarket" and o["date"] == "2026-09-28")
        self.assertEqual(mon["panel"], "monday")  # the Accretion Ledger rides with Monday's pre-market

    def test_weekday_morning(self):
        up = [o["slot"] for o in ideas.occurrences(MON_9AM) if not o["passed"]]
        self.assertEqual(up[:2], ["ai_noon", "midday"])


class Assign(Base):
    def test_stories_go_to_the_next_slot_in_their_lane_and_briefs_absorb_their_items(self):
        with db.session() as s:
            _item(s, "sc", "Circle USDC supply hits a record", pillar="stablecoins")
            _item(s, "ai", "OpenAI ships a new frontier model", lane="ai", pillar="ai_models")
            _item(s, "dc", "Strategy prices a new STRC offering", pillar="digital_credit")
            _item(s, "rnd", "Random account loves AI", kind="x_post", lane="ai", pillar="ai_models", author="rando")
            s.add(db.Brief(run_slot="midday", run_date="2026-09-28", title="STRC offering: what it funds",
                           pillar="digital_credit", priority=3, item_ids=["dc"],
                           sources=[{"title": "t", "url": "https://x.test/dc", "publisher": "Reuters", "kind": "news"}]))
            s.commit()
        occ = ideas.occurrences(MON_9AM)
        out = ideas.assign(monitor.stream(hours=72), ideas.load_briefs(since_hours=24), occ, MON_9AM)
        ai = out["2026-09-28_ai_noon"]
        midday = out["2026-09-28_midday"]
        self.assertEqual([x["title"] for x in ai], ["OpenAI ships a new frontier model"])  # low-traction post dropped
        self.assertEqual(midday[0]["kind"], "brief")  # desk picks lead
        self.assertTrue(midday[0]["hot"])
        titles = [x["title"] for x in midday]
        self.assertIn("Circle USDC supply hits a record", titles)
        self.assertNotIn("Strategy prices a new STRC offering", titles)  # already inside the brief
        self.assertEqual(midday[0]["items"][0]["url"], "https://x.test/dc")

    def test_off_day_brief_goes_to_the_next_slot_in_its_lane(self):
        with db.session() as s:
            s.add(db.Brief(run_slot="midday", run_date="2026-09-26", title="Saturday manual run", pillar="bitcoin",
                           priority=2, sources=[]))
            s.commit()
        occ = ideas.occurrences(datetime(2026, 9, 26, 15, 0, tzinfo=NY))
        with mock.patch.object(ideas, "utcnow", return_value=utcnow()):
            out = ideas.assign([], ideas.load_briefs(since_hours=24), occ, datetime(2026, 9, 26, 15, 0, tzinfo=NY))
        self.assertEqual([x["title"] for x in out["2026-09-28_premarket"]], ["Saturday manual run"])

    def test_related_finds_the_same_story_in_other_words(self):
        a = {"key": "a", "title": "Circle and Tether freeze Bitget hack funds", "newest": "1"}
        b = {"key": "b", "title": "Tether, Circle freeze wallets tied to Bitget exploit", "newest": "2"}
        c = {"key": "c", "title": "Waymo expands to Denver", "newest": "3"}
        self.assertEqual([x["key"] for x in ideas.related(a, [a, b, c])], ["b"])


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


class SettingsCache(Base):
    def test_saving_settings_is_seen_immediately(self):
        self.assertEqual(config.settings()["alerts"]["discord"], ["showcase_ready", "test"])
        config.save("settings", {"alerts": {"discord": ["desk"]}})
        self.assertEqual(config.settings()["alerts"]["discord"], ["desk"])  # the cache was dropped on save
        config.settings()["alerts"]["discord"].append("mutated")
        self.assertEqual(config.settings()["alerts"]["discord"], ["desk"])  # callers get their own copy


if __name__ == "__main__":
    unittest.main()
