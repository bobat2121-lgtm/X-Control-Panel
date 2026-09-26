"""News monitor + desk, offline (network and AI mocked)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-mon-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database
os.environ["LLM_BACKEND"] = "mock"
os.environ.pop("X_BEARER_TOKEN", None)

from xcp import db  # noqa: E402
from xcp.agents import collect, desk, monitor  # noqa: E402
from xcp.sources import rss  # noqa: E402
from xcp.timeutil import NY, utcnow  # noqa: E402

GN = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fed requests comment on stablecoin rules under GENIUS Act - Reuters</title>
<link>https://news.google.com/a</link><pubDate>%s</pubDate><source url="https://reuters.com">Reuters</source>
<description>&lt;a href="x"&gt;link list&lt;/a&gt;</description></item></channel></rss>"""


def _news(s, iid, title, publisher, minutes_ago=10, **meta):
    row, _ = collect.upsert_item(s, id=iid, kind="news", source="feed", text=title, url=f"https://x.test/{iid}",
                                 author=publisher, author_name=publisher,
                                 created_at=utcnow() - timedelta(minutes=minutes_ago), lane_hint="btc",
                                 pillar_hint=meta.pop("pillar_hint", None), meta={"title": title, **meta})
    return row


class Base(unittest.TestCase):
    def setUp(self):
        db.engine()
        with db.session() as s:
            s.query(db.Item).delete()
            s.query(db.Brief).delete()
            s.commit()
        for k in (monitor.PINGS, monitor.PINGED, "config:settings"):
            db.kv_delete(k)


class Parsing(Base):
    def test_google_news_title_and_publisher(self):
        body = GN % datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT").encode()
        resp = mock.Mock(content=body, raise_for_status=lambda: None)
        with mock.patch.object(rss.httpx, "get", return_value=resp):
            e = rss.fetch_feed("GN", "https://news.google.com/rss/search?q=x")[0]
        self.assertEqual(e["title"], "Fed requests comment on stablecoin rules under GENIUS Act")
        self.assertEqual(e["publisher"], "Reuters")

    def test_no_keyword_story_is_general_bitcoin_not_digital_credit(self):
        self.assertEqual(collect.classify("Bitget hacker moves stolen coins", "btc")[1], "bitcoin")
        self.assertEqual(collect.classify("Circle's USDC supply jumps", "btc")[1], "stablecoins")


