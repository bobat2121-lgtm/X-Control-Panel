"""⚙️ Control Room: agents, settings, watchlist, voice, market inputs, calendar."""
from __future__ import annotations

import copy

import pandas as pd
import streamlit as st
from sqlalchemy import select

from panel import cache
from panel.common import badge, enqueue, hero, is_owner, last_monitor_run, section
from xcp import config, db, gh, notify, showcase
from xcp import voice as voice_mod
from xcp.agents.collect import x_reads_this_month, x_reads_today
from xcp.config import env
from xcp.sources import calendar_feeds, issuers
from xcp.timeutil import fmt_ago, fmt_ny, parse_iso, today_ny

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CODEX_MODELS = {  # from OpenAI's Codex model list (Sept 2026)
    "": "Codex default for your plan (recommended)",
    "gpt-6-sol": "gpt-6-sol: strong all-rounder",
    "gpt-6-astra": "gpt-6-astra: complex research workflows",
    "gpt-6-luna": "gpt-6-luna: fastest, lightest on usage",
    "gpt-5.5": "gpt-5.5: legacy, retires Oct 14, 2026",
}
LANES = ["btc", "ai"]

if not is_owner():
    st.info("🔒 The Control Room is owner-only. Unlock with 🔑 Owner at the top right.", icon="🔒")
    st.stop()

settings = config.settings()
_lim = settings.get("limits", {})
_reads, _cap = cache.get(("cr", "reads"), x_reads_this_month, ttl=60), int(_lim.get("x_monthly_post_cap", 4000))
_mon = last_monitor_run()


def _pending_count() -> int:
    with db.session() as s:
        return len(db.pending_requests(s))


_queued = cache.get(("cr", "queued"), _pending_count, ttl=20)
_mode = settings.get("writer", {}).get("mode", "monitor")
hero("CONTROLROOM.EXE", "The <em>engine room</em>.",
     "Agents, schedule, sources, voice and budgets. Everything the monitor and desk run on.",
     stats=[(f"{_reads:,}", f"X reads this month · cap {_cap:,}", _reads > .8 * _cap),
            (fmt_ago(parse_iso(_mon.get("at"))) if _mon.get("at") else "—", "monitor last ran"),
            ("Monitor" if _mode == "monitor" else "Drafts", "writer mode"),
            (_queued, "AI requests queued", _queued > 0)],
     kicker=f"≈ ${_reads * 0.005:.2f} of ${_cap * 0.005:.0f} X budget used", icon="⚙️")
# Only the open tab runs (switching tabs reruns), so a click here doesn't rebuild all eight.
t_agents, t_show, t_settings, t_watch, t_voice, t_style, t_market, t_cal = st.tabs(
    ["🛰 Agents", "🛠 Showcase", "⚙️ Settings", "👀 Watchlist", "🗣 Voice & rules", "📚 Style library",
     "💹 Market inputs", "📅 Calendar"], key="cr_tab", on_change="rerun")

