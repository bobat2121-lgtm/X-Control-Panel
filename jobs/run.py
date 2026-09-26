"""Agent entry point.

  python -m jobs.run plan [--job NAME]        # prints GitHub step outputs (work / needs_llm / jobs)
  python -m jobs.run auto [--job NAME]        # run whatever is due + queued panel requests
  python -m jobs.run job premarket            # run one job now (premarket|ai_noon|midday|friday_close|nightly|weekly|requests|snapshot)
  python -m jobs.run seed-demo                # fill the DB with mock data to try the panel offline
  python -m jobs.run gen-key                  # make a CODEX_AUTH_KEY
  python -m jobs.run upload-codex-auth [--path .codex-cloud/auth.json]
  python -m jobs.run test-discord
  python -m jobs.run showcase --mode watch|once|preflight [--panel monday|wednesday|friday]
                               --de-dir PATH --de-python PATH [--quiet]
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
    p_sc = sub.add_parser("showcase", help="watch / re-check / preflight the Digital Credit Report showcase")
    p_sc.add_argument("--mode", default="watch", choices=["watch", "once", "preflight"])
    p_sc.add_argument("--panel", default="", help="monday | wednesday | friday (default: today's)")
    p_sc.add_argument("--de-dir", default=os.environ.get("DE_DIR", "de"), help="digital-exposure checkout")
    p_sc.add_argument("--de-python", default=os.environ.get("DE_PYTHON", sys.executable),
                      help="Python with digital-exposure's requirements installed")
    p_sc.add_argument("--quiet", action="store_true", help="no Discord alerts")
    p_sc.add_argument("--plan", action="store_true", help="only print go=true|false for the workflow")
    p_style = sub.add_parser("load-style", help="load style-library entries from a JSON file")
    p_style.add_argument("path")
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

    if args.cmd == "showcase":
        from xcp.agents import showcase_watch

        if args.plan:
            p = showcase_watch.plan(args.mode, args.panel or None,
                                    manual=os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
            lines = [f"go={'true' if p['go'] else 'false'}", f"panel={p.get('panel', '')}", f"reason={p['reason']}"]
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            print("\n".join(lines))
            return 0
        de_dir = Path(args.de_dir).resolve()
        if not (de_dir / "scripts" / "render_previews.py").exists():
            print(f"digital-exposure checkout not found at {de_dir}", file=sys.stderr)
            return 1
        if Path(args.de_python).exists():  # its scripts run with cwd=de_dir, so a relative path would break
            args.de_python = str(Path(args.de_python).resolve())
        if args.mode == "preflight":
            fn = lambda: showcase_watch.preflight(de_dir, args.de_python, quiet=args.quiet)  # noqa: E731
            name = "showcase:preflight"
        else:
            fn = lambda: showcase_watch.watch(args.panel or None, args.mode, de_dir, args.de_python,  # noqa: E731
                                              quiet=args.quiet)
            name = f"showcase:{args.panel or 'today'}"
        trigger = "manual" if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" else "schedule"
        r = scheduler.run_job(name, trigger=trigger, fn=fn)
        print(json.dumps(r, indent=1, default=str))
        return 0 if r.get("status") == "ok" and not r.get("error") else 1

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

    if args.cmd == "load-style":
        from xcp import db

        entries = json.loads(Path(args.path).read_text(encoding="utf-8"))
        added = skipped = 0
        with db.session() as s:
            existing = {(r.url, r.hook_type) for r in s.query(db.StyleExample).all()}
            for e in entries:
                url = e.get("url") or (f"https://x.com/{e['handle']}/status/{e['id']}" if e.get("id") else "")
                if (url, e.get("hook_type", "")) in existing:
                    skipped += 1
                    continue
                s.add(db.StyleExample(
                    source=e.get("source", "admired"), handle=e.get("handle", ""), url=url,
                    format=e.get("format", "short_observation"), length=e.get("length", "short"),
                    pillar=e.get("pillar", "bitcoin"), hook_type=e.get("hook_type", ""), pattern=e.get("pattern", ""),
                    skeleton=e.get("skeleton", ""), demo=e.get("demo", ""), why_it_works=e.get("why", ""),
                    text=e.get("text", "") if e.get("source") == "mine" else "",
                    metrics={k: e[k] for k in ("likes", "views", "reposts") if k in e},
                    strength=int(e.get("strength", 7))))
                added += 1
            s.commit()
        print(f"style library: added {added}, skipped {skipped} duplicates")
        return 0

    if args.cmd == "test-discord":
        from xcp import notify

        ok = notify.discord("✅ X Control Panel is connected", "Alerts from your agents will land here.", kind="test")
        print("sent" if ok else "not sent (check DISCORD_WEBHOOK_URL)")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