class Stories(Base):
    def test_same_story_across_outlets_groups_and_trends(self):
        with db.session() as s:
            ids = [_news(s, "n1", "Fed proposes stablecoin issuer rules under GENIUS Act", "Reuters").id,
                   _news(s, "n2", "Fed proposes new stablecoin issuer rules, GENIUS Act", "CoinDesk").id,
                   _news(s, "n3", "GENIUS Act: Fed proposes rules for stablecoin issuers", "Cointelegraph").id,
                   _news(s, "n4", "Waymo expands robotaxi service to Denver", "Electrek").id]
            s.commit()
        monitor.assign_clusters(ids)
        by_title = {c["title"][:20]: c for c in monitor.stream(hours=2)}
        fed = next(c for t, c in by_title.items() if t.startswith("Fed proposes"))
        self.assertEqual(fed["publishers"], 3)
        with mock.patch.object(monitor.notify, "discord") as ping, \
                mock.patch.object(monitor, "_quiet", return_value=False):
            events = monitor.evaluate(ids)
        self.assertTrue(any(e["reason"].startswith("Trending") for e in events))
        self.assertEqual(ping.call_count, 1)  # one story, one ping

    def test_relevance_rules(self):
        with db.session() as s:
            xrp = _news(s, "a", "XRP hits all-time high after SEC settlement", "Bitget")
            gn = _news(s, "b", "Exchange hack losses rise", "KuCoin", google=True, pillar_hint="legislation")
            tag = _news(s, "c", "Kalshi loses appeal", "Cointelegraph", pillar_hint="legislation")
            sc = _news(s, "d", "XRP Ledger adds USDC stablecoin support", "Decrypt")
            s.commit()
            self.assertEqual(monitor.relevance(xrp), 0)  # altcoin, no lane keyword
            self.assertEqual(monitor.relevance(gn), 0)  # Google News needs a real keyword
            self.assertEqual(monitor.relevance(tag), 1)  # a topic feed counts on its own
            self.assertGreater(monitor.relevance(sc), 0)  # altcoin + stablecoin still counts

    def test_breaking_words_match_whole_words_only(self):
        with db.session() as s:
            faq = _news(s, "f", "SEC Issues FAQs on stablecoin staking receipts", "SEC")
            sue = _news(s, "g", "CFTC sues stablecoin issuer over reserves", "Reuters")
            s.commit()
            self.assertEqual(monitor.priority_reason(faq), "")
            self.assertTrue(monitor.priority_reason(sue).startswith("Breaking (sues)"))

    def test_pings_respect_quiet_hours_freshness_and_hourly_limit(self):
        with db.session() as s:
            old = _news(s, "o", "Senate passes stablecoin bill", "AP", minutes_ago=600)
            fresh = [_news(s, f"p{i}", f"Senate passes stablecoin bill number {i} amendment {i*7}", f"Outlet{i}")
                     for i in range(7)]
            s.commit()
        with mock.patch.object(monitor.notify, "discord") as ping, \
                mock.patch.object(monitor, "_quiet", return_value=True):
            monitor.evaluate([old.id] + [f.id for f in fresh])
        self.assertEqual(ping.call_count, 0)  # overnight: flagged, not pinged
        db.kv_delete(monitor.PINGED)
        with mock.patch.object(monitor.notify, "discord") as ping, \
                mock.patch.object(monitor, "_quiet", return_value=False):
            monitor.evaluate([old.id] + [f.id for f in fresh])
        self.assertEqual(ping.call_count, 5)  # hourly cap; the 10-hour-old story never pings

    def test_quiet_hours_window(self):
        self.assertTrue(monitor._quiet(datetime(2026, 9, 28, 23, 30, tzinfo=NY)))
        self.assertTrue(monitor._quiet(datetime(2026, 9, 28, 5, 0, tzinfo=NY)))
        self.assertFalse(monitor._quiet(datetime(2026, 9, 28, 8, 0, tzinfo=NY)))


class Budget(Base):
    def test_searches_cannot_spend_the_watchlist_reserve(self):
        with mock.patch.object(collect, "x_reads_today", return_value=150), \
                mock.patch.object(collect, "x_reads_this_month", return_value=1000):
            self.assertEqual(collect.x_budget("watchlist"), 50)  # 200 daily cap - 150
            self.assertEqual(collect.x_budget("search"), 0)  # 200 - 60 reserved - 150 < 0


class Desk(Base):
    def test_desk_writes_briefs_not_drafts(self):
        with db.session() as s:
            _news(s, "d1", "Strategy proposes daily dividends for STRC", "CoinDesk", priority="Breaking (proposes)")
            _news(s, "d2", "Circle USDC supply hits a record", "Decrypt")
            s.commit()
        before = db.session().query(db.Draft).count()
        with mock.patch.object(desk.market, "take_snapshot", return_value={"as_of_ny": "now", "btc": {"price": 1}}), \
                mock.patch.object(desk.collect, "collect", return_value={"x_reads": 0}), \
                mock.patch.object(desk.notify, "discord") as digest:
            out = desk.run_desk("premarket")
        self.assertGreaterEqual(out["briefs"], 2)
        with db.session() as s:
            briefs = s.query(db.Brief).all()
            self.assertTrue(all(b.sources and b.sources[0]["url"] for b in briefs))
            self.assertEqual(s.query(db.Draft).count(), before)  # no AI drafts in monitor mode
        digest.assert_called_once()

    def test_slots_route_to_the_desk_in_monitor_mode(self):
        from xcp import scheduler

        with mock.patch.object(desk, "run_desk", return_value={"briefs": 1}) as run:
            self.assertEqual(scheduler._dispatch("premarket"), {"briefs": 1})
        run.assert_called_once_with("premarket")


if __name__ == "__main__":
    unittest.main()
