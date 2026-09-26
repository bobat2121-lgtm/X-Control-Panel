"""Digital Credit Report (bobat2121-lgtm/digital-exposure): its 8-K feed and a read-only render of its X images.

Nothing here writes to that repository. The showcase workflow checks it out and runs its own scripts:
  scripts/render_previews.py --out DIR     -> monday.png, wednesday.png, friday.png, audit.json
  scripts/audit_panels.py --out DIR        -> checks.json (independent recomputation, PASS / WARN / FAIL)
  scripts/check_monday_publication.py --live --output-dir DIR -> check.json (Monday edition admission)
The page's "Download X image" button (panels_page.py) calls the same render functions with the same
inputs, so the PNG is what the button would give you at that moment.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from xcp import config
from xcp.timeutil import NY, parse_iso

log = logging.getLogger(__name__)
PANELS = ("monday", "wednesday", "friday")
WEEKLY_OK = {"ready_for_review", "validated_for_review"}


def cfg() -> dict:
    return config.settings().get("digital_exposure", {})


def panel_spec(panel: str) -> dict:
    return (cfg().get("panels") or {}).get(panel, {})


def report_url(panel: str) -> str:
    return f"{(cfg().get('app_url') or '').rstrip('/')}/?report={panel}"


# ------------------------------------------------------------------ 8-K feed (Cloudflare Worker, public)

def fetch_feed(timeout: int = 20) -> dict:
    r = httpx.get(cfg().get("feed_url"), headers={"User-Agent": "XControlPanel-showcase/1.0"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("filings"), list):
        raise ValueError("8-K feed: unexpected format")
    return data


def _is_weekly(f: dict) -> bool:
    ex = f.get("extracted") or {}
    facts = ex.get("facts") or {}
    return (f.get("form") == "8-K" and not f.get("baseline") and f.get("status") in WEEKLY_OK
            and bool(ex.get("extractionValidated")) and bool(ex.get("balanceDate"))
            and ("weekly_btc_purchases" in facts or "weekly_btc_sales" in facts))


def weekly_filings(feed: dict, release: date) -> dict[str, dict]:
    """This week's validated weekly 8-K per issuer (MSTR, ASST), accepted on the release date in New York."""
    out: dict[str, dict] = {}
    for f in feed.get("filings", []):
        if f.get("ticker") not in ("MSTR", "ASST") or not _is_weekly(f):
            continue
        accepted = parse_iso(f.get("acceptedAt"))
        if accepted is None or accepted.astimezone(NY).date() != release:
            continue
        prev = out.get(f["ticker"])
        if prev is None or parse_iso(prev.get("acceptedAt")) < accepted:
            out[f["ticker"]] = f
    return out


def latest_weekly(feed: dict, ticker: str) -> dict | None:
    rows = [f for f in feed.get("filings", []) if f.get("ticker") == ticker and _is_weekly(f)]
    rows.sort(key=lambda f: f["extracted"]["balanceDate"])
    return rows[-1] if rows else None


def filing_summary(f: dict) -> dict:
    ex = f.get("extracted") or {}
    return {"ticker": f.get("ticker"), "accession": f.get("accession"), "accepted_at": f.get("acceptedAt"),
            "first_seen_at": f.get("firstSeenAt"), "url": f.get("primaryDocumentUrl"),
            "period_start": ex.get("periodStart"), "balance_date": ex.get("balanceDate"),
            "facts": ex.get("facts") or {}, "securities": ex.get("securities") or {}}


# ------------------------------------------------------------------ render + audit (runs the repo's own scripts)

@dataclass
class Render:
    ok: bool = False
    commit: str = ""
    audit: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    publication: dict | None = None
    pngs: dict[str, bytes] = field(default_factory=dict)
    error: str = ""
    output: str = ""
    seconds: float = 0.0
    used_fallback: bool = False
    fallback_reason: str = ""

    @property
    def saved_quotes(self) -> bool:
        """render_previews.py prints this when the live quote refresh failed."""
        return "using saved quotes" in self.output


def git_head(repo_dir: Path | str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
        return p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _run(python: str, args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run([str(python), *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def _tail(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[-n:]


def render(repo_dir: Path | str, python: str, out_dir: Path | str, *, monday_check: bool = False,
           timeout: int = 900) -> Render:
    repo_dir, out_dir = Path(repo_dir), Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = Render(commit=git_head(repo_dir))
    logs: list[str] = []
    t0 = time.monotonic()
    try:
        p = _run(python, ["scripts/render_previews.py", "--out", str(out_dir)], repo_dir, timeout)
        logs.append(p.stdout + p.stderr)
        if p.returncode != 0:
            res.error = f"render_previews.py exited {p.returncode}: {_tail(p.stderr)}"
            return res
        p = _run(python, ["scripts/audit_panels.py", "--out", str(out_dir)], repo_dir, timeout)
        logs.append(p.stdout + p.stderr)
        if p.returncode != 0:
            res.error = f"audit_panels.py exited {p.returncode}: {_tail(p.stderr)}"
            return res
        if monday_check:
            pub_dir = out_dir / "publication"
            p = _run(python, ["scripts/check_monday_publication.py", "--live", "--output-dir", str(pub_dir)],
                     repo_dir, timeout)
            logs.append(p.stdout + p.stderr)
            try:  # exit 1 with check.json = the newest pair isn't complete yet (not a crash)
                res.publication = json.loads((pub_dir / "check.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                res.publication = {"status": "error", "reason": _tail(p.stderr) or f"exit {p.returncode}"}
        res.audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
        res.checks = json.loads((out_dir / "checks.json").read_text(encoding="utf-8"))
        for name in PANELS:
            f = out_dir / f"{name}.png"
            if f.exists():
                res.pngs[name] = f.read_bytes()
        res.ok = True
    except subprocess.TimeoutExpired as e:
        res.error = f"timed out after {e.timeout}s"
    except (OSError, ValueError) as e:
        res.error = f"{type(e).__name__}: {e}"
    finally:
        res.output = "\n".join(logs)[-12000:]
        res.seconds = round(time.monotonic() - t0, 1)
    return res


def checkout_commit(repo_dir: Path | str, commit: str, dest: Path | str) -> Path | None:
    """A detached worktree of an older commit (the last code that rendered cleanly)."""
    repo_dir, dest = Path(repo_dir), Path(dest)
    subprocess.run(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(dest)],
                   capture_output=True, timeout=60)
    shutil.rmtree(dest, ignore_errors=True)
    p = subprocess.run(["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(dest), commit],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        log.warning("worktree for %s failed: %s", commit, p.stderr[-400:])
        return None
    return dest
