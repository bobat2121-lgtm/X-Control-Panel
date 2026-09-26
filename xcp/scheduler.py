"""Decides what's due. GitHub cron fires at both EST and EDT offsets; we gate on New York time."""
from __future__ import annotations

import io
import logging
import traceback
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db, llm, notify
from xcp.agents import analyst, ondemand, slot, strategist
from xcp.timeutil import at_ny, aware, days_match, now_ny, today_ny, utcnow

log = logging.getLogger(__name__)
WINDOW_BEFORE = timedelta(minutes=20)
WINDOW_AFTER = timedelta(minutes=100)


def schedule() -> dict[str, dict]:
    st = config.settings()
    jobs = {name: {"run_at": s["run_at"], "days": s.get("days"), "kind": "slot"} for name, s in st["slots"].items()}
    for name, s in st.get("jobs", {}).items():
        jobs[name] = {"run_at": s["run_at"], "days": s.get("days"), "kind": name}
    return jobs


def _ran_today(job: str) -> bool:
    with db.session() as s:
        rows = s.scalars(select(db.Run).where(db.Run.job == job, db.Run.run_date == today_ny().isoformat())).all()
    for r in rows:
        if r.status == "ok":
            return True
        if r.status == "running" and utcnow() - aware(r.started_at) < timedelta(minutes=45):
            return True
    return False


def due_jobs() -> list[str]:
    now = now_ny()
    due = []
    for name, spec in schedule().items():
        if not days_match(spec["days"], now.date()):
            continue
        target = at_ny(now.date(), spec["run_at"])
        if target - WINDOW_BEFORE <= now <= target + WINDOW_AFTER and not _ran_today(name):
            due.append(name)
    return due


def forced_jobs() -> list[str]:
    """'Run now' buttons in the panel queue a run_job request."""
    return [r.payload.get("job") for r in ondemand.pending("run_job") if (r.payload or {}).get("job")]


def plan(force: str | None = None) -> dict:
    jobs = list(dict.fromkeys(([force] if force else []) + forced_jobs() + due_jobs()))
    llm_requests = [r for r in ondemand.pending() if r.kind in ondemand.LLM_KINDS]
    work = bool(jobs or llm_requests)
    needs_llm = work and llm.backend() == "codex"
    return {"jobs": jobs, "requests": len(llm_requests), "work": work, "needs_llm": needs_llm}


def run_job(name: str, trigger: str = "schedule") -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    with db.session() as s:
        run = db.Run(job=name, run_date=today_ny().isoformat(), trigger=trigger, status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    status, stats = "ok", {}
    try:
        stats = _dispatch(name) or {}
    except Exception as e:
        status = "error"
        stats = {"error": str(e)[:500]}
        log.error("job %s failed:\n%s", name, traceback.format_exc())
        notify.discord(f"❌ {name} failed", str(e)[:1500], color=0xE74C3C)
    finally:
        logging.getLogger().removeHandler(handler)
        with db.session() as s:
            run = s.get(db.Run, run_id)
            run.status, run.stats, run.finished_at = status, stats, utcnow()
            run.x_reads = int(stats.get("x_reads", 0) or 0)
            run.log = stream.getvalue()[-20000:]
            s.commit()
    return {"job": name, "status": status, **stats}


def _dispatch(name: str) -> dict:
    st = config.settings()
    if name in st["slots"]:
        return slot.run_slot(name)
    if name == "nightly":
        out = {"posts": analyst.import_own_posts()}
        out["x_reads"] = out["posts"].get("x_reads", 0)
        out["ideas"] = strategist.daily()
        out["readiness"] = strategist.readiness_alert()
        return out
    if name == "weekly":
        return strategist.weekly()
    if name == "style_refresh":
        from xcp.agents import style

        return style.refresh()
    if name == "requests":
        return ondemand.process_all()
    if name == "snapshot":
        from xcp.sources import market

        market.take_snapshot()
        return {"snapshot": "ok"}
    raise ValueError(f"unknown job: {name}")


def run_auto(force: str | None = None) -> list[dict]:
    forced = set(forced_jobs())
    p = plan(force)
    results = []
    # run_job requests are consumed here (their job runs below)
    for r in ondemand.pending("run_job"):
        with db.session() as s:
            row = s.get(db.Request, r.id)
            row.status, row.finished_at = "done", utcnow()
            s.commit()
    for job in p["jobs"]:
        trigger = "manual" if (job == force or job in forced) else "schedule"
        results.append(run_job(job, trigger))
    if [r for r in ondemand.pending() if r.kind in ondemand.LLM_KINDS]:
        results.append(run_job("requests", "panel"))
    return results
