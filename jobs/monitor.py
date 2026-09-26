"""The 15-minute news monitor (monitor.yml). Light on purpose: no AI, no market-data libraries.

  python -m jobs.monitor            # pull everything new, flag priority items, ping Discord
  python -m jobs.monitor --no-ping  # same, without Discord
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="jobs.monitor")
    ap.add_argument("--no-ping", action="store_true")
    args = ap.parse_args(argv)

    from xcp import db
    from xcp.agents import monitor
    from xcp.timeutil import today_ny, utcnow

    with db.session() as s:
        run = db.Run(job="monitor", run_date=today_ny().isoformat(), trigger="schedule", status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    status, stats = "ok", {}
    try:
        stats = monitor.run(ping=not args.no_ping)
    except Exception as e:
        status, stats = "error", {"error": str(e)[:500]}
        logging.error("monitor failed:\n%s", traceback.format_exc())
    with db.session() as s:
        run = s.get(db.Run, run_id)
        run.status, run.stats, run.finished_at = status, stats, utcnow()
        run.x_reads = int(stats.get("x_reads", 0) or 0)
        s.commit()
    print(json.dumps(stats, default=str))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