# ------------------------------------------------------------------ agents
with t_agents:
    if t_agents.open:
        auth = db.kv_get("codex_auth")
        checks = [
            ("Database", "Postgres (cloud)" if db.is_postgres() else "SQLite (local)", True),
            ("LLM", f"{env('LLM_BACKEND') or settings.get('llm', {}).get('backend', 'codex')} "
                    f"(Codex login stored {auth['updated_at'][:10]})" if auth else "Codex login not uploaded yet", bool(auth)),
            ("Discord alerts", "webhook set" if env("DISCORD_WEBHOOK_URL") else "not set in this app's secrets",
             bool(env("DISCORD_WEBHOOK_URL"))),
            ("Instant runs", f"dispatch → {env('GITHUB_REPO')}" if env("GH_DISPATCH_TOKEN") else
             ("local background runs" if env("LOCAL_AGENT") == "1" else "queued until next scheduled run"),
             bool(env("GH_DISPATCH_TOKEN") or env("LOCAL_AGENT") == "1")),
            ("X handle", settings.get("account", {}).get("handle") or "set in Settings", bool(settings.get("account", {}).get("handle"))),
        ]
        c = st.columns(len(checks))
        for col, (name, val, ok) in zip(c, checks):
            col.markdown(f"{badge('ON', 'olive') if ok else badge('SET UP', 'paper')} **{name}**  \n"
                         f"<span class='xcp-muted'>{val}</span>", unsafe_allow_html=True)

        lim = settings.get("limits", {})
        month_reads, month_cap = x_reads_this_month(), int(lim.get("x_monthly_post_cap", 4000))
        st.caption(f"X API reads · today: {x_reads_today()} / {lim.get('x_daily_post_cap', 200)} · this month: "
                   f"{month_reads} / {month_cap} (≈ \\${month_reads * 0.005:.2f} of \\${month_cap * 0.005:.0f} cap, "
                   f"\\$0.005/post; manual style refreshes excluded)")
        st.progress(min(month_reads / month_cap, 1.0) if month_cap else 0.0)

        section("Run now", "starts in the cloud")
        jobs = ["monitor"] + list(settings["slots"]) + ["nightly", "weekly", "snapshot", "style_refresh"]
        labels = {**{k: v.get("label", k) for k, v in settings["slots"].items()}, "monitor": "📡 Monitor",
                  "nightly": "🌙 Nightly (metrics + ideas)", "weekly": "📅 Weekly review", "snapshot": "💹 Market snapshot",
                  "style_refresh": "📚 Style refresh"}
        mode = settings.get("writer", {}).get("mode", "monitor")
        st.caption("Writer mode: " + ("🗞 **Monitor**: the slots write desk briefs and you write the posts."
                                      if mode == "monitor" else "✍️ **Drafts**: the slots write post drafts.")
                   + " Change it in Settings.")
        with st.container(horizontal=True, gap="small"):
            for j in jobs:
                st.button(labels[j], key=f"run_{j}", on_click=enqueue, args=("run_job", {"job": j}),
                          type="primary" if j == "monitor" else "secondary")

        section("Recent runs")
        with db.session() as s:
            runs = list(s.scalars(select(db.Run).where(db.Run.job != "monitor").order_by(db.Run.started_at.desc())
                                  .limit(40)).all())
            mon_runs = list(s.scalars(select(db.Run).where(db.Run.job == "monitor").order_by(db.Run.started_at.desc())
                                      .limit(96)).all())
            reqs = list(s.scalars(select(db.Request).order_by(db.Request.created_at.desc()).limit(40)).all())
        icon = {"ok": "✅", "error": "❌", "running": "⏳"}
        if mon_runs:
            errs = sum(1 for r in mon_runs if r.status == "error")
            st.caption(f"📡 Monitor: last run {fmt_ny(mon_runs[0].started_at)} ({mon_runs[0].status}) · {len(mon_runs)} runs "
                       f"in the last day or so, {errs} with errors · "
                       + ", ".join(f"{k} {v}" for k, v in (mon_runs[0].stats or {}).items() if k != "empty_feeds"))
        for r in runs[:15]:
            with st.expander(f"{icon.get(r.status, '•')} {r.job} · {fmt_ny(r.started_at)} · {r.trigger}"
                             + (f" · X reads {r.x_reads}" if r.x_reads else "")):
                st.json(r.stats or {}, expanded=False)
                st.code(r.log or "(no log)", language=None)
        section("Request queue")
        if reqs:
            st.dataframe([{"id": r.id, "kind": r.kind, "status": r.status, "created": fmt_ny(r.created_at),
                           "error": r.error or ""} for r in reqs], hide_index=True, width="stretch")
        if st.button("🔔 Send a test Discord alert"):
            ok = notify.discord("✅ Test from X Control Panel", "If you can read this, alerts work.", kind="test")
            st.toast("Sent" if ok else "Not sent: DISCORD_WEBHOOK_URL is missing in this app's secrets",
                     icon="🔔" if ok else "⚠️")

# ------------------------------------------------------------------ showcase
RUN_ICONS = {"waiting": "⏳", "blocked": "⚠️", "ready": "🟢", "posted": "✅", "missed": "🔴"}
CHECK_ICONS = {"PASS": "✅", "WARN": "⚠️", "WAIT": "⏳", "FAIL": "❌"}


def _sc_dispatch(mode: str, panel: str) -> None:
    ok, why = gh.dispatch("showcase.yml", {"mode": mode, "panel": panel})
    st.toast(f"Showcase {mode} started in the cloud ({panel or 'today'})." if ok else f"Couldn't start it: {why}",
             icon="🛰️" if ok else "⚠️")


def _use_anyway(run_id: int) -> None:
    from xcp.agents import showcase_watch

    with db.session() as s:
        r = s.get(db.ShowcaseRun, run_id)
        r.warnings = list(r.warnings or []) + [{"id": "override", "status": "WARN",
                                                "detail": "posted on your override: " + "; ".join(
                                                    b.get("detail", "") for b in (r.blockers or []))[:300]}]
        s.commit()
    showcase_watch.finalize(run_id, note="Owner override.", quiet=True)
    st.toast("Showcase draft created in the Feed from the last render", icon="🛠")


