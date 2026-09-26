"""📊 Scoreboard: what you posted, how it did, and whether you're on the 80/20 mix."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import select

from panel import cache
from panel.common import esc_md, hero, is_owner, section
from xcp import config, db
from xcp.agents import analyst
from xcp.agents.collect import classify
from xcp.timeutil import NY, aware, utcnow

TARGET_LABELS = {"digital_credit": "Digital credit", "bitcoin": "Bitcoin", "macro": "Macro", "ai": "AI"}
PALETTE = ["#0E0E0C", "#CDB891", "#8C8572", "#6B7A26"]  # ink, tan, mute, olive (the accent series)

days = int(st.session_state.get("sb_days") or 30)


def _fetch(days: int) -> tuple[list, list]:
    since = utcnow() - timedelta(days=days)
    with db.session() as s:
        posts = list(s.scalars(select(db.Post).where(db.Post.posted_at >= since)
                               .order_by(db.Post.posted_at.desc())).all())
        drafts = list(s.scalars(select(db.Draft).where(db.Draft.created_at >= since,
                                                       db.Draft.kind.in_(["regular", "showcase"]))).all())
    return posts, drafts


posts, drafts = cache.get(("score", days), lambda: _fetch(days), ttl=120)  # metrics import nightly

rows = []
for p in posts:
    m = p.latest_metrics or {}
    imp = m.get("impressions") or 0
    eng = sum(m.get(k, 0) or 0 for k in ("likes", "reposts", "replies", "quotes"))
    rows.append({"posted": aware(p.posted_at).astimezone(NY), "group": TARGET_LABELS.get(config.target_group(p.pillar)),
                 "pillar": p.pillar, "tone": p.tone or "—", "style": p.style or "—", "kind": p.kind,
                 "impressions": imp, "likes": m.get("likes", 0), "reposts": m.get("reposts", 0),
                 "replies": m.get("replies", 0), "bookmarks": m.get("bookmarks", 0),
                 "eng_rate": round(eng * 100 / imp, 2) if imp else None, "text": p.text[:140], "link": p.url})
df = pd.DataFrame(rows)

_eng = df["eng_rate"].dropna().mean() if len(df) and df["eng_rate"].notna().any() else None
hero("SCOREBOARD.EXE", "What's <em>working</em>.",
     "Your posts, their reach, and whether you're on the 80/20 mix. Your X posts are imported nightly.",
     stats=[(len(df), f"posts · {days}d"), (f"{df['impressions'].mean():,.0f}" if len(df) else "—", "avg impressions"),
            (f"{_eng:.2f}%" if _eng is not None else "—", "avg engagement", bool(_eng and _eng >= 3)),
            (f"{sum(1 for p in posts if p.draft_id)}/{len(posts)}" if posts else "—", "written in the panel")],
     kicker=f"{days}-day window", icon="📊")
st.segmented_control("Window", [7, 14, 30, 90], default=30, format_func=lambda d: f"{d}d", key="sb_days")

if df.empty:
    st.info("No tracked posts yet. Posts are imported nightly from X once your handle and X API key are set, "
            "or when you click ✅ Posted in the Feed.", icon="📊")
else:
    section("Mix vs target", f"last {days} days")
    m = analyst.mix(days)
    mix_df = pd.DataFrame([{"group": TARGET_LABELS.get(g, g), "actual %": round(m["shares"].get(g, 0), 1),
                            "target %": t} for g, t in m["targets"].items()]).set_index("group")
    c = st.columns([1.2, 1])
    wk = df.assign(week=df["posted"].dt.to_period("W").dt.start_time.dt.date).pivot_table(
        index="week", columns="group", values="impressions", aggfunc="count", fill_value=0)
    wk.index = [f"wk of {d:%b %d}" for d in wk.index]
    c[0].bar_chart(wk, stack=True, height=260, color=PALETTE[:len(wk.columns)])
    c[1].dataframe(mix_df, width="stretch")

    section("What's working", "by group, tone and style")
    c = st.columns(3)
    for col, dim in zip(c, ["group", "tone", "style"]):
        agg = df.groupby(dim).agg(posts=("impressions", "size"), avg_impr=("impressions", "mean"),
                                  avg_eng=("eng_rate", "mean")).round(1).sort_values("avg_impr", ascending=False)
        col.caption(f"By {dim}")
        col.dataframe(agg, width="stretch")
    by_hour = df.assign(hour=df["posted"].dt.hour).groupby("hour")["impressions"].mean().round(0)
    st.caption("Avg impressions by hour posted (ET)")
    st.bar_chart(by_hour, height=180, color="#0E0E0C")

    section("Posts")
    st.dataframe(df.sort_values("posted", ascending=False), hide_index=True, width="stretch",
                 column_config={"link": st.column_config.LinkColumn("link", display_text="open ↗"),
                                "posted": st.column_config.DatetimeColumn("posted", format="MMM D, h:mm a"),
                                "text": st.column_config.TextColumn("text", width="large")})

section("Draft funnel")
status_counts = Counter(d.status for d in drafts)
reasons = Counter(d.dismiss_reason for d in drafts if d.status == "dismissed" and d.dismiss_reason)
f = st.columns(4)
f[0].metric("Drafts generated", len(drafts))
f[1].metric("Posted", status_counts.get("posted", 0))
f[2].metric("Edited, not posted", status_counts.get("edited", 0))
f[3].metric("Dismissed", status_counts.get("dismissed", 0))
if reasons:
    st.caption("Why drafts got dismissed (fed back to the writer through your edits and choices)")
    st.bar_chart(pd.Series(reasons), height=180, color="#CDB891")

memo = cache.get(("score", "memo"), lambda: db.kv_get("weekly_memo"), ttl=300)
if memo and memo.get("text"):
    section("Weekly memo", memo.get("date", ""))
    st.markdown(esc_md(memo["text"]))

if is_owner():
    with st.expander("➕ Log a post manually"):
        with st.form("log_post", clear_on_submit=True):
            url = st.text_input("Post URL")
            text = st.text_area("Post text")
            pillar = st.selectbox("Pillar", ["(auto)"] + list(config.pillars()))
            tone = st.selectbox("Tone", ["analytical", "timely", "funny"])
            if st.form_submit_button("Log") and (url or text):
                p = classify(text)[1] if pillar == "(auto)" else pillar
                analyst.log_post(None, url, text, p, tone, None)
                cache.bust("score", "feed")
                st.rerun()
