"""Showcase watcher: wait for fresh data, render the Digital Credit Report panel, audit it, then draft the post.

Runs in .github/workflows/showcase.yml (its own queue, so a long Monday wait never blocks the agent):
  watch     loop from the panel's start time: cheap readiness check every poll_seconds, full render + audit
            at most every render_every_seconds, stop when ready or at the deadline
  once      one render + audit now (the panel's "Re-check now" button); refreshes the image if already ready
  preflight render all three panels the evening before and report anything broken (no posting)
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from xcp import config, db, gh, notify, showcase, xtext
from xcp import showcase_gate as gate
from xcp.sources import digital_exposure as de
from xcp.timeutil import at_ny, fmt_ny, now_ny, nyse_open, today_ny, utcnow, weekly_release_date

log = logging.getLogger(__name__)
LAST_GOOD = "showcase:last_good_commit"
GREEN, AMBER, RED = 0x2ECC71, 0xF1C40F, 0xE74C3C


# ------------------------------------------------------------------ helpers

def _row(run_id: int):
    with db.session() as s:
        return s.get(db.ShowcaseRun, run_id)


def _update(run_id: int, **fields) -> None:
    with db.session() as s:
        r = s.get(db.ShowcaseRun, run_id)
        for k, v in fields.items():
            setattr(r, k, v)
        r.updated_at = utcnow()
        s.commit()


def _log(run_id: int, line: str) -> None:
    log.info(line)
    with db.session() as s:
        r = s.get(db.ShowcaseRun, run_id)
        r.log = ((r.log or "") + f"{fmt_ny(utcnow(), '%H:%M:%S')} {line}\n")[-20000:]
        r.updated_at = utcnow()
        s.commit()


def _get_or_create(date_str: str, panel: str) -> int:
    spec = showcase.panels().get(panel, {})
    with db.session() as s:
        r = showcase.run_for(s, date_str, panel)
        if r is None:
            r = db.ShowcaseRun(run_date=date_str, panel=panel, slot=spec.get("slot", ""),
                               title=spec.get("title", panel), status="waiting")
            s.add(r)
            s.commit()
        return r.id


def _previous_fingerprint(panel: str, date_str: str) -> str:
    with db.session() as s:
        r = s.scalars(select(db.ShowcaseRun).where(db.ShowcaseRun.panel == panel, db.ShowcaseRun.run_date < date_str,
                                                   db.ShowcaseRun.status.in_(["ready", "posted"]))
                      .order_by(db.ShowcaseRun.run_date.desc())).first()
    return gate.fingerprint(gate.values_of({"panels": {panel: r.audit}}, panel)) if r and r.audit else ""


def _mark(run_id: int, key: str) -> bool:
    """True the first time an alert key is used for this run (so each ping goes out once)."""
    with db.session() as s:
        r = s.get(db.ShowcaseRun, run_id)
        alerts = dict(r.alerts or {})
        if alerts.get(key):
            return False
        alerts[key] = utcnow().isoformat()
        r.alerts = alerts
        s.commit()
    return True


def _lines(checks: list, limit: int = 6) -> str:
    return "\n".join(f"• {c.detail if hasattr(c, 'detail') else c.get('detail', '')}" for c in checks[:limit]) or "—"


# ------------------------------------------------------------------ one render + audit

def _render(panel: str, de_dir: Path, de_python: str, work: Path) -> de.Render:
    """Render at the repo's current commit; if that code is broken, fall back to the last commit that rendered cleanly."""
    r = de.render(de_dir, de_python, work / "render", monday_check=(panel == "monday"))
    problem = r.error or ("" if r.ok and panel in (r.audit.get("panels") or {}) else "outputs missing")
    last_good = db.kv_get(LAST_GOOD)
    if problem and last_good and last_good != r.commit:
        wt = de.checkout_commit(de_dir, last_good, work / "lastgood")
        if wt:
            r2 = de.render(wt, de_python, work / "render-lastgood", monday_check=(panel == "monday"))
            if r2.ok:
                r2.used_fallback, r2.fallback_reason = True, f"main {r.commit[:7]} failed: {problem}"[:400]
                return r2
    return r