with t_show:
    if t_show.open:
        pre = db.kv_get("showcase:preflight") or {}
        last_good = db.kv_get("showcase:last_good_commit") or ""
        c = st.columns(3)
        c[0].markdown("**Last preflight**  \n" + (
            f"<span class='xcp-muted'>{fmt_ny(parse_iso(pre['at']))} · code {str(pre.get('commit', ''))[:7]} · "
            + " · ".join(f"{p} {'✅' if x.get('clean') else '⚠️'}" for p, x in (pre.get("panels") or {}).items())
            + (f" · ❌ {pre['error'][:120]}" if pre.get("error") else "") + "</span>"
            if pre.get("at") else "<span class='xcp-muted'>none yet (runs Sun/Tue/Thu ~8:13 PM ET)</span>"),
            unsafe_allow_html=True)
        c[1].markdown(f"**Last clean digital-exposure code**  \n<span class='xcp-muted'>"
                      f"{last_good[:7] or '—'} (fallback if main breaks)</span>", unsafe_allow_html=True)
        c[2].markdown(f"**Cloud buttons**  \n<span class='xcp-muted'>"
                      f"{'ready' if gh.can_dispatch() else 'set GH_DISPATCH_TOKEN in this app’s secrets'}</span>",
                      unsafe_allow_html=True)

        b = st.columns([1.3, 1, 1, 1])
        sc_opts = [""] + list((settings.get("digital_exposure", {}).get("panels") or {}))
        sc_pick = b[0].selectbox("Panel", sc_opts, format_func=lambda p: p or "today's panel", label_visibility="collapsed")
        b[1].button("🔄 Re-check now", on_click=_sc_dispatch, args=("once", sc_pick), width="stretch",
                    help="One render + audit now; drafts the post if it passes")
        b[2].button("👀 Watch today's window", on_click=_sc_dispatch, args=("watch", sc_pick), width="stretch",
                    help="Starts the watcher if a scheduled start was missed")
        b[3].button("🧪 Preflight all three", on_click=_sc_dispatch, args=("preflight", ""), width="stretch",
                    help="Renders all three panels and reports anything broken. Posts nothing")

        section("Lineup", "next two weeks")
        st.dataframe([{"date": f"{r['date']:%a %b %d}", "panel": r["title"], "slot": r["label"], "post": r["post"],
                       "window (ET)": r["window"],
                       "status": (RUN_ICONS.get(r["run"].status, "") + " " + r["run"].status) if r["run"] else "scheduled"}
                      for r in showcase.lineup(14)], hide_index=True, width="stretch")

        section("Runs")
        with db.session() as s:
            sc_runs = list(s.scalars(select(db.ShowcaseRun).order_by(db.ShowcaseRun.run_date.desc(),
                                                                     db.ShowcaseRun.id.desc()).limit(12)).all())
        if not sc_runs:
            st.caption("No showcase runs yet. The first one is the next Mon/Wed/Fri window.")
        for r in sc_runs:
            n = r.audit_summary or {}
            with st.expander(f"{RUN_ICONS.get(r.status, '•')} {r.run_date} · {r.title} · {r.status} · {r.renders} render(s)"
                             f" · digital-exposure {n.get('PASS', 0)}/{n.get('WARN', 0)}/{n.get('FAIL', 0)}"):
                cc = st.columns([3, 2])
                with cc[0]:
                    st.dataframe([{"": CHECK_ICONS.get(x.get("status"), ""), "check": x.get("id"), "detail": x.get("detail")}
                                  for x in r.checks or []], hide_index=True, width="stretch")
                    if r.filings:
                        st.caption("8-Ks: " + " · ".join(f"{f.get('ticker')} [{f.get('accession')}]({f.get('url')}) "
                                                         f"balance {f.get('balance_date')}" for f in r.filings.values()))
                    st.caption(f"code {r.de_commit[:7]}{' (last-good fallback)' if r.used_fallback else ''} · "
                               f"ready {fmt_ny(r.ready_at) if r.ready_at else '—'} · updated {fmt_ny(r.updated_at)}")
                    if r.status in ("waiting", "blocked", "missed") and r.png:
                        st.button("Use this image anyway", key=f"sc_use_{r.id}", on_click=_use_anyway, args=(r.id,),
                                  help="Creates the showcase draft from the last render, with the open issues noted")
                with cc[1]:
                    if r.png:
                        st.image(r.png, width="stretch")
                with st.popover("digital-exposure checks & log"):
                    st.dataframe([{"": CHECK_ICONS.get(x.get("status"), ""), "panel": x.get("panel"), "check": x.get("label"),
                                   "value": str(x.get("value")), "detail": x.get("detail")} for x in r.audit_checks or []],
                                 hide_index=True, width="stretch")
                    st.code(r.log or "(no log)", language=None)

