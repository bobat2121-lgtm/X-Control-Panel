"""Scout + Writer + Editor for one posting slot."""
from __future__ import annotations

import logging

from xcp import config, db, llm, notify, showcase, xtext
from xcp.agents import collect, context, editor
from xcp.sources import market
from xcp.timeutil import dayname, today_ny

log = logging.getLogger(__name__)

SLOT_BRIEFS = {
    "premarket": ("Pre-market (US equities open at 9:30 ET). Cover the BTC overnight move; pre-market MSTR, STRC, "
                  "SATA and ASST; anything Strategy or Strive filed (Monday 8-Ks with BTC purchases and preferred ATM "
                  "sales are big); today's macro calendar. Make one variant a compact 'brief' morning rundown."),
    "ai_noon": ("AI slot (posting is optional). Focus on frontier model releases, benchmark results, and big "
                "physical-AI updates (robotaxis, autonomous trucks, humanoids, industrial automation; significant "
                "ones only). Favor takes grounded in real capability over hype."),
    "midday": ("Midday, with the market open. React to how the session is playing out: BTC intraday, MSTR, STRC and "
               "SATA vs their $100 par, ETF and treasury-company news, and macro data released this morning."),
    "friday_close": ("Friday after the close. Weekly wrap: how BTC, MSTR, STRC and SATA finished, what mattered this "
                     "week, and what to watch next week."),
}


def _mock_slot(items: list[db.Item], flat: dict, n_stories: int, n_variants: int, idea) -> dict:
    by_pillar: dict[str, list[db.Item]] = {}
    for it in items:
        by_pillar.setdefault(it.pillar, []).append(it)
    stories, drafts = [], []
    btc = flat.get("btc.price")
    for i, (pillar, its) in enumerate(sorted(by_pillar.items(), key=lambda kv: -len(kv[1]))[:n_stories]):
        top = its[0]
        key = f"{pillar}-{i}"
        stories.append({"key": key, "title": f"[mock] {pillar} chatter ({len(its)} items)",
                        "summary": top.text[:200], "pillar": pillar, "item_ids": [x.id for x in its[:5]],
                        "scores": {"timeliness": 6, "insight": 5, "engagement": 5, "fit": 7, "humor": 3},
                        "angle_ideas": ["mock angle"]})
        lead = f"$BTC at ${btc:,.0f}. " if btc else ""
        variants = [{"label": "ABC"[j], "style": ["analyst", "punchy", "funny"][j % 3],
                     "parts": [f"[mock {['analyst', 'punchy', 'funny'][j % 3]}] {lead}{top.text[:160]}"]}
                    for j in range(n_variants)]
        drafts.append({"story_key": key, "title": f"[mock] {pillar} take", "pillar": pillar, "tone": "analytical",
                       "variants": variants,
                       "numbers_used": [{"label": "BTC", "value": f"{btc:,.0f}", "source": "btc.price"}] if btc else [],
                       "inspiration_item_ids": [x.id for x in its[:3]], "chart_hint": "btc_7d"})
    replies = [{"item_id": it.id, "reply": "[mock] Sharp point. The coverage math is the part people miss.",
                "why": "mock"} for it in items if it.kind == "x_post"][:2]
    sv = []
    if idea is not None:
        sv = [{"label": "A", "style": "showcase", "parts": [showcase.default_showcase_text(idea)]}]
    return {"stories": stories, "drafts": drafts, "replies": replies, "showcase_variants": sv}


