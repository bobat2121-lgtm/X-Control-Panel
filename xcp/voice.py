"""Voice drafts: write and preview a new voice profile without touching the live one, then make it live.

The live profile is config "voice" (every writer uses it). The draft lives in kv "voice:draft". A preview runs
the draft through the real writer (Codex on your ChatGPT) on a fixed set of briefs, so you can judge the voice
on the same posts each time. Making it live keeps every earlier live version in kv "voice:history".
"""
from __future__ import annotations

import copy

from xcp import config, db, llm, xtext
from xcp.agents import context, editor
from xcp.timeutil import utcnow

KV_DRAFT, KV_HISTORY, KV_PREVIEW, KV_BRIEFS = "voice:draft", "voice:history", "voice:preview", "voice:preview_briefs"

# Facts are from the Sep 26, 2026 Digital Credit Report render, the Sep 21 8-Ks and the calendar feeds.
DEFAULT_BRIEFS = [
    {"id": "P1", "kind": "short post", "title": "SATA holds par, STRC doesn't",
     "angle": "Why one engine is glued to par and the other isn't.",
     "facts": ["$SATA closed at or above $100 in 15 of the last 20 sessions",
               "$STRC closed at or above $100 in 0 of the last 20 sessions",
               "$SATA last close $100.01, 13.00% dividend rate",
               "$STRC last close $98.54, 12% rate (12.18% effective yield)",
               "Strategy carries about $6.75B of debt principal senior to its preferreds",
               "Strive has zero debt"]},
    {"id": "P2", "kind": "short post", "title": "Strategy's quiet week",
     "angle": "They bought BTC and their own credit with cash instead of issuing stock. Why I like that.",
     "facts": ["Strategy bought 950 BTC in the 8-K week Sep 14-20 and now holds 846,000 BTC",
               "Strategy repurchased $174.0M of $STRC (1.77M shares) that week",
               "Strategy issued $0 of common stock that week",
               "Cash on hand $6.09B ($5.04B USD Reserve + $1.05B USD cash), down $310M on the week",
               "$MSTR trades at about 1.19x basic NAV"]},
    {"id": "P3", "kind": "short post", "title": "$ASST is a two-variable stock",
     "angle": "Trend talk with scenario math, not a price target: the multiple is the swing factor.",
     "facts": ["$ASST NAV/share is about $14.19 (estimate)", "NAV/share is up 7.5% this quarter",
               "$ASST trades at 2.07x NAV", "BTC/share is up 12.1% this quarter and 9.4% over the last 3 weeks",
               "Scenario math (already computed, use as-is): multiple 1.5x with NAV/share +20% = about -13%; "
               "multiple 2.0x with NAV/share +20% = about +16%; multiple 1.0x with NAV/share flat = about -52%"]},
    {"id": "P4", "kind": "short post, humor", "title": "Digital credit's glow-up",
     "angle": "The skeptics' arc on digital credit vs where SATA trades now. Make it funny.",
     "facts": ["$SATA closed at $100.01", "Skeptics called digital credit a Ponzi with a coupon, then said it "
               "would never hold par"]},
    {"id": "P5", "kind": "reply to @PunterJeff (his post was about SATA liquidity and turnover)",
     "title": "Turnover reply", "angle": "Agree and add a number.",
     "facts": ["$SATA traded about 24.3% of its shares outstanding this week", "$SATA is a 13% perpetual preferred"]},
    {"id": "N1", "kind": "short post (Friday after-close wrap)", "title": "Week wrap at the Friday mark",
     "angle": "Weekly wrap in my rundown style.",
     "facts": ["BTC at the Friday 4pm mark: $84,006, up 3.47% on the week",
               "BTC is 28.4% above its 200-week SMA (the Cheap zone on my Closing Mark panel)",
               "Cycle checklist: 4 bull, 3 neutral, 0 bear", "Fear & Greed: 73 (Greed)",
               "$MSTR 1.19x NAV, $ASST 2.08x NAV"]},
    {"id": "N2", "kind": "short post (Tuesday evening, looking at Wednesday)", "title": "Big Wednesday",
     "angle": "Tomorrow stacks macro data and an STRC catalyst on the same morning.",
     "facts": ["Wed Sep 30, 8:30 ET: PCE inflation (August) and GDP Q2 third estimate",
               "Strategy posts STRC's October dividend rate on Sep 30 (last business day of the month)",
               "STRC's rate is 12% now", "STRC record date is Sep 30", "$STRC last close $98.54"]},
    {"id": "N3", "kind": "short post or 2-part thread (AI lane: what I build)", "title": "My agent waits for the 8-Ks",
     "angle": "Show off what I built with AI, then ask followers what they automate.",
     "facts": ["Every Monday my agent waits for both the Strategy and Strive 8-Ks",
               "It renders my Digital Credit Report panel with the same code as the site's download button",
               "It checks the image against the 8-Ks: BTC bought and held, cash, common and preferred raised",
               "Only then does it draft the post and ping me with the image",
               "It runs on GitHub Actions and writes with my ChatGPT account",
               "On day one it caught a bug that drew blank price/NAV tiles on my Friday panel while the old "
               "audit still passed"]},
    {"id": "N4", "kind": "short post (my positions)", "title": "Still adding at 2x",
     "angle": "Why I keep adding $ASST even at a 2x premium. Talk my book honestly; sizes are XX.",
     "facts": ["I added to $ASST this week (size: XX)", "BTC treasuries and digital credit are XX% of my portfolio",
               "$ASST trades at about 2.07x NAV", "$ASST BTC/share is up 12.1% this quarter",
               "I sold my car in February 2026 to buy BTC, MSTR and ASST"]},
    {"id": "N5", "kind": "long analytical post (Premium long post or a 4-6 part thread)",
     "title": "What gets STRC back to par",
     "angle": "Walk through why STRC sits under $100, what fixes it, and what I'm watching. Show the math, "
              "give my view, say what would change it.",
     "facts": ["$STRC last close $98.54; 0 of the last 20 closes at or above $100",
               "STRC's rate is 12%, a 12.18% effective yield at $98.54",
               "3-month T-bill yield 4.24%; STRC's spread over the bill is about 794 bp, SATA's about 876 bp",
               "Strategy repurchased $174.0M of STRC last week, funded from cash",
               "About $6.75B of Strategy debt principal sits senior to the preferreds",
               "Strategy holds $5.04B USD Reserve + $1.05B USD cash, about 45 months of dividends",
               "$SATA sits at $100.01 with zero debt above it",
               "Strategy announces STRC's October rate on Sep 30"]},
]