# ------------------------------------------------------------------ settings
with t_settings:
    if t_settings.open:
        new = copy.deepcopy(settings)
        c = st.columns(2)
        new["account"]["handle"] = c[0].text_input("Your X handle (no @)", settings["account"].get("handle", ""))
        new["account"]["premium"] = c[1].toggle("X Premium (long posts)", settings["account"].get("premium", True))

        wm = settings.get("writer", {}).get("mode", "monitor")
        new.setdefault("writer", {})["mode"] = st.radio(
            "Writer mode", ["monitor", "drafts"], index=["monitor", "drafts"].index(wm) if wm in ("monitor", "drafts") else 0,
            horizontal=True, format_func=lambda m: {"monitor": "🗞 Monitor: brief me, I write",
                                                    "drafts": "✍️ Drafts: the agents also write posts"}[m],
            help="Monitor = the slots write desk briefs (what happened, why it matters, numbers, angle questions). "
                 "Drafts = the original mode, with AI-written post options. Switch back any time.")
        kinds_on = settings.get("alerts", {}).get("discord", notify.DEFAULT_KINDS)
        new.setdefault("alerts", {})["discord"] = st.multiselect(
            "Discord pings for", list(notify.KINDS), default=[k for k in kinds_on if k in notify.KINDS],
            format_func=notify.KINDS.get,
            help="Only these reach Discord. Everything else stays in the panel; the newest held alert of each kind is "
                 "listed below.")
        held = [(k, db.kv_get(f"notify:held:{k}")) for k in notify.KINDS if k not in new["alerts"]["discord"]]
        held = [(k, h) for k, h in held if h]
        if held:
            with st.expander(f"Held back from Discord ({len(held)} kinds)"):
                st.dataframe([{"kind": notify.KINDS[k], "latest": h.get("title", ""), "when": fmt_ny(parse_iso(h.get("at")))}
                              for k, h in sorted(held, key=lambda x: x[1].get("at", ""), reverse=True)],
                             hide_index=True, width="stretch")

        st.markdown("**Mix targets (% of posts)**")
        tg = settings["targets"]["pillars"]
        c = st.columns(len(tg))
        for col, (g, v) in zip(c, tg.items()):
            new["targets"]["pillars"][g] = col.number_input(g, 0, 100, int(v), step=5)
        total = sum(new["targets"]["pillars"].values())
        if total != 100:
            st.warning(f"Targets sum to {total}%, not 100%")
        tt = settings["targets"]["tones"]
        c = st.columns(len(tt))
        for col, (g, v) in zip(c, tt.items()):
            new["targets"]["tones"][g] = col.number_input(f"tone: {g}", 0, 100, int(v), step=5)

        st.markdown("**Slots** (ET). `run_at` is when the agent starts; `post_at` is when you post.")
        slot_rows = [{"slot": k, "label": v.get("label", k), "run_at": v["run_at"], "post_at": v["post_at"],
                      "days": ",".join(v.get("days", [])), "lane": v.get("lane", "btc"), "stories": v.get("stories", 3),
                      "variants": v.get("variants", 3), "optional": bool(v.get("optional", False))}
                     for k, v in settings["slots"].items()]
        edited = st.data_editor(pd.DataFrame(slot_rows), hide_index=True, width="stretch", key="slots_ed",
                                disabled=["slot"],
                                column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES)})
        for _, row in edited.iterrows():
            new["slots"][row["slot"]].update({
                "label": row["label"], "run_at": row["run_at"], "post_at": row["post_at"],
                "days": [d.strip() for d in str(row["days"]).split(",") if d.strip()], "lane": row["lane"],
                "stories": int(row["stories"]), "variants": int(row["variants"]), "optional": bool(row["optional"])})
        st.caption("GitHub's cron triggers are set in .github/workflows/agent.yml. If you move a run_at by more "
                   "than about an hour, update the cron there too.")

        st.markdown("**Showcase panels** (Digital Credit Report). `start`–`deadline` is the watch window (ET); "
                    "`nudge` flags it as still waiting (a Discord ping only if 'showcase waits' is switched on above).")
        de_cfg = settings.get("digital_exposure", {})
        sc_rows = [{"panel": k, "title": v.get("title", k), "day": v.get("day", ""), "slot": v.get("slot", ""),
                    "start": v.get("start", ""), "nudge": v.get("nudge", ""), "deadline": v.get("deadline", ""),
                    "post": v.get("post", "")} for k, v in (de_cfg.get("panels") or {}).items()]
        sc = st.data_editor(pd.DataFrame(sc_rows), hide_index=True, width="stretch", key="sc_ed", disabled=["panel"],
                            column_config={"day": st.column_config.SelectboxColumn("day", options=DAYS),
                                           "slot": st.column_config.SelectboxColumn("slot", options=list(settings["slots"]))})
        new.setdefault("digital_exposure", copy.deepcopy(de_cfg)).setdefault("panels", {})
        for _, r in sc.iterrows():
            new["digital_exposure"]["panels"][r["panel"]] = {k: str(r[k]) for k in
                                                             ("title", "day", "slot", "start", "nudge", "deadline", "post")}
        st.caption("The showcase workflow's start times are in .github/workflows/showcase.yml. Moving a window by more "
                   "than about an hour needs a cron change there too.")

        st.markdown("**Limits**")
        c = st.columns(4)
        new["limits"]["x_max_posts_per_run"] = c[0].number_input("X reads per run", 0, 5000, int(_lim.get("x_max_posts_per_run", 100)))
        new["limits"]["x_daily_post_cap"] = c[1].number_input("X reads per day", 0, 20000, int(_lim.get("x_daily_post_cap", 200)))
        new["limits"]["x_monthly_post_cap"] = c[2].number_input("X reads per month", 0, 200000,
                                                                int(_lim.get("x_monthly_post_cap", 4000)),
                                                                help="4,000 ≈ $20 at $0.005/post. Manual style refreshes excluded.")
        new["limits"]["items_to_llm"] = c[3].number_input("Items shown to the writer", 20, 400, int(_lim.get("items_to_llm", 120)))
        new["limits"]["include_watchlist_replies"] = st.toggle("Include watchlist accounts' replies",
                                                               bool(_lim.get("include_watchlist_replies", False)))
        c = st.columns(3)
        new["llm"]["backend"] = c[0].selectbox("LLM backend", ["codex", "mock"],
                                               index=["codex", "mock"].index(settings["llm"].get("backend", "codex")))
        cur_model = settings["llm"].get("codex_model", "") or ""
        model_opts = list(CODEX_MODELS) + ([cur_model] if cur_model and cur_model not in CODEX_MODELS else [])
        new["llm"]["codex_model"] = c[1].selectbox(
            "Codex model (writes every draft)", model_opts, index=model_opts.index(cur_model),
            format_func=lambda m: CODEX_MODELS.get(m, f"{m} (custom)"),
            help="Uses your ChatGPT plan's Codex limits. Heavier models use more of your plan's allowance.")
        efforts = {"": "Model default", "low": "low: fastest", "medium": "medium", "high": "high: more careful writing",
                   "xhigh": "xhigh: slowest, heaviest on limits"}
        cur_eff = settings["llm"].get("reasoning_effort", "") or ""
        eff_opts = list(efforts) + ([cur_eff] if cur_eff not in efforts else [])
        new["llm"]["reasoning_effort"] = c[2].selectbox("Reasoning effort", eff_opts, index=eff_opts.index(cur_eff),
                                                        format_func=lambda e: efforts.get(e, e))

        b = st.columns([1, 1, 4])
        if b[0].button("💾 Save settings", type="primary"):
            config.save("settings", new)
            st.toast("Settings saved", icon="💾")
            st.rerun()
        if b[1].button("Reset to defaults"):
            config.reset("settings")
            st.rerun()

