"""LLM calls through Codex CLI signed in with your ChatGPT account (no API key).

In the cloud (GitHub Actions) the Codex login lives encrypted in the database:
it is restored to CODEX_HOME before a run and written back afterwards, because
Codex refreshes the token in place roughly every 8 days.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from xcp import config, db
from xcp.config import env
from xcp.timeutil import utcnow

log = logging.getLogger(__name__)
SCHEMA_DIR = Path(__file__).parent / "schemas"
PROMPT_DIR = Path(__file__).parent / "prompts"
AUTH_KV_KEY = "codex_auth"


class LLMError(RuntimeError):
    pass


def backend() -> str:
    return (env("LLM_BACKEND") or config.settings().get("llm", {}).get("backend") or "codex").lower()


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def render_prompt(name: str, **values) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    for k, v in values.items():
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, indent=1, default=str)
        text = text.replace("{{" + k + "}}", v)
    return text


def run_json(prompt: str, schema_name: str, mock: Callable[[], dict] | None = None) -> dict:
    if backend() == "mock":
        if mock is None:
            raise LLMError("mock backend has no mock for this task")
        return mock()
    return _run_codex(prompt, load_schema(schema_name))


# ------------------------------------------------------------------ codex auth

def _fernet():
    from cryptography.fernet import Fernet

    key = env("CODEX_AUTH_KEY")
    if not key:
        raise LLMError("CODEX_AUTH_KEY is not set")
    return Fernet(key.encode())


def store_auth(auth_json_text: str) -> None:
    json.loads(auth_json_text)  # validate
    token = _fernet().encrypt(auth_json_text.encode()).decode()
    db.kv_set(AUTH_KV_KEY, {"token": token, "updated_at": utcnow().isoformat()})


def _codex_home() -> Path | None:
    """When CODEX_AUTH_FROM_DB=1, build a private CODEX_HOME from the DB copy."""
    if env("CODEX_AUTH_FROM_DB") != "1":
        return None
    home = Path(env("CODEX_HOME") or (Path(tempfile.gettempdir()) / "xcp-codex"))
    home.mkdir(parents=True, exist_ok=True)
    auth_path = home / "auth.json"
    if not auth_path.exists():
        row = db.kv_get(AUTH_KV_KEY)
        if not row:
            raise LLMError("No Codex login in the database. Run: python -m jobs.run upload-codex-auth")
        auth_path.write_text(_fernet().decrypt(row["token"].encode()).decode(), encoding="utf-8")
        os.chmod(auth_path, 0o600)
    cfg = home / "config.toml"
    if not cfg.exists():
        cfg.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
    return home


def _persist_auth_if_changed(home: Path | None, before: str | None) -> None:
    if home is None:
        return
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return
    after = auth_path.read_text(encoding="utf-8")
    if hashlib.sha256(after.encode()).hexdigest() != before:
        store_auth(after)
        log.info("Codex login refreshed and saved back to the database")


# ------------------------------------------------------------------ runner

def _run_codex(prompt: str, schema: dict, timeout: int = 900) -> dict:
    # On Windows npm installs a bare 'codex' shell script next to codex.cmd; only the .cmd is runnable here.
    exe = (shutil.which("codex.cmd") if os.name == "nt" else None) or shutil.which("codex")
    if not exe:
        raise LLMError("Codex CLI not found. Install with: npm install -g @openai/codex")
    home = _codex_home()
    before = None
    if home is not None:
        before = hashlib.sha256((home / "auth.json").read_bytes()).hexdigest()
    run_env = dict(os.environ)
    if home is not None:
        run_env["CODEX_HOME"] = str(home)

    llm_cfg = config.settings().get("llm", {})
    model = env("CODEX_MODEL") or llm_cfg.get("codex_model") or ""
    effort = env("CODEX_REASONING_EFFORT") or llm_cfg.get("reasoning_effort") or ""
    extra = shlex.split(env("CODEX_EXTRA_ARGS", "") or "")
    if effort:  # unquoted value: Codex falls back to a raw string, which avoids shell-quoting issues on Windows
        extra = ["-c", f"model_reasoning_effort={effort}"] + extra
    last_err = ""
    try:
        for attempt in (1, 2):
            with tempfile.TemporaryDirectory() as td:
                schema_path = Path(td) / "schema.json"
                out_path = Path(td) / "out.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
                       "--output-schema", str(schema_path), "-o", str(out_path)]
                if model:
                    cmd += ["-m", model]
                cmd += extra + ["-"]
                try:
                    proc = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", capture_output=True,
                                          cwd=td, env=run_env, timeout=timeout)
                except subprocess.TimeoutExpired:
                    last_err = f"codex timed out after {timeout}s"
                    continue
                raw = out_path.read_text(encoding="utf-8") if out_path.exists() else proc.stdout
                parsed = _parse_json(raw)
                if proc.returncode == 0 and parsed is not None:
                    return parsed
                last_err = f"exit {proc.returncode}: {(proc.stderr or '')[-1500:]}"
                log.warning("codex attempt %s failed: %s", attempt, last_err)
        raise LLMError(last_err)
    finally:
        _persist_auth_if_changed(home, before)


def _parse_json(raw: str | None):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None