def draft() -> str:
    return db.kv_get(KV_DRAFT) or ""


def save_draft(text: str) -> None:
    db.kv_set(KV_DRAFT, text)


def briefs() -> list[dict]:
    return db.kv_get(KV_BRIEFS) or DEFAULT_BRIEFS


def history() -> list[dict]:
    return db.kv_get(KV_HISTORY) or []


def make_live() -> None:
    """Draft becomes the live voice; the old one is kept; v2 style mode and the robot check switch on."""
    text = draft()
    if not text.strip():
        raise ValueError("the draft is empty")
    db.kv_set(KV_HISTORY, history() + [{"text": config.get("voice"), "replaced_at": utcnow().isoformat()}])
    config.save("voice", text)
    st = copy.deepcopy(db.kv_get("config:settings") or {})
    st["voice"] = {**(st.get("voice") or {}), "style_mode": "v2", "robot_check": True}
    config.save("settings", st)


def _briefs_text(items: list[dict]) -> str:
    out = []
    for b in items:
        out.append(f"### {b['id']} · {b['kind']} · {b['title']}\nAngle: {b['angle']}\nFacts:\n"
                   + "\n".join(f"- {f}" for f in b["facts"]))
    return "\n\n".join(out)


def preview(which: str = "draft") -> dict:
    """Run the briefs through the real writer with the draft (or live) voice. Stored in kv, never in the Feed."""
    items = briefs()
    voice = draft() if which == "draft" else config.get("voice")
    mode = "v2" if which == "draft" else None
    prompt = llm.render_prompt("voice_preview", handle=context.handle(), voice=voice,
                               guidelines=config.get("guidelines"),
                               style=context.style_block("btc", seed="voice-preview", mode=mode),
                               briefs=_briefs_text(items))
    out = llm.run_json(prompt, "voice_preview", mock=lambda: {"posts": [
        {"id": b["id"], "parts": [f"[mock] {b['title']}"]} for b in items]})
    by_id = {p.get("id"): p for p in out.get("posts", [])}
    results = []
    for b in items:
        parts = [x for x in (by_id.get(b["id"], {}).get("parts") or []) if x.strip()]
        flags = editor.check_variant(parts, {}, b["facts"], "reply" if b["kind"].startswith("reply") else "",
                                     robot=True) if parts else ["missing"]
        results.append({"id": b["id"], "title": b["title"], "kind": b["kind"], "text": xtext.join_parts(parts),
                        "flags": flags})
    record = {"at": utcnow().isoformat(), "which": which, "results": results}
    db.kv_set(f"{KV_PREVIEW}:{which}", record)
    return {"which": which, "posts": len(results), "flags": sum(len(r["flags"]) for r in results)}