# ------------------------------------------------------------------ watchlist
with t_watch:
    if t_watch.open:
        wl = config.get("watchlist")
        accounts = wl.get("accounts", [])
        st.caption(f"{len(accounts)} accounts. The Scout reads their recent posts at each slot "
                   f"(bundled into as few X searches as possible).")
        pillar_opts = [""] + list(config.pillars())
        df = pd.DataFrame(accounts or [{"handle": "", "lane": "btc", "pillar": "", "note": ""}],
                          columns=["handle", "lane", "pillar", "note"])
        ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="wl_ed",
                            column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES, required=True),
                                           "pillar": st.column_config.SelectboxColumn("pillar", options=pillar_opts)})
        with st.expander("Bulk add (paste handles)"):
            bulk = st.text_area("One per line, e.g. `@handle btc digital_credit`", height=120, key="wl_bulk")
        if st.button("💾 Save watchlist", type="primary"):
            rows = [{k: (v if isinstance(v, str) else "") for k, v in r.items()} for r in ed.to_dict("records")]
            for line in (bulk or "").splitlines():
                bits = line.replace(",", " ").split()
                if bits:
                    rows.append({"handle": bits[0], "lane": bits[1] if len(bits) > 1 and bits[1] in LANES else "btc",
                                 "pillar": bits[2] if len(bits) > 2 else "", "note": ""})
            seen, clean = set(), []
            for r in rows:
                h = (r.get("handle") or "").strip().lstrip("@")
                if h and h.lower() not in seen:
                    seen.add(h.lower())
                    clean.append({"handle": h, "lane": r.get("lane") or "btc", "pillar": r.get("pillar") or "",
                                  "note": r.get("note") or ""})
            config.save("watchlist", {"accounts": clean})
            st.toast(f"Saved {len(clean)} accounts", icon="👀")
            st.rerun()

        st.markdown("**Keyword searches on X**")
        pcfg = config.get("pillars")
        xs = st.data_editor(pd.DataFrame(pcfg.get("x_searches", [])), num_rows="dynamic", hide_index=True,
                            width="stretch", key="xs_ed",
                            column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES),
                                           "query": st.column_config.TextColumn("query", width="large")})
        if st.button("💾 Save searches"):
            newp = copy.deepcopy(pcfg)
            newp["x_searches"] = [{"name": r["name"], "lane": r["lane"], "query": r["query"], "max": int(r["max"] or 30)}
                                  for _, r in xs.iterrows() if r.get("query")]
            config.save("pillars", newp)
            st.toast("Searches saved", icon="🔎")