def evaluate(panel: str, de_dir: Path, de_python: str, work: Path, run_id: int, feed: dict | None,
             filings: dict) -> tuple[gate.Verdict, de.Render]:
    r = _render(panel, de_dir, de_python, work)
    row = _row(run_id)
    settle = int(de.cfg().get("friday_settle_minutes", 10))
    if not r.ok:
        v = gate.Verdict(panel)
        v.add("render", "FAIL", r.error[:500])
    else:
        v = gate.evaluate(panel, audit=r.audit, checks=r.checks, png=r.pngs.get(panel), now=now_ny(),
                          filings=filings, feed=feed, publication=r.publication, saved_quotes=r.saved_quotes,
                          previous_fingerprint=_previous_fingerprint(panel, row.run_date), settle_minutes=settle)
        if r.used_fallback:
            v.add("code", "WARN", f"rendered with last-good code {r.commit[:7]} ({r.fallback_reason})")
        elif not v.failed:
            db.kv_set(LAST_GOOD, r.commit)
    png = r.pngs.get(panel)
    if row.status in ("ready", "posted") and not v.ready:  # a re-check never replaces a good image with a worse one
        _log(run_id, f"re-check {r.commit[:7]} → {v.summary()}; keeping the ready image")
        return v, r
    panel_checks = [c for c in (r.checks.get("checks") or []) if c.get("panel") in (panel, "sources")]
    fields = dict(renders=row.renders + 1, de_commit=r.commit, used_fallback=r.used_fallback, checks=v.as_dicts(),
                  blockers=[c.__dict__ for c in v.blockers], warnings=[c.__dict__ for c in v.warnings],
                  audit_summary=r.checks.get("summary") or {}, audit_checks=panel_checks,
                  audit={**((r.audit.get("panels") or {}).get(panel) or {}), "monday_notice": r.audit.get("monday_notice"),
                         "stale_sections": r.audit.get("stale_sections"), "rendered_at": r.audit.get("rendered_at")},
                  filings={t: de.filing_summary(f) for t, f in filings.items()})
    if png:
        fields.update(png=png, png_sha256=hashlib.sha256(png).hexdigest())
    if not v.ready:
        fields["status"] = "blocked" if v.failed else "waiting"
    _update(run_id, **fields)
    _log(run_id, f"render {r.commit[:7]} in {r.seconds}s → {v.summary()}"
         + (f" · blockers: {'; '.join(c.detail for c in v.blockers)[:500]}" if v.blockers else ""))
    return v, r


# ------------------------------------------------------------------ outcomes

def finalize(run_id: int, note: str = "", quiet: bool = False) -> int | None:
    """Mark ready, put the image + caption in the Feed, ping Discord, and ask the agent for captions."""
    with db.session() as s:
        r = s.get(db.ShowcaseRun, run_id)
        first = r.draft_id is None
        r.status, r.ready_at, r.updated_at = "ready", r.ready_at or utcnow(), utcnow()
        d = showcase.create_draft(s, r)
        s.commit()
        draft_id, run = d.id, r
    if not first:
        _log(run_id, "image refreshed on the existing draft")
        return draft_id
    if config.settings().get("writer", {}).get("mode", "monitor") != "drafts":  # you write the captions
        if not quiet:
            _alert_ready(run, draft_id, note)
        return draft_id
    with db.session() as s:
        s.add(db.Request(kind="showcase_captions", payload={"draft_id": draft_id, "run_id": run_id}))
        s.commit()
    if gh.can_dispatch():
        ok, why = gh.dispatch("agent.yml")
        _log(run_id, f"captions requested; agent {'started' if ok else 'not started: ' + why}")
    if not quiet:
        _alert_ready(run, draft_id, note)
    return draft_id


def _alert_ready(run: db.ShowcaseRun, draft_id: int, note: str) -> None:
    spec = showcase.panels().get(run.panel, {})
    n = run.audit_summary or {}
    with db.session() as s:
        vs = db.current_variants(s, draft_id)
    caption = xtext.join_parts(vs[0].parts) if vs else ""
    what = {"monday": gate.release_filings_label({t: {"acceptedAt": f.get("accepted_at"),
                                                      "extracted": {"balanceDate": f.get("balance_date")}}
                                                  for t, f in (run.filings or {}).items()}),
            "friday": "Week ended " + str(gate.values_of({"panels": {"friday": run.audit}}, "friday").get("Week ended")),
            "wednesday": "Rates, prices and the flow ledger are current."}.get(run.panel, "")
    desc = [f"**Audited and ready** at {fmt_ny(utcnow(), '%I:%M %p')} ET · post {spec.get('post', '')}.",
            what,
            f"digital-exposure audit: {n.get('PASS', 0)} PASS · {n.get('WARN', 0)} WARN · {n.get('FAIL', 0)} FAIL · "
            f"our checks: {sum(1 for c in run.checks if c['status'] == 'PASS')}/{len(run.checks)} pass",
            note, "Save the image below and attach it to your post."
            + (" Captions with more voice arrive in the Feed in a couple of minutes."
               if config.settings().get("writer", {}).get("mode", "monitor") == "drafts" else "")]
    fields = [("Caption A (facts only)", caption[:1000])]
    if run.warnings:
        fields.append(("Heads-up", _lines(run.warnings)))
    url = xtext.intent_post(caption)
    if len(url) < 1800:
        fields.append(("Post", f"[🚀 Open X with caption A]({url}) (attach the image)"))
    notify.discord(f"🟢 {run.title} ready", "\n\n".join(x for x in desc if x), fields, color=GREEN,
                   image=run.png, image_name=f"{run.panel}.png", kind="showcase_ready")


