"""Checks — "done" means the app looked, not that the model said so.

Marking a step done has always needed evidence, but the evidence was a
sentence the model wrote: "ran the script, output correct". Nothing compared
that sentence with the world. A model that is wrong, rushed, or misled by a
page it read can write that sentence about work that didn't happen, and an
unattended run is exactly where nobody is there to notice.

A CHECK is a small fact the app verifies itself:

  {"type": "file_exists",   "path": "report.xlsx", "min_bytes": 1000}
  {"type": "file_contains", "path": "out.csv", "text": "TOTAL"}     (or "regex")
  {"type": "command",       "command": "pytest -q", "expect_exit": 0,
                            "expect_output": "passed"}
  {"type": "url",           "url": "https://site/health", "status": 200,
                            "contains": "ok"}
  {"type": "sent",          "tool": "send_email", "contains": "jo@acme.com"}

`sent` looks in the approval queue: it passes only once an approved call
whose summary matches has actually RUN. While it is still waiting for the
person, the check says so, and the step belongs in 'blocked', not 'done'.

A step with checks can't be marked done unless they pass right then; a task
can't be completed unless every check on its done steps still passes; a goal
with checks can't be 'achieved' unless they pass. A failure comes back with
what was actually observed, so the agent can fix the work rather than the
wording.

Command checks get no extra power: one runs only if the same command would
run WITHOUT asking (read-only, a rule you saved, or full access). Otherwise
the check reports that it couldn't run, and why. Checks never prompt.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

TYPES = ("file_exists", "file_contains", "command", "url", "sent")
MAX_CHECKS = 8
COMMAND_TIMEOUT = 120


def normalise(raw) -> list[dict]:
    """Accept one check or a list, as dicts or a JSON string."""
    if raw in (None, "", [], {}):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    return [c for c in raw if isinstance(c, dict)][:MAX_CHECKS]


def problem(c: dict) -> str:
    """Why this check can't be run as written ('' if it can)."""
    t = c.get("type")
    if t not in TYPES:
        return f"unknown check type {t!r} (use {', '.join(TYPES)})"
    need = {"file_exists": ["path"], "file_contains": ["path"], "command": ["command"],
            "url": ["url"], "sent": ["contains"]}[t]
    missing = [k for k in need if not str(c.get(k) or "").strip()]
    if missing:
        return f"{t} check needs {', '.join(missing)}"
    if t == "file_contains" and not (c.get("text") or c.get("regex")):
        return "file_contains check needs text or regex"
    if t == "url" and not str(c["url"]).startswith(("http://", "https://")):
        return "url check needs an http(s) url"
    if t == "file_contains" and c.get("regex"):
        try:
            re.compile(c["regex"])
        except re.error as exc:
            return f"bad regex: {exc}"
    return ""


def describe(c: dict) -> str:
    t = c.get("type")
    if t == "file_exists":
        return f"{c['path']} exists" + (f" (≥{c['min_bytes']} bytes)" if c.get("min_bytes") else "")
    if t == "file_contains":
        return f"{c['path']} contains " + (f"/{c['regex']}/" if c.get("regex") else f"“{c['text']}”")
    if t == "command":
        return f"`{c['command'][:80]}` exits {int(c.get('expect_exit', 0))}" + (
            f" and prints “{c['expect_output']}”" if c.get("expect_output") else "")
    if t == "url":
        return f"{c['url']} answers {int(c.get('status', 200))}" + (
            f" with “{c['contains']}”" if c.get("contains") else "")
    if t == "sent":
        return f"an approved {c.get('tool') or 'action'} matching “{c['contains']}” went out"
    return str(c)


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #
def _file(c: dict) -> dict:
    p = Path(str(c["path"])).expanduser()
    if not p.exists():
        return {"ok": False, "observed": f"{p} does not exist"}
    if c["type"] == "file_exists":
        size = p.stat().st_size if p.is_file() else 0
        if c.get("min_bytes") and size < int(c["min_bytes"]):
            return {"ok": False, "observed": f"{p} is only {size} bytes"}
        return {"ok": True, "observed": f"{p} exists ({size} bytes)" if p.is_file() else f"{p} exists"}
    if not p.is_file():
        return {"ok": False, "observed": f"{p} is a folder, not a file"}
    text = p.read_text("utf-8", errors="replace")
    if c.get("regex"):
        m = re.search(c["regex"], text, re.M)
        return ({"ok": True, "observed": f"matched “{m.group(0)[:80]}”"} if m
                else {"ok": False, "observed": f"no match in {len(text):,} characters"})
    if str(c["text"]) in text:
        return {"ok": True, "observed": f"found “{str(c['text'])[:60]}”"}
    return {"ok": False, "observed": f"“{str(c['text'])[:60]}” not in {p.name} ({len(text):,} characters)"}