# ------------------------------------------------------------------ voice
with t_voice:
    if t_voice.open:
        st.markdown("**Posting guidelines** · every writer and rewrite follows these (guiding, not binding)")
        guidelines = st.text_area("Posting guidelines", config.get("guidelines"), height=230,
                                  label_visibility="collapsed", key="guidelines_ta")
        if st.button("💾 Save guidelines", type="primary"):
            config.save("guidelines", guidelines)
            st.toast("Guidelines saved", icon="📏")
        st.divider()
        st.markdown("**Voice profile** · the single biggest lever on draft quality. Paste your best posts, phrases you "
                    "never use, and what you believe.")
        voice = st.text_area("Voice profile", config.get("voice"), height=480, label_visibility="collapsed")
        if st.button("💾 Save voice", type="primary"):
            config.save("voice", voice)
            st.toast("Voice saved", icon="🗣")

        st.divider()
        st.markdown("**🧪 Voice draft (not live)** · edit a new voice, preview it on the same example briefs every "
                    "time, then make it live. Previews never reach the Feed.")
        d_text = st.text_area("Voice draft", voice_mod.draft() or config.get("voice"), height=480,
                              label_visibility="collapsed", key="voice_draft_ta")

        def _preview_draft() -> None:
            voice_mod.save_draft(st.session_state.get("voice_draft_ta", ""))
            enqueue("voice_preview", {"which": "draft"})

        c = st.columns([1, 1.3, 1.6])
        if c[0].button("💾 Save draft", width="stretch"):
            voice_mod.save_draft(d_text)
            st.toast("Draft saved (not live)", icon="🧪")
        c[1].button("🧪 Preview the draft", on_click=_preview_draft, width="stretch",
                    help=f"Writes the {len(voice_mod.briefs())} example posts with the draft on your ChatGPT (about 2-4 "
                         "minutes). Nothing is posted or added to the Feed.")
        with c[2].popover("🚀 Make the draft live", width="stretch"):
            st.caption("Replaces the live voice (kept in history below) and turns on the v2 style mode and the "
                       "robot check for every draft.")
            if st.button("Yes, make it live", type="primary", key="voice_live"):
                voice_mod.save_draft(d_text)
                voice_mod.make_live()
                st.toast("The draft is now the live voice", icon="🚀")
                st.rerun()
        prev = db.kv_get("voice:preview:draft")
        if prev:
            st.caption(f"Latest preview of the draft · {fmt_ny(parse_iso(prev['at']))}")
            for r in prev["results"]:
                with st.container(border=True):
                    st.markdown(f"**{r['id']} · {r['title']}** · <span class='xcp-muted'>{r['kind']}</span>",
                                unsafe_allow_html=True)
                    st.code(r["text"] or "(nothing written)", language=None, wrap_lines=True)
                    for f in r["flags"]:
                        st.caption(f"⚠️ {f}")
        hist = voice_mod.history()
        if hist:
            with st.expander(f"Earlier live versions ({len(hist)})"):
                for h in reversed(hist):
                    st.caption(f"Replaced {fmt_ny(parse_iso(h['replaced_at']))}")
                    st.code(h["text"][:6000], language=None, wrap_lines=True)

# ------------------------------------------------------------------ market inputs
with t_market:
    if t_market.open:
        m = settings.get("market", {})
        auto_on = st.toggle("Fill holdings and dividend rates automatically", bool(m.get("auto_inputs", True)),
                            help="BTC holdings from the latest weekly 8-K, STRC's rate from strategy.com, SATA's from "
                                 "strive.com. Checked on every market snapshot.")
        auto = issuers.last()
        labels = {"mstr_btc_holdings": "Strategy BTC holdings", "asst_btc_holdings": "Strive BTC holdings",
                  "strc_annual_rate_pct": "STRC rate %", "sata_annual_rate_pct": "SATA rate %"}
        if auto.get("values"):
            st.dataframe([{"input": labels.get(k, k), "value": f"{v['value']:,.4g}" if v["value"] < 100 else f"{v['value']:,.0f}",
                           "as of": v.get("as_of"), "source": v.get("source"), "": "" if v.get("fresh") else "last good value"}
                          for k, v in auto["values"].items()], hide_index=True, width="stretch")
            st.caption(f"Last checked {fmt_ny(parse_iso(auto.get('checked_at')))}"
                       + (f" · ⚠️ {'; '.join(auto['errors'])[:300]}" if auto.get("errors") else ""))
        else:
            st.caption("No automatic check yet: it runs with the next market snapshot (every agent run, or Refresh).")
        st.markdown("**Fallback values** (used if a source fails, or everything when the toggle is off)")
        c = st.columns(2)
        mstr = c[0].number_input("Strategy BTC holdings", 0, 5_000_000, int(m.get("mstr_btc_holdings") or 0), step=100)
        asst = c[1].number_input("Strive BTC holdings", 0, 1_000_000, int(m.get("asst_btc_holdings") or 0), step=10)
        c = st.columns(3)
        strc = c[0].number_input("STRC annual dividend rate %", 0.0, 30.0, float(m.get("strc_annual_rate_pct") or 0.0), step=0.25)
        sata = c[1].number_input("SATA annual dividend rate %", 0.0, 30.0, float(m.get("sata_annual_rate_pct") or 0.0), step=0.25)
        asof = c[2].text_input("Rates as of", m.get("rates_as_of", ""))
        tickers = st.text_input("Tickers on the Market Desk", ", ".join(m.get("tickers", [])))
        if st.button("💾 Save market inputs", type="primary"):
            new = copy.deepcopy(settings)
            new["market"].update({"auto_inputs": auto_on, "mstr_btc_holdings": mstr or None, "asst_btc_holdings": asst or None,
                                  "strc_annual_rate_pct": strc or None, "sata_annual_rate_pct": sata or None,
                                  "rates_as_of": asof, "tickers": [t.strip() for t in tickers.split(",") if t.strip()]})
            config.save("settings", new)
            st.toast("Saved. Takes effect on the next snapshot.", icon="💹")

