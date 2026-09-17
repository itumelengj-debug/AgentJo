"""Issues — capture app problems as structured, paste-able reports.

When something in Agent Jo itself misbehaves (a bad answer, a button that did
nothing, an error mid-turn), one click or one chat message snapshots everything
a developer needs into AGENT_HOME/problems.jsonl:

  • your one-line description of what went wrong
  • the recent conversation transcript (what led up to it)
  • the engine in use, plus app/environment context
  • the most recent internal errors (chat worker + scheduler exceptions are
    fed into a small ring buffer as they happen)

Reports are LOCAL ONLY — nothing is sent anywhere. Each report formats to a
compact text block; copy it (or the whole file) into a session with Claude and
that's the bug report. The transcript excerpt can contain conversation
content, so review before sharing.
"""
from __future__ import annotations

import json
import platform
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import config

_LOCK = threading.Lock()
RECENT_ERRORS: deque = deque(maxlen=30)     # (ts, source, message)


def _path():
    return config.AGENT_HOME / "problems.jsonl"


def _err_path():
    return config.AGENT_HOME / "errors.jsonl"


def note_error(source: str, message: str) -> None:
    """Feed an internal error into the ring buffer AND persist it, so errors are
    picked up automatically (chat worker, scheduler, HTTP 500s, and the browser
    UI all report here) and survive a restart. Cheap, never raises."""
    try:
        ts = time.time()
        src = str(source)[:40]
        msg = " ".join(str(message).split())[:300]
        RECENT_ERRORS.append((ts, src, msg))
        p = _err_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "source": src, "message": msg}) + "\n")
        # keep the file from growing without bound
        try:
            if p.stat().st_size > 512_000:
                lines = p.read_text("utf-8").splitlines()[-500:]
                with _LOCK:
                    p.write_text("\n".join(lines) + "\n", "utf-8")
        except Exception:
            pass
    except Exception:
        pass


def recent_errors(n: int = 10) -> list[dict]:
    """Latest auto-captured errors, newest first (file-backed, restart-proof)."""
    out = []
    try:
        for ln in _err_path().read_text("utf-8").splitlines()[-max(1, n):]:
            try:
                e = json.loads(ln)
                out.append({"ts": e.get("ts", 0),
                            "iso": datetime.fromtimestamp(
                                float(e.get("ts", 0)), timezone.utc
                            ).strftime("%Y-%m-%d %H:%M UTC"),
                            "source": e.get("source", ""),
                            "message": e.get("message", "")})
            except Exception:
                pass
    except Exception:
        pass
    return list(reversed(out))


def clear_errors() -> int:
    n = len(recent_errors(500))
    try:
        with _LOCK:
            _err_path().unlink(missing_ok=True)
        RECENT_ERRORS.clear()
    except Exception:
        pass
    return n


def _env() -> dict:
    return {
        "build": getattr(config, "BUILD_ID", "unknown"),
        "platform": platform.platform(terse=True),
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "venv": "yes" if sys.prefix != getattr(sys, "base_prefix",
                                               sys.prefix) else "no",
        # the engine stays — when a report says something failed, the first
        # question is always which engine was serving it
        "engine": getattr(config, "DEFAULT_ENGINE", "")
        or getattr(config, "BACKEND", ""),
        "provider": getattr(config, "BACKEND", ""),
        "brand": getattr(config, "BRAND_NAME", "Symbolic Synapse"),
        "model": getattr(config, "MODEL", ""),
        "ollama_model": getattr(config, "OLLAMA_MODEL", ""),
    }


def record_issue(memory, note: str, session_id: str = "",
                 engine: str = "") -> dict:
    """Snapshot a problem report and append it to problems.jsonl."""
    note = " ".join((note or "").split())[:500]
    transcript = []
    try:
        if session_id:
            transcript = [
                {"role": m["role"],
                 "text": " ".join((m["content"] or "").split())[:400]}
                for m in memory.recent_session_messages(session_id, 12)]
    except Exception:
        pass
    counts = {}
    for k, fn in (("memories", "memory_count"), ("tasks", None),
                  ("playbooks", "playbook_count"), ("lessons", "lesson_count")):
        try:
            counts[k] = (len(memory.active_tasks()) if k == "tasks"
                         else getattr(memory, fn)())
        except Exception:
            counts[k] = None
    entry = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "note": note,
        "engine": (engine or "").strip()[:60],
        "session_id": session_id or "",
        "transcript": transcript,
        "errors": [{"iso": e["iso"], "source": e["source"],
                    "message": e["message"]} for e in recent_errors(8)],
        "env": _env(),
        "counts": counts,
    }
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    return entry


def list_issues(n: int = 20) -> list[dict]:
    try:
        lines = _path().read_text("utf-8").splitlines()[-max(1, n):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return list(reversed(out))
    except Exception:
        return []


def issue_count() -> int:
    try:
        return sum(1 for ln in _path().read_text("utf-8").splitlines()
                   if ln.strip())
    except Exception:
        return 0


def clear_all() -> int:
    n = issue_count()
    try:
        with _LOCK:
            _path().unlink(missing_ok=True)
    except Exception:
        pass
    return n


def format_issue(e: dict) -> str:
    """One report as a compact text block, ready to paste to a developer."""
    lines = [f"## Issue — {e.get('iso', '')}",
             f"Report: {e.get('note', '')}",
             f"Engine: {e.get('engine') or '(unknown)'}"]
    env = e.get("env") or {}
    lines.append("Env: " + ", ".join(f"{k}={v}" for k, v in env.items() if v))
    counts = {k: v for k, v in (e.get("counts") or {}).items() if v is not None}
    if counts:
        lines.append("State: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    errs = e.get("errors") or []
    if errs:
        lines.append("Recent internal errors:")
        lines += [f"  [{x['iso']} {x['source']}] {x['message']}" for x in errs]
    tr = e.get("transcript") or []
    if tr:
        lines.append("Conversation excerpt (most recent last):")
        lines += [f"  {m['role']}: {m['text']}" for m in tr]
    return "\n".join(lines)


def export_all() -> str:
    issues = list(reversed(list_issues(100)))      # oldest first reads naturally
    if not issues:
        return "(no issues recorded)"
    brand = getattr(config, "BRAND_NAME", "Symbolic Synapse")
    header = (f"# {brand} — {getattr(config, 'AGENT_NAME', 'Agent Jo')} problem reports ({len(issues)}) — generated "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
    return header + "\n\n".join(format_issue(e) for e in issues)
