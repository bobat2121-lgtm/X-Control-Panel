"""Agent entry point.

  python -m jobs.run plan [--job NAME]        # prints GitHub step outputs (work / needs_llm / jobs)
  python -m jobs.run auto [--job NAME]        # run whatever is due + queued panel requests
  python -m jobs.run job premarket            # run one job now (premarket|ai_noon|midday|friday_close|nightly|weekly|requests|snapshot)
  python -m jobs.run seed-demo                # fill the DB with mock data to try the panel offline
  python -m jobs.run gen-key                  # make a CODEX_AUTH_KEY
  python -m jobs.run upload-codex-auth [--path .codex-cloud/auth.json]
  python -m jobs.run test-discord
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from xcp.config import ROOT


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Request URLs can carry API keys in query strings, and Actions logs are public on a public repo.
    for noisy in ("httpx", "httpcore", "yfinance", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="jobs.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--job", default="")
    p_auto = sub.add_parser("auto")
    p_auto.add_argument("--job", default="")
    p_job = sub.add_parser("job")
    p_job.add_argument("name")
    sub.add_parser("seed-demo")
    sub.add_parser("gen-key")
    p_up = sub.add_parser("upload-codex-auth")
    p_up.add_argument("--path", default=str(ROOT / ".codex-cloud" / "auth.json"))
    sub.add_parser("test-discord")
    args = ap.parse_args(argv)

    if args.cmd == "gen-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0

    from xcp import scheduler

    if args.cmd == "plan":
        p = scheduler.plan(args.job or None)
        lines = [f"work={'true' if p['work'] else 'false'}",
                 f"needs_llm={'true' if p['needs_llm'] else 'false'}",
                 f"jobs={','.join(p['jobs'])}", f"requests={p['requests']}"]
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        print("\n".join(lines))
        return 0

    if args.cmd == "auto":
        results = scheduler.run_auto(args.job or None)
        print(json.dumps(results, indent=1, default=str))
        return 1 if any(r.get("status") == "error" for r in results) else 0

    if args.cmd == "job":
        r = scheduler.run_job(args.name, trigger="manual")
        print(json.dumps(r, indent=1, default=str))
        return 0 if r.get("status") == "ok" else 1

    if args.cmd == "seed-demo":
        from jobs.demo import seed

        seed()
        return 0

    if args.cmd == "upload-codex-auth":
        from xcp import llm

        path = Path(args.path)
        if not path.exists():
            print(f"Not found: {path}\nCreate it with a dedicated login (see README, step 4).", file=sys.stderr)
            return 1
        llm.store_auth(path.read_text(encoding="utf-8"))
        print("Codex login stored (encrypted) in the database.")
        return 0

    if args.cmd == "test-discord":
        from xcp import notify

        ok = notify.discord("✅ X Control Panel is connected", "Alerts from your agents will land here.")
        print("sent" if ok else "not sent (check DISCORD_WEBHOOK_URL)")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