# ------------------------------------------------------------------ calendar
def _cal_sync() -> None:
    out = calendar_feeds.sync()
    st.toast(f"Calendar: {out.get('added', 0)} added, {out.get('updated', 0)} updated"
             + (f" · {len(out['errors'])} source error(s)" if out.get("errors") else ""), icon="📅")


def _bls_toggle() -> None:
    new = copy.deepcopy(settings)
    new.setdefault("calendar", {})["bls_contact"] = bool(st.session_state.get("bls_contact"))
    config.save("settings", new)


with t_cal:
    if t_cal.open:
        cal_cfg = settings.get("calendar", {})
        st.caption("Fills itself nightly: FOMC (Fed), GDP and PCE (BEA), STRC record/pay dates (strategy.com), MSTR/ASST "
                   "earnings (Nasdaq, 'est.' until confirmed), and your report's curated STRC/SATA events. Rows marked "
                   "auto: are managed for you; add your own below (launches, conferences) and they're never touched.")
        c = st.columns([1.2, 3])
        c[0].button("🔄 Refresh now", on_click=_cal_sync, width="stretch")
        c[1].toggle("Include CPI and jobs dates (BLS)", bool(cal_cfg.get("bls_contact")), key="bls_contact",
                    on_change=_bls_toggle,
                    help="BLS only answers requests that carry a contact email. On = the calendar sends your "
                         "SEC_USER_AGENT contact to bls.gov when it checks the schedule.")
        last = db.kv_get("calendar:last_sync") or {}
        if last.get("at"):
            st.caption(f"Last refresh {fmt_ny(parse_iso(last['at']))}: " + " · ".join(
                f"{k} {v}" for k, v in (last.get("by_source") or {}).items())
                + (f" · ⚠️ {'; '.join(last['errors'])[:300]}" if last.get("errors") else "")
                + (f" · {last['note']}" if last.get("note") else ""))
        with db.session() as s:
            evs = list(s.scalars(select(db.CalendarEvent).where(db.CalendarEvent.date >= today_ny().isoformat())
                                 .order_by(db.CalendarEvent.date)).all())
        df = pd.DataFrame([{"id": e.id, "date": e.date, "time": e.time, "title": e.title, "pillar": e.pillar,
                            "importance": e.importance, "notes": e.notes} for e in evs],
                          columns=["id", "date", "time", "title", "pillar", "importance", "notes"])
        ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="cal_ed",
                            disabled=["id"],
                            column_config={"pillar": st.column_config.SelectboxColumn("pillar", options=list(config.pillars())),
                                           "importance": st.column_config.NumberColumn("importance", min_value=1, max_value=3)})
        if st.button("💾 Save calendar", type="primary"):
            keep_ids = set()
            with db.session() as s:
                for r in ed.to_dict("records"):
                    if not r.get("date") or not r.get("title"):
                        continue
                    rid = r.get("id")
                    row = s.get(db.CalendarEvent, int(rid)) if rid and not pd.isna(rid) else None
                    if row is None:
                        row = db.CalendarEvent()
                        s.add(row)
                    row.date, row.time, row.title = str(r["date"])[:10], str(r.get("time") or "")[:5], r["title"]
                    row.pillar = r.get("pillar") or "macro"
                    row.importance = int(r.get("importance") or 2)
                    row.notes = r.get("notes") or ""
                    s.flush()
                    keep_ids.add(row.id)
                for e in evs:
                    if e.id not in keep_ids:
                        s.delete(s.get(db.CalendarEvent, e.id))
                s.commit()
            st.toast("Calendar saved", icon="📅")
            st.rerun()

