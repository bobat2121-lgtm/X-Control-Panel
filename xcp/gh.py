"""Start GitHub Actions workflows (panel buttons, and the showcase watcher handing off to the agent)."""
from __future__ import annotations

import logging

import httpx

from xcp.config import env

log = logging.getLogger(__name__)


def can_dispatch() -> bool:
    return bool(env("GH_DISPATCH_TOKEN") and env("GITHUB_REPO"))


def dispatch(workflow: str, inputs: dict | None = None) -> tuple[bool, str]:
    """workflow_dispatch on this repo. Needs GH_DISPATCH_TOKEN (Actions: write) and GITHUB_REPO."""
    token, repo = env("GH_DISPATCH_TOKEN"), env("GITHUB_REPO")
    if not (token and repo):
        return False, "GH_DISPATCH_TOKEN / GITHUB_REPO not set"
    body: dict = {"ref": env("GITHUB_BRANCH", "main")}
    if inputs:
        body["inputs"] = {k: str(v) for k, v in inputs.items()}
    try:
        r = httpx.post(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                       json=body, timeout=15)
    except httpx.HTTPError as e:
        return False, str(e)
    if r.status_code in (201, 204):
        return True, "started"
    log.warning("dispatch %s failed: %s %s", workflow, r.status_code, r.text[:300])
    return False, f"HTTP {r.status_code}"