def _alert(run_id: int, key: str, title: str, text: str, color: int, quiet: bool) -> None:
    if not quiet and _mark(run_id, key):
        notify.discord(title, text, color=color, kind="showcase_status")


# ------------------------------------------------------------------ the loop

def _pregate(panel: str, feed_ok: bool, filings: dict) -> tuple[bool, str]:
    """Cheap checks before spending a full render: Monday needs both 8-Ks, Friday needs the close to settle."""
    now = now_ny()
    if panel == "friday" and nyse_open(now.date()):
        settle = int(de.cfg().get("friday_settle_minutes", 10))
        ready_at = at_ny(now.date(), "16:00") + timedelta(minutes=settle)
        if now < ready_at:
            return False, f"waiting for the 4:00 PM close to settle (from {ready_at:%I:%M %p})"
    if panel == "monday" and len(filings) < 2:
        if not feed_ok:
            return False, "8-K feed unreachable"
        missing = [n for t, n in (("MSTR", "Strategy"), ("ASST", "Strive")) if t not in filings]
        return False, "waiting for this week's 8-K: " + ", ".join(missing)
    return True, ""


def watch(panel: str | None, mode: str, de_dir: Path, de_python: str, quiet: bool = False) -> dict:
    d = today_ny()
    panel = panel or showcase.panel_for(d)
    if not panel:
        return {"skipped": f"no showcase panel on {d:%a %b %d}"}
    spec = showcase.panels().get(panel, {})
    start, nudge, deadline = (at_ny(d, spec.get(k, dflt)) for k, dflt in
                              (("start", "00:00"), ("nudge", "23:58"), ("deadline", "23:59")))
    now = now_ny()
    if mode == "watch" and now < start - timedelta(minutes=15):
        return {"skipped": f"too early for {panel} (window opens {spec.get('start')} ET)"}

    run_id = _get_or_create(d.isoformat(), panel)
    row = _row(run_id)
    if mode == "watch" and row.status in ("ready", "posted"):
        return {"skipped": f"{panel} already {row.status}", "run_id": run_id}

    poll = int(de.cfg().get("poll_seconds", 120))
    every = int(de.cfg().get("render_every_seconds", 300))
    work = Path(tempfile.mkdtemp(prefix="xcp-showcase-"))
    release = weekly_release_date(d)
    last_render = None
    _log(run_id, f"{mode} started for {spec.get('title', panel)} (window {spec.get('start')}–{spec.get('deadline')} ET)")
    while True:
        now = now_ny()
        feed, filings = None, {}
        try:
            feed = de.fetch_feed()
            if panel == "monday":
                filings = de.weekly_filings(feed, release)
        except Exception as e:  # network, format
            log.warning("8-K feed: %s", e)
        with db.session() as s:
            r = s.get(db.ShowcaseRun, run_id)
            r.attempts += 1
            s.commit()
        ok, why = _pregate(panel, feed is not None, filings)
        due = last_render is None or time.monotonic() - last_render >= every
        if ok and (due or mode == "once"):
            v, _ = evaluate(panel, de_dir, de_python, work, run_id, feed, filings)
            last_render = time.monotonic()
            if v.ready:
                draft_id = finalize(run_id, note="Rendered with last-good code; main is broken." if _row(run_id).used_fallback
                                    else "", quiet=quiet)
                return {"panel": panel, "status": "ready", "draft_id": draft_id, "checks": v.summary()}
            if v.failed:
                sig = "fail:" + ",".join(sorted(c.id for c in v.blockers if c.status == "FAIL"))
                _alert(run_id, sig, f"⚠️ {spec.get('title', panel)}: the audit found a problem",
                       f"{_lines([c for c in v.blockers if c.status == 'FAIL'])}\n\nStill retrying until "
                       f"{spec.get('deadline')} ET. Backup drafts are in the Feed.", RED, quiet)
        elif not ok:
            _update(run_id, status="waiting", blockers=[{"id": "pregate", "status": "WAIT", "detail": why}])
            if (_row(run_id).attempts % 10) == 1:
                _log(run_id, why)

        if mode == "once":
            row = _row(run_id)
            return {"panel": panel, "status": row.status, "blockers": row.blockers}
        now = now_ny()
        if nudge <= now < deadline:
            row = _row(run_id)
            _alert(run_id, "nudge", f"⏳ {spec.get('title', panel)} isn't ready yet",
                   f"{_lines(row.blockers or [])}\n\nStill checking until {spec.get('deadline')} ET.", AMBER, quiet)
        if now >= deadline:
            _update(run_id, status="missed")
            row = _row(run_id)
            _alert(run_id, "missed", f"🔴 {spec.get('title', panel)} missed its window",
                   f"Not ready by {spec.get('deadline')} ET.\n\n{_lines(row.blockers or [])}\n\nBackup drafts are in "
                   f"the Feed. Control Room → Showcase can re-check, or use the last render anyway.", RED, quiet)
            _log(run_id, "deadline reached")
            return {"panel": panel, "status": "missed", "blockers": row.blockers}
        time.sleep(poll)