# ------------------------------------------------------------------ style library
FORMATS = ["short_observation", "quick_analysis", "long_analysis", "thread", "humor_meme", "contrarian",
           "data_callout", "news_reaction", "question_hook", "chart_callout", "reply"]
with t_style:
    if t_style.open:
        sc = settings.get("style", {})
        st.info(f"📚 **Style refresh** (Agents tab → Run now) pulls the latest {sc.get('posts_per_account', 100)} posts from "
                f"each watchlist account plus up to {sc.get('own_posts_max', 1000)} of yours via the X API "
                "(≈ \\$0.005/post), then extracts new patterns. Mark favorites by setting strength 8+; "
                f"favorites are always shown to the writer, and the rest rotate.", icon="📚")
        with db.session() as s:
            rows = list(s.scalars(select(db.StyleExample).order_by(db.StyleExample.source.desc(),
                                                                    db.StyleExample.strength.desc())).all())
        mine_n = sum(1 for r in rows if r.source == "mine")
        st.caption(f"{mine_n} of your own posts · {len(rows) - mine_n} craft patterns from accounts you admire · "
                   f"{sum(1 for r in rows if r.active)} active. The writer sees your posts first, then a varied, "
                   "mostly-short sample of patterns on every run. Other accounts' wording is never stored.")

        with st.expander("➕ Add your own best posts (highest priority)"):
            with st.form("add_mine", clear_on_submit=True):
                txt = st.text_area("Paste posts. Separate several with a line containing only ---", height=180)
                c = st.columns(3)
                fmt = c[0].selectbox("Format", FORMATS)
                pil = c[1].selectbox("Pillar", list(config.pillars()))
                url = c[2].text_input("Link (optional)")
                if st.form_submit_button("Add") and txt.strip():
                    from xcp import xtext
                    with db.session() as s:
                        for post in xtext.split_parts(txt):
                            s.add(db.StyleExample(source="mine", text=post, format=fmt, pillar=pil, url=url,
                                                  length="long" if len(post) > 600 else "short", strength=9))
                        s.commit()
                    st.rerun()

        c = st.columns(3)
        f_src = c[0].multiselect("Source", ["mine", "admired", "repost"], default=["mine", "admired"],
                                help="repost = what you or they amplified; never used as voice")
        f_fmt = c[1].multiselect("Format", FORMATS, placeholder="All formats")
        f_handle = c[2].multiselect("Account", sorted({r.handle for r in rows if r.handle}), placeholder="All accounts")
        shown = [r for r in rows if r.source in f_src and (not f_fmt or r.format in f_fmt)
                 and (not f_handle or r.handle in f_handle)]
        df = pd.DataFrame([{"id": r.id, "active": r.active, "strength": r.strength, "source": r.source,
                            "handle": r.handle, "format": r.format, "length": r.length, "pillar": r.pillar,
                            "hook": r.hook_type, "pattern": r.pattern, "template": r.skeleton,
                            "your-lane example": r.demo if r.source != "mine" else r.text, "link": r.url}
                           for r in shown],
                          columns=["id", "active", "strength", "source", "handle", "format", "length", "pillar", "hook",
                                   "pattern", "template", "your-lane example", "link"])
        ed = st.data_editor(df, hide_index=True, width="stretch", key="style_ed", disabled=["id", "source", "handle"],
                            column_config={
                                "strength": st.column_config.NumberColumn("strength", min_value=1, max_value=10),
                                "format": st.column_config.SelectboxColumn("format", options=FORMATS),
                                "length": st.column_config.SelectboxColumn("length", options=["short", "medium", "long"]),
                                "pillar": st.column_config.SelectboxColumn("pillar", options=list(config.pillars())),
                                "pattern": st.column_config.TextColumn("pattern", width="large"),
                                "template": st.column_config.TextColumn("template", width="large"),
                                "your-lane example": st.column_config.TextColumn("your-lane example", width="large"),
                                "link": st.column_config.LinkColumn("link", display_text="source ↗")})
        if st.button("💾 Save library changes", type="primary"):
            with db.session() as s:
                for r in ed.to_dict("records"):
                    row = s.get(db.StyleExample, int(r["id"]))
                    if row is None:
                        continue
                    row.active, row.strength = bool(r["active"]), int(r["strength"] or 7)
                    row.format, row.length, row.pillar = r["format"], r["length"], r["pillar"]
                    row.hook_type, row.pattern, row.skeleton = r["hook"] or "", r["pattern"] or "", r["template"] or ""
                    if row.source == "mine":
                        row.text = r["your-lane example"] or row.text
                    else:
                        row.demo = r["your-lane example"] or ""
                s.commit()
            st.toast("Style library saved", icon="📚")
            st.rerun()