def run_slot(slot: str) -> dict:
    settings = config.settings()
    spec = settings["slots"][slot]
    lane = spec.get("lane", "btc")
    d = today_ny()
    date_str = d.isoformat()
    stats: dict = {"slot": slot}

    snap = market.take_snapshot()
    snap_text, snap_time, flat = context.snapshot_block(snap)
    stats["collect"] = collect.collect(lane)
    stats["x_reads"] = stats["collect"].get("x_reads", 0)
    limits = settings.get("limits", {})
    items = collect.select_items(lane, limit=int(limits.get("items_to_llm", 120)))
    item_map = {it.id: it for it in items}

    # --- showcase?
    idea = None
    showcase_note = ""
    with db.session() as s:
        if showcase.is_showcase(slot, d):
            if showcase.existing_showcase_draft(s, date_str, slot):
                showcase_note = "showcase draft already exists"
            else:
                idea = showcase.pick_for_slot(s, d, slot)
                showcase_note = f"showcase: {idea.title}" if idea else "NO BUILD READY for this showcase slot"
    is_sc = idea is not None
    # A ready showcase is the main post, so only one backup story is needed.
    has_showcase = is_sc or showcase_note == "showcase draft already exists"
    n_stories = 1 if has_showcase else int(spec.get("stories", 3))
    n_variants = int(spec.get("variants", 3))

    if is_sc:
        sc_block = "## Showcase: this slot's main post is a Build Lab creation\n" + context.compact({
            "title": idea.title, "hook": idea.hook, "format": idea.format, "concept": idea.concept[:1200],
            "link": idea.shipped_url, "media": idea.media_url, "launch_post_draft": idea.launch_post,
            "pillar": idea.pillar})
        link = idea.shipped_url or "(link added by the owner)"
        sc_task = (f"Write 3 showcase variants (style 'showcase', or 'thread' for one of them) announcing this "
                   f"creation. Put the hook first, then what it shows and why it matters today; tie it to the "
                   f"snapshot where that's natural. End single posts with the link {link}. Make one variant a "
                   f"3-4 part thread.")
    else:
        sc_block, sc_task = "", "No showcase this slot: return an empty showcase_variants array."

    prompt = llm.render_prompt(
        "slot", handle=context.handle(), slot_label=spec.get("label", slot), post_at=spec.get("post_at", ""),
        weekday=dayname(d).title(), date=date_str, lane=lane, slot_brief=SLOT_BRIEFS.get(slot, ""),
        voice=config.get("voice"), guidelines=config.get("guidelines"), style=context.style_block(lane, seed=f"{date_str}-{slot}"),
        mix=context.mix_block(), snapshot=snap_text, snapshot_time=snap_time,
        calendar=context.calendar_block(), recent=context.recent_block(),
        items=context.items_block(items, int(limits.get("item_text_chars", 600))),
        showcase_block=sc_block, showcase_task=sc_task, max_stories=str(max(n_stories + 2, 5)),
        n_stories=str(n_stories), n_variants=str(n_variants))
    out = llm.run_json(prompt, "slot", mock=lambda: _mock_slot(items, flat, n_stories, n_variants, idea))

    # --- persist
    created = []
    with db.session() as s:
        story_ids: dict[str, int] = {}
        for st_ in out.get("stories", []):
            sc = st_.get("scores", {})
            score = (2 * sc.get("timeliness", 0) + 2 * sc.get("insight", 0) + sc.get("engagement", 0)
                     + 2 * sc.get("fit", 0) + sc.get("humor", 0)) / 8
            row = db.Story(slot=slot, slot_date=date_str, key=st_["key"], title=st_["title"], summary=st_["summary"],
                           pillar=st_["pillar"], lane=config.pillar_lane(st_["pillar"]), scores=sc,
                           score=round(score, 2), item_ids=st_.get("item_ids", []),
                           angle_ideas=st_.get("angle_ideas", []))
            s.add(row)
            s.flush()
            story_ids[st_["key"]] = row.id

        source_texts_all = [it.text for it in items]
        for dr in out.get("drafts", []):
            insp_items = [item_map[i] for i in dr.get("inspiration_item_ids", []) if i in item_map]
            story = next((x for x in out.get("stories", []) if x["key"] == dr["story_key"]), None)
            flags = []
            for i, v in enumerate(dr.get("variants", [])):
                for f in editor.check_variant(v["parts"], flat, [x.text for x in insp_items] or source_texts_all,
                                                  v.get("style", "")):
                    flags.append(f"{v.get('label') or 'ABC'[i]}: {f}")
            draft = db.Draft(slot=slot, slot_date=date_str, kind="regular", story_id=story_ids.get(dr["story_key"]),
                             lane=config.pillar_lane(dr["pillar"]), pillar=dr["pillar"], tone=dr["tone"],
                             title=dr.get("title", ""), numbers=dr.get("numbers_used", []), editor_flags=flags,
                             chart_hint=dr.get("chart_hint", "none"),
                             score=float(story and s.get(db.Story, story_ids.get(story["key"])).score or 0),
                             source_item_id=insp_items[0].id if insp_items else None,
                             inspiration=[_insp(x) for x in insp_items])
            s.add(draft)
            s.flush()
            for i, v in enumerate(dr.get("variants", [])[:4]):
                db.add_variant_version(s, draft.id, (v.get("label") or "ABCD"[i])[:1].upper(), v["parts"],
                                       v.get("style", "analyst"), "ai")
            created.append(draft)

        for rp in out.get("replies", []):
            it = item_map.get(rp["item_id"])
            if not it:
                continue
            draft = db.Draft(slot=slot, slot_date=date_str, kind="reply", lane=it.lane, pillar=it.pillar,
                             tone="timely", title=rp.get("why", "")[:200], source_item_id=it.id,
                             inspiration=[_insp(it)], score=it.score,
                             editor_flags=[f"A: {f}" for f in editor.check_variant([rp["reply"]], flat, [it.text], "reply")])
            s.add(draft)
            s.flush()
            db.add_variant_version(s, draft.id, "A", [rp["reply"]], "reply", "ai")

        sc_draft = None
        if is_sc and out.get("showcase_variants"):
            idea_row = s.get(db.BuildIdea, idea.id)
            sc_draft = showcase.create_showcase_draft(s, idea_row, slot, date_str, out["showcase_variants"])
        elif is_sc:
            sc_draft = showcase.create_showcase_draft(s, s.get(db.BuildIdea, idea.id), slot, date_str)
        s.commit()

    stats.update({"stories": len(out.get("stories", [])), "drafts": len(created),
                  "replies": len(out.get("replies", [])), "showcase": showcase_note})
    _alert(slot, spec, created, sc_draft, showcase_note, stats)
    return stats