def plan(mode: str, panel: str | None, manual: bool = False) -> dict:
    """Cheap go / no-go before the workflow installs digital-exposure (extra cron starts exit here)."""
    d, now = today_ny(), now_ny()
    if mode == "preflight" and manual:
        return {"go": True, "reason": "manual preflight"}
    if mode == "preflight":
        tomorrow = showcase.panel_for(d + timedelta(days=1))
        if not tomorrow:
            return {"go": False, "reason": "no showcase tomorrow"}
        if (db.kv_get("showcase:preflight") or {}).get("date") == d.isoformat():
            return {"go": False, "reason": "preflight already ran today"}
        return {"go": True, "reason": f"preflight for tomorrow's {tomorrow}"}
    panel = panel or showcase.panel_for(d)
    if not panel:
        return {"go": False, "reason": f"no showcase panel on {d:%a %b %d}"}
    if mode == "once":
        return {"go": True, "panel": panel, "reason": "manual re-check"}
    spec = showcase.panels().get(panel, {})
    start, deadline = at_ny(d, spec.get("start", "00:00")), at_ny(d, spec.get("deadline", "23:59"))
    if now < start - timedelta(minutes=15):
        return {"go": False, "panel": panel, "reason": f"too early (window opens {spec.get('start')} ET)"}
    with db.session() as s:
        r = showcase.run_for(s, d.isoformat(), panel)
    if r and r.status in ("ready", "posted", "missed"):
        return {"go": False, "panel": panel, "reason": f"already {r.status}"}
    if now > deadline + timedelta(hours=1):
        return {"go": False, "panel": panel, "reason": "window closed"}
    return {"go": True, "panel": panel, "reason": "in window"}


def preflight(de_dir: Path, de_python: str, quiet: bool = False) -> dict:
    """Evening check before a showcase day: does today's digital-exposure code still render all three cleanly?"""
    work = Path(tempfile.mkdtemp(prefix="xcp-preflight-"))
    r = de.render(de_dir, de_python, work / "render", monday_check=True)
    out: dict = {"at": utcnow().isoformat(), "date": today_ny().isoformat(), "commit": r.commit,
                 "seconds": r.seconds, "error": r.error, "panels": {}}
    if r.ok:
        for p in de.PANELS:
            v = gate.structural_only(p, r.audit, r.checks, r.pngs.get(p))
            out["panels"][p] = {"clean": not v.failed, "summary": v.summary(),
                                "issues": [c.__dict__ for c in v.blockers + v.warnings]}
        out["audit_summary"] = r.checks.get("summary")
        pub = r.publication or {}
        out["monday_publication"] = {"status": pub.get("status"), "detail": pub.get("subtitle") or pub.get("reason")}
        if all(x["clean"] for x in out["panels"].values()):
            db.kv_set(LAST_GOOD, r.commit)
    db.kv_set("showcase:preflight", out)
    tomorrow = showcase.panel_for(today_ny() + timedelta(days=1))
    bad = r.error or (tomorrow and not out["panels"].get(tomorrow, {}).get("clean", False))
    if bad and not quiet:
        title = showcase.title(tomorrow) if tomorrow else "Showcase"
        detail = r.error or _lines([c for c in out["panels"][tomorrow]["issues"] if c["status"] == "FAIL"])
        last_good = db.kv_get(LAST_GOOD)
        notify.discord(f"⚠️ Preflight: tomorrow's {title} won't render cleanly",
                       f"digital-exposure {r.commit[:7]}:\n{detail}\n\n"
                       + (f"If main is still broken tomorrow, the watcher falls back to {last_good[:7]}, the last "
                          f"commit that rendered cleanly." if last_good else "No earlier clean commit is on record."),
                       color=RED, kind="showcase_status")
    return out
