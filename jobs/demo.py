"""Seed a local DB with demo content so the panel can be explored before keys exist.
Uses the mock LLM; market data and RSS/EDGAR are fetched live. Delete data/app.db to start clean."""
from __future__ import annotations

import os
import random
from datetime import timedelta

from xcp import db
from xcp.timeutil import today_ny, utcnow

DEMO_POSTS = [
    ("btc_macro_guy", "digital_credit", "STRC back at par after the rate reset. The thermostat works: the dividend adjusts, the price stays near $100, and the BTC reserve does the heavy lifting."),
    ("credit_analyst", "digital_credit", "SATA now pays daily. Fixed income finally meets a 24/7 asset. The part people miss: coverage ratios at today's BTC price."),
    ("onchain_nerd", "bitcoin", "Hashrate at a new high while fees sit near the floor. Miners are betting on the long game."),
    ("macro_desk", "macro", "10Y drifting lower into CPI. If the print is soft, liquidity-sensitive assets get the first bid."),
    ("ai_evals", "ai_benchmarks", "New frontier model posts a big jump on SWE-bench Verified. The gap between the top 3 labs is now inside the noise."),
    ("robotaxi_watch", "physical_ai", "Robotaxi service expands to two more metros this quarter. Driverless miles are compounding faster than most forecasts."),
    ("model_drops", "ai_models", "Open-weights release today closes much of the gap to closed models on reasoning tasks."),
]


def seed() -> None:
    os.environ["LLM_BACKEND"] = "mock"
    from xcp import scheduler
    from xcp.agents import collect, strategist

    with db.session() as s:
        for i, (author, pillar, text) in enumerate(DEMO_POSTS):
            collect.upsert_item(
                s, id=f"x:demo{i}", kind="x_post", source="demo", text=f"[demo] {text}",
                url=f"https://x.com/{author}/status/100000000000000{i}", author=author, author_name=author.title(),
                author_followers=random.randint(5_000, 400_000), created_at=utcnow() - timedelta(hours=random.randint(1, 10)),
                metrics={"like_count": random.randint(50, 3000), "retweet_count": random.randint(5, 400),
                         "quote_count": random.randint(0, 80), "reply_count": random.randint(5, 200)},
                pillar_hint=pillar)
        s.commit()

    for job in ("premarket", "ai_noon", "midday"):
        print(scheduler.run_job(job, trigger="demo"))
    strategist.daily()

    # one idea ready and slotted into the next showcase
    from xcp import showcase

    with db.session() as s:
        idea = s.query(db.BuildIdea).order_by(db.BuildIdea.id).first()
        nxt = next(iter(showcase.lineup(14)), None)
        if idea and nxt:
            idea.status = "ready"
            idea.shipped_url = "https://example.com/strc-par-keeper"
            idea.showcase_date, idea.showcase_slot = nxt["date"].isoformat(), nxt["slot"]
        for i in range(8):
            pillar = random.choice(["digital_credit", "digital_credit", "bitcoin", "macro", "ai_models"])
            posted = utcnow() - timedelta(days=random.uniform(0, 6))
            pid = f"demo-post-{i}"
            if s.get(db.Post, pid):
                continue
            m = {"impressions": random.randint(800, 40000), "likes": random.randint(5, 600),
                 "reposts": random.randint(0, 90), "replies": random.randint(0, 50), "quotes": random.randint(0, 20),
                 "bookmarks": random.randint(0, 120)}
            s.add(db.Post(id=pid, url="", text=f"[demo] tracked post #{i} about {pillar}", posted_at=posted,
                          pillar=pillar, lane="ai" if pillar.startswith("ai") else "btc",
                          tone=random.choice(["analytical", "timely", "funny"]), source="demo", latest_metrics=m))
            s.add(db.PostMetric(post_id=pid, **m))
        s.add(db.CalendarEvent(date=(today_ny() + timedelta(days=2)).isoformat(), time="08:30",
                               title="[demo] Example macro release", pillar="macro", importance=3))
        s.commit()
    print("Demo data ready. Run: streamlit run streamlit_app.py")
