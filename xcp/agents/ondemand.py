"""Process work queued from the panel (rewrites, draft-from-link, remixes...)."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db, llm, xtext
from xcp.agents import collect, context, editor, strategist
from xcp.sources import web, x_api
from xcp.timeutil import today_ny, utcnow

log = logging.getLogger(__name__)

REWRITE_PRESETS = {
    "shorter": "Make it shorter: under 200 characters, same core point.",
    "punchier": "Make it punchier: stronger verbs, no filler, a sharper first line.",
    "more_data": "Strengthen it with one or two relevant numbers from the snapshot.",
    "funnier": "Make it genuinely funny while keeping the point true. Nothing cringe.",
    "hook": "Rewrite the first line into a scroll-stopping hook. Keep the rest.",
    "thread": "Expand into a 4-6 part thread. Part 1 is the hook; each part stands on its own.",
    "single": "Condense into one single post (not a thread).",
}
LLM_KINDS = {"rewrite", "draft_from_url", "draft_from_text", "draft_from_story", "regenerate", "idea_from_story",
             "idea_remix", "ideas_generate", "style_sparks", "showcase_captions", "voice_preview"}


def pending(kind: str | None = None) -> list[db.Request]:
    with db.session() as s:
        q = select(db.Request).where(db.Request.status == "pending")
        if kind:
            q = q.where(db.Request.kind == kind)
        return list(s.scalars(q.order_by(db.Request.created_at)).all())


def _finish(req_id: int, status: str, result: dict | None = None, error: str | None = None) -> None:
    with db.session() as s:
        r = s.get(db.Request, req_id)
        r.status, r.result, r.error, r.finished_at = status, result or {}, error, utcnow()
        s.commit()


def _mark_running(ids: list[int]) -> None:
    with db.session() as s:
        for r in s.scalars(select(db.Request).where(db.Request.id.in_(ids))):
            r.status = "running"
        s.commit()


def process_all() -> dict:
    stats = {"done": 0, "errors": 0}
    rewrites = [r for r in pending("rewrite")]
    if rewrites:
        _run_rewrites(rewrites, stats)
    for r in pending():
        if r.kind not in LLM_KINDS or r.kind == "rewrite":
            continue
        _mark_running([r.id])
        try:
            result = HANDLERS[r.kind](r.payload or {})
            _finish(r.id, "done", result)
            stats["done"] += 1
        except Exception as e:  # keep the queue moving; the error shows in the panel
            log.exception("request %s failed", r.id)
            _finish(r.id, "error", error=str(e)[:1000])
            stats["errors"] += 1
    return stats


# ------------------------------------------------------------------ rewrites (batched)

def _run_rewrites(reqs: list[db.Request], stats: dict) -> None:
    _mark_running([r.id for r in reqs])
    snap_text, snap_time, flat = context.snapshot_block()
    blocks, meta = [], {}
    with db.session() as s:
        for r in reqs:
            p = r.payload or {}
            v = s.scalars(select(db.Variant).where(db.Variant.draft_id == p.get("draft_id"),
                                                   db.Variant.label == p.get("label"),
                                                   db.Variant.is_current.is_(True))).first()
            d = s.get(db.Draft, p.get("draft_id"))
            if not v or not d:
                _finish(r.id, "error", error="draft/option not found")
                continue
            instr = REWRITE_PRESETS.get(p.get("preset", ""), "") or p.get("instruction", "")
            meta[r.id] = (d, v, instr)
            blocks.append(context.compact({"request_id": r.id, "instruction": instr, "parts": v.parts,
                                           "context": [i.get("text", "")[:300] for i in d.inspiration or []]}))
    if not meta:
        return

    def mock():
        return {"results": [{"request_id": rid, "parts": [f"[mock rewrite: {m[2][:30]}] " + m[1].parts[0][:200]]}
                            for rid, m in meta.items()]}

    try:
        prompt = llm.render_prompt("rewrite", handle=context.handle(), voice=config.get("voice"), guidelines=config.get("guidelines"),
                                   snapshot=snap_text, snapshot_time=snap_time, requests="\n".join(blocks))
        out = llm.run_json(prompt, "rewrite", mock=mock)
    except Exception as e:
        for rid in meta:
            _finish(rid, "error", error=str(e)[:1000])
        stats["errors"] += len(meta)
        return
    results = {x["request_id"]: x["parts"] for x in out.get("results", [])}
    with db.session() as s:
        for rid, (d, v, instr) in meta.items():
            parts = results.get(rid)
            if not parts:
                _finish(rid, "error", error="no result returned")
                stats["errors"] += 1
                continue
            style = "thread" if len(parts) > 1 else (v.style if v.style != "thread" else "analyst")
            db.add_variant_version(s, d.id, v.label, parts, style, f"ai:{instr[:60]}")
            draft = s.get(db.Draft, d.id)
            flags = [f for f in (draft.editor_flags or []) if not f.startswith(f"{v.label}:")]
            flags += [f"{v.label}: {f}" for f in editor.check_variant(parts, flat, [i.get("text", "") for i in
                                                                                     d.inspiration or []], style)]
            draft.editor_flags = flags
            s.commit()
            _finish(rid, "done", {"variant_label": v.label})
            stats["done"] += 1


# ------------------------------------------------------------------ single drafts

def _single_draft(source_text: str, angle: str, n_variants: int = 3) -> tuple[dict, dict]:
    snap_text, snap_time, flat = context.snapshot_block()

    def mock():
        return {"title": "[mock] on-demand draft", "pillar": collect.classify(source_text)[1], "tone": "timely",
                "variants": [{"label": "ABC"[i], "style": ["analyst", "punchy", "quote"][i],
                              "parts": [f"[mock {['analyst', 'punchy', 'quote'][i]}] {source_text[:180]}"]}
                             for i in range(n_variants)],
                "numbers_used": [], "chart_hint": "none"}

    prompt = llm.render_prompt("single_draft", handle=context.handle(), voice=config.get("voice"), guidelines=config.get("guidelines"),
                               style=context.style_block(None, n=8), snapshot=snap_text,
                               snapshot_time=snap_time, source=source_text,
                               angle=angle or "(none)", n_variants=str(n_variants))
    return llm.run_json(prompt, "single_draft", mock=mock), flat


def _save_single(out: dict, flat: dict, slot: str, insp: list[dict], source_item_id: str | None = None,
                 story_id: int | None = None, draft_id: int | None = None) -> int:
    with db.session() as s:
        if draft_id:
            draft = s.get(db.Draft, draft_id)
        else:
            draft = db.Draft(slot=slot, slot_date=today_ny().isoformat(), kind="regular", inspiration=insp,
                             source_item_id=source_item_id, story_id=story_id)
            s.add(draft)
        draft.title, draft.pillar, draft.tone = out.get("title", ""), out["pillar"], out["tone"]
        draft.lane = config.pillar_lane(out["pillar"])
        draft.numbers, draft.chart_hint = out.get("numbers_used", []), out.get("chart_hint", "none")
        draft.status = "new"
        s.flush()
        flags = []
        for i, v in enumerate(out.get("variants", [])[:4]):
            label = (v.get("label") or "ABCD"[i])[:1].upper()
            db.add_variant_version(s, draft.id, label, v["parts"], v.get("style", "analyst"), "ai")
            flags += [f"{label}: {f}" for f in editor.check_variant(v["parts"], flat, [x.get("text", "") for x in insp],
                                                                   v.get("style", ""))]
        draft.editor_flags = flags
        s.commit()
        return draft.id


def h_draft_from_url(p: dict) -> dict:
    url = (p.get("url") or "").strip()
    tid = xtext.tweet_id_from_url(url)
    item_id, insp, text = None, [], ""
    if tid and x_api.configured():
        client = x_api.XClient()
        t = client.get_tweet(tid)
        if t:
            with db.session() as s:
                row, _ = collect.upsert_item(s, id=f"x:{t['id']}", kind="x_post", source="on_demand", text=t["text"],
                                             url=t["url"], author=t["author"], author_name=t["author_name"],
                                             author_followers=t["author_followers"], created_at=t["created_at"],
                                             metrics=t["metrics"])
                s.commit()
                item_id = row.id
            text = f"X post by @{t['author']}: {t['text']}\n{t['url']}"
            insp = [{"id": item_id, "label": "x_post", "url": t["url"], "author": t["author"], "text": t["text"][:400],
                     "metrics": t["metrics"]}]
    if not text:
        page = web.fetch_page(url)
        text = f"{page['title']}\n{page['url']}\n{page['text'][:6000]}"
        insp = [{"label": "link", "url": page["url"], "author": page["title"][:80], "text": page["text"][:400]}]
    out, flat = _single_draft(text, p.get("angle", ""))
    return {"draft_id": _save_single(out, flat, "on_demand", insp, source_item_id=item_id)}


def h_draft_from_text(p: dict) -> dict:
    text = p.get("text", "")
    out, flat = _single_draft(f"The owner's own idea: {text}", p.get("angle", ""))
    insp = [{"label": "idea", "url": "", "author": "your idea", "text": text[:400]}]
    return {"draft_id": _save_single(out, flat, "on_demand", insp)}


def h_draft_from_story(p: dict) -> dict:
    with db.session() as s:
        story = s.get(db.Story, p["story_id"])
        items = [s.get(db.Item, i) for i in story.item_ids]
        items = [i for i in items if i]
        story.status = "used"
        s.commit()
    source = f"Story: {story.title}\n{story.summary}\n\n" + context.items_block(items)
    insp = [{"id": i.id, "label": i.kind, "url": i.url, "author": i.author, "text": i.text[:400],
             "metrics": i.metrics} for i in items[:5]]
    out, flat = _single_draft(source, p.get("angle", ""))
    return {"draft_id": _save_single(out, flat, "on_demand", insp, story_id=story.id,
                                     source_item_id=items[0].id if items else None)}


def h_regenerate(p: dict) -> dict:
    with db.session() as s:
        d = s.get(db.Draft, p["draft_id"])
        insp = d.inspiration or []
        story = s.get(db.Story, d.story_id) if d.story_id else None
    source = (f"Story: {story.title}\n{story.summary}\n\n" if story else "") + "\n".join(
        f"{i.get('author', '')}: {i.get('text', '')} {i.get('url', '')}" for i in insp)
    out, flat = _single_draft(source or d.title, p.get("angle", "Fresh angles, different from before."))
    return {"draft_id": _save_single(out, flat, d.slot, insp, draft_id=d.id)}


def h_idea_from_story(p: dict) -> dict:
    with db.session() as s:
        story = s.get(db.Story, p["story_id"])
    out = strategist.generate(n=1, focus=f"Build something around this story: {story.title}. {story.summary}")
    return {"idea_ids": strategist.save_ideas(out.get("ideas", []), note=f"From story #{story.id}")}


def h_idea_remix(p: dict) -> dict:
    with db.session() as s:
        idea = s.get(db.BuildIdea, p["idea_id"])
    mode = p.get("mode", "remix")
    focus = {
        "remix": "Remix this idea into a fresh variation with a different visual metaphor.",
        "simpler": "Make a much simpler version that can be built in under an hour (effort S).",
        "wilder": "Make a bolder, more creative, more surprising version. Push the concept.",
        "series": "Turn this into a recurring series: 3 ideas that could run weekly or monthly.",
    }.get(mode, "Remix this idea.")
    n = 3 if mode == "series" else 1
    out = strategist.generate(n=n, focus=f"{focus}\nOriginal idea: {idea.title}. {idea.hook}\n{idea.concept[:800]}")
    return {"idea_ids": strategist.save_ideas(out.get("ideas", []), note=f"{mode} of #{idea.id}")}


def h_style_sparks(p: dict) -> dict:
    """Fresh drafts, each in a different format from the style library."""
    n = max(1, min(int(p.get("n", 5)), 10))
    snap_text, snap_time, flat = context.snapshot_block()
    since = (today_ny() - timedelta(days=2)).isoformat()
    with db.session() as s:
        stories = s.scalars(select(db.Story).where(db.Story.slot_date >= since)
                            .order_by(db.Story.score.desc()).limit(15)).all()
    stories_text = "\n".join(f"- [{x.pillar}] {x.title}: {x.summary[:220]}" for x in stories) or "(none yet)"

    def mock():
        fmts = ["short_observation", "quick_analysis", "humor_meme", "contrarian", "long_analysis"]
        return {"drafts": [{"title": f"[mock spark] {f}", "pillar": "digital_credit", "tone": "analytical",
                            "format": f, "text": f"[mock {f}] STRC keeps hugging par.", "numbers_used": []}
                           for f in fmts[:n]]}

    prompt = llm.render_prompt("sparks", handle=context.handle(), voice=config.get("voice"), guidelines=config.get("guidelines"),
                               style=context.style_block(None, n=16, seed=utcnow().isoformat()),
                               stories=stories_text, snapshot=snap_text, snapshot_time=snap_time,
                               focus=p.get("focus", "") or "(none)", n=str(n))
    out = llm.run_json(prompt, "sparks", mock=mock)
    ids = []
    with db.session() as s:
        for dr in out.get("drafts", [])[:n]:
            parts = xtext.split_parts(dr["text"])
            d = db.Draft(slot="on_demand", slot_date=today_ny().isoformat(), kind="regular", pillar=dr["pillar"],
                         lane=config.pillar_lane(dr["pillar"]), tone=dr["tone"],
                         title=f"✨ {dr.get('format', '')}: {dr.get('title', '')}"[:200],
                         numbers=dr.get("numbers_used", []),
                         editor_flags=[f"A: {f}" for f in editor.check_variant(parts, flat, [])])
            s.add(d)
            s.flush()
            db.add_variant_version(s, d.id, "A", parts, "thread" if len(parts) > 1 else "punchy", "ai:spark")
            ids.append(d.id)
        s.commit()
    return {"draft_ids": ids}


def h_ideas_generate(p: dict) -> dict:
    out = strategist.generate(n=int(p.get("n", 3)), focus=p.get("focus", ""))
    return {"idea_ids": strategist.save_ideas(out.get("ideas", []), note="Generated from the panel")}


PANEL_NOTES = {
    "monday": "It is the Monday Accretion Ledger: what Strategy and Strive bought with the week's capital (from "
              "their weekly 8-Ks) and what it did per share.",
    "wednesday": "It is the Wednesday Coupon Sheet: STRC and SATA yields and spreads over the 3-month bill, the "
                 "rest of the preferred ladder, and the coupon calendar.",
    "friday": "It is the Friday Closing Mark: BTC at the 4 pm mark, MSTR and ASST price/NAV, macro, and a "
              "rule-based cycle checklist.",
}


def h_showcase_captions(p: dict) -> dict:
    """Voicier caption options (B, C, D) for an audited Digital Credit Report image. A stays the fact-only one."""
    from xcp import notify, showcase
    from xcp.showcase_gate import values_of
    from xcp.sources import digital_exposure as de

    with db.session() as s:
        d = s.get(db.Draft, int(p["draft_id"]))
        run = s.get(db.ShowcaseRun, int(p["run_id"])) if p.get("run_id") else showcase.run_for_draft(s, d.id)
    values = values_of({"panels": {run.panel: run.audit}}, run.panel)
    url = de.report_url(run.panel)
    whats_new = (d.inspiration or [{}])[0].get("text", "") or "(see figures)"
    prompt = llm.render_prompt(
        "showcase", handle=context.handle(), voice=config.get("voice"), guidelines=config.get("guidelines"),
        style=context.style_block("btc", seed=f"showcase-{run.id}"), title=run.title,
        panel_note=PANEL_NOTES.get(run.panel, ""), values="\n".join(f"- {k}: {v}" for k, v in values.items()),
        whats_new=whats_new, url=url, n="3")
    out = llm.run_json(prompt, "showcase", mock=lambda: {"variants": [
        {"label": "B", "style": "brief", "parts": [f"[mock] {run.title} is out. {url}"]}]})
    known = [f"{k} {v}" for k, v in values.items()]
    flags, made = [], []
    with db.session() as s:
        for i, v in enumerate(out.get("variants", [])[:3]):
            label = "BCD"[i]
            parts = [x for x in v.get("parts", []) if x.strip()]
            if not parts:
                continue
            flags += [f"{label}: {f}" for f in editor.check_variant(parts, {}, known, v.get("style", ""))]
            db.add_variant_version(s, d.id, label, parts, v.get("style", "showcase"), "ai:showcase")
            made.append((label, xtext.join_parts(parts)))
        row = s.get(db.Draft, d.id)
        row.editor_flags = list(row.editor_flags or []) + flags
        s.commit()
    if made:
        fields = []
        for label, text in made:
            link = xtext.intent_post(text.split(xtext.THREAD_SEP)[0])
            fields.append((f"Option {label}", text[:900] + (f"\n[🚀 Open X]({link})" if len(link) < 1000 else "")))
        notify.discord(f"✍️ Captions for {run.title}", "Attach the image from the 🟢 message (or the Feed).", fields,
                       kind="drafts")
    return {"draft_id": d.id, "variants": len(made), "flags": len(flags)}


HANDLERS = {
    "style_sparks": h_style_sparks,
    "ideas_generate": h_ideas_generate,
    "draft_from_url": h_draft_from_url,
    "draft_from_text": h_draft_from_text,
    "draft_from_story": h_draft_from_story,
    "regenerate": h_regenerate,
    "idea_from_story": h_idea_from_story,
    "idea_remix": h_idea_remix,
    "showcase_captions": h_showcase_captions,
    "voice_preview": lambda p: h_voice_preview(p),
}


def h_voice_preview(p: dict) -> dict:
    from xcp import voice

    return voice.preview(p.get("which", "draft"))