def _insp(it: db.Item) -> dict:
    return {"id": it.id, "label": it.kind, "url": it.url, "author": it.author, "text": (it.text or "")[:400],
            "metrics": it.metrics or {}, "followers": it.author_followers}


def _alert(slot: str, spec: dict, drafts: list[db.Draft], sc_draft, showcase_note: str, stats: dict) -> None:
    title = f"{spec.get('label', slot)} drafts ready. Post at {spec.get('post_at', '')} ET"
    desc_lines = []
    top_text = ""
    with db.session() as s:
        lead = sc_draft or (sorted(drafts, key=lambda x: -x.score)[0] if drafts else None)
        if lead:
            vs = db.current_variants(s, lead.id)
            if vs:
                top_text = xtext.join_parts(vs[0].parts)
    if sc_draft:
        desc_lines.append("🛠 **Showcase slot.** Your Build Lab creation is the main post.")
    elif "NO BUILD" in showcase_note:
        desc_lines.append("⚠️ **Showcase slot, but no build is marked ready.** Regular drafts are below.")
    if top_text:
        desc_lines.append(f"**Top pick:**\n{top_text[:900]}")
        first = top_text.split(xtext.THREAD_SEP)[0]
        url = xtext.intent_post(first)
        if len(url) < 1800:
            desc_lines.append(f"[🚀 Post top pick on X]({url})")
    fields = [("Drafts", f"{len(drafts)} stories × options · {stats.get('replies', 0)} reply ideas"),
              ("Scan", f"{stats['collect'].get('x_posts_new', 0)} new X posts · "
                       f"{stats['collect'].get('filings_new', 0)} filings · {stats['collect'].get('news_new', 0)} news")]
    if stats["collect"].get("errors"):
        fields.append(("Warnings", "; ".join(stats["collect"]["errors"])[:1000]))
    notify.discord(title, "\n\n".join(desc_lines), fields)