def _command(c: dict, memory=None, auto_approve: bool = False, session_id: str = "") -> dict:
    from .tools import is_read_only_command
    cmd = str(c["command"])
    allowed = auto_approve or is_read_only_command(cmd)
    if not allowed and memory is not None:
        try:
            allowed = bool(memory.command_permitted(cmd))
        except Exception:
            allowed = False
    if not allowed:
        return {"ok": False, "not_run": True,
                "observed": ("not run: this command would need the person's permission. "
                             "Run it with run_command and have them choose 'always allow', "
                             "or check another way")}
    from . import sandbox
    plan = sandbox.prepare(cmd, session_id)       # the same fence as run_command
    if "refuse" in plan:
        return {"ok": False, "not_run": True, "observed": "not run: " + plan["refuse"]}
    try:
        t = int(c.get("timeout") or COMMAND_TIMEOUT)
        if "argv" in plan:
            r = subprocess.run(plan["argv"], capture_output=True, text=True, env=plan["env"],
                               cwd=plan["cwd"], timeout=t)
        else:
            r = subprocess.run(plan["shell"], shell=True, capture_output=True, text=True,
                               env=plan["env"], timeout=t)
    except subprocess.TimeoutExpired:
        return {"ok": False, "observed": "timed out"}
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    tail = out[-300:]
    want = int(c.get("expect_exit", 0))
    if r.returncode != want:
        return {"ok": False, "observed": f"exit {r.returncode}, wanted {want}: {tail}"}
    if c.get("expect_output") and str(c["expect_output"]) not in out:
        return {"ok": False, "observed": f"exit {r.returncode} but “{c['expect_output']}” "
                                         f"not in the output: {tail}"}
    return {"ok": True, "observed": f"exit {r.returncode}" + (f": {tail[-120:]}" if tail else "")}


def _url(c: dict) -> dict:
    want = int(c.get("status", 200))
    req = urllib.request.Request(c["url"], headers={"User-Agent": "AgentJo-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code, body = r.status, r.read(400_000).decode("utf-8", "replace")
    except Exception as exc:
        code = getattr(exc, "code", 0) or 0
        if not code:
            return {"ok": False, "observed": f"couldn't reach it: {type(exc).__name__}: {str(exc)[:120]}"}
        body = ""
    if code != want:
        return {"ok": False, "observed": f"answered {code}, wanted {want}"}
    if c.get("contains") and str(c["contains"]) not in body:
        return {"ok": False, "observed": f"answered {code} but “{c['contains']}” isn't on the page"}
    return {"ok": True, "observed": f"answered {code}"}


def _sent(c: dict) -> dict:
    from . import intercepts
    want = str(c["contains"]).lower()
    tool = str(c.get("tool") or "")
    hits = [i for i in intercepts.pending()
            if (not tool or i.get("tool") == tool)
            and (want in str(i.get("summary", "")).lower()
                 or want in json.dumps(i.get("args") or {}, default=str).lower())]
    done = [i for i in hits if i.get("state") == "done"]
    if done:
        return {"ok": True, "observed": f"went out {done[-1].get('ran_at', '')}: {done[-1]['summary'][:100]}"}
    waiting = [i for i in hits if i.get("state") in ("waiting", "approved")]
    if waiting:
        return {"ok": False, "waiting": True,
                "observed": "still waiting for the person's approval: this step is "
                            "blocked on them, not done"}
    bad = [i for i in hits if i.get("state") in ("refused", "failed")]
    if bad:
        return {"ok": False, "observed": f"it was {bad[-1]['state']}: {str(bad[-1].get('result') or '')[:120]}"}
    return {"ok": False, "observed": "nothing matching was sent"}


def run(c: dict, *, memory=None, auto_approve: bool = False, session_id: str = "") -> dict:
    why = problem(c)
    if why:
        return {"ok": False, "check": str(c)[:120], "observed": why}
    try:
        t = c["type"]
        if t in ("file_exists", "file_contains"):
            r = _file(c)
        elif t == "command":
            r = _command(c, memory, auto_approve, session_id)
        elif t == "url":
            r = _url(c)
        else:
            r = _sent(c)
    except Exception as exc:
        r = {"ok": False, "observed": f"{type(exc).__name__}: {str(exc)[:160]}"}
    r["check"] = describe(c)
    return r


def run_all(checks: list[dict], *, memory=None, auto_approve: bool = False,
            session_id: str = "") -> dict:
    results = [run(c, memory=memory, auto_approve=auto_approve, session_id=session_id)
               for c in checks]
    ok = bool(results) and all(r["ok"] for r in results)
    _audit(ok, results)
    return {"ok": ok, "results": results,
            "waiting": any(r.get("waiting") for r in results),
            "text": "; ".join(("✓ " if r["ok"] else "✗ ") + f"{r['check']}: {r['observed']}"
                              for r in results)}


def _audit(ok: bool, results: list) -> None:
    try:
        from . import audit
        audit.record("check", ok=ok, n=len(results),
                     failed="; ".join(r["check"] for r in results if not r["ok"])[:200])
    except Exception:
        pass
