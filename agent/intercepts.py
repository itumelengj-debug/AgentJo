"""Intercepts — seeing a tool call before it changes anything outside.

Full access and unattended autonomy are both "go ahead without asking", and
that is usually what you want. But the two are not the same risk: a command
that reads a file and a command that sends an email both arrive as a tool
call, and only one of them is retractable.

So calls are sorted by what they would change:

  Reads and local work run. Listing a directory or searching memory does not
  need a person, and asking about them teaches you to click through prompts
  without reading — which is worse than not asking at all.

  Anything that reaches OUTSIDE — sends mail, submits a form, posts to an
  API, deletes beyond the workspace, spends money — is held with its exact
  inputs shown, and waits.

The inputs matter more than the tool name. "send_email" tells you nothing;
"send_email to hiring@acme.com, subject 'Application — Senior Data Engineer',
1.4 KB" tells you whether it is right. What gets shown here is exactly what
would be sent, not a summary of it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Tools whose effects leave this machine or cannot be undone. Everything not
# listed is treated as safe to run, so a new READING tool is never blocked by
# omission — but a new SENDING tool must be added here, which is why the list
# is short, explicit, and sits next to the reason it exists.
EXTERNAL = {
    "send_email": "sends mail to someone outside",
    "send_message": "sends a message outside",
    "outreach_send": "sends a campaign",
    "apply_on_portal": "submits an application form",
    "submit_form": "submits a form on a website",
    "http_post": "posts data to an external service",
    "mcp_call": "acts through an external tool server",
    "delete_file": "deletes something",
    "job_apply": "sends a job application",
}

# `run_command` is NOT here. It already has a permission system of its own
# that asks, remembers your answer per command, and distinguishes read-only
# from destructive. Adding a second gate in front of it meant two mechanisms
# guarding one thing — the first one asked, the second held the answer
# hostage, and the shell stopped working. One gate per action.
OWNED_ELSEWHERE = {"run_command": "the command permission system"}

# Commands that only read, even though run_command is on the list above.
_READ_ONLY_CMD = ("dir", "ls", "type", "cat", "findstr", "grep", "where",
                  "echo", "python -c \"print", "git status", "git log",
                  "git diff", "head", "tail", "wc", "stat")


def _dir() -> Path:
    d = config.AGENT_HOME / "intercepts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return _dir() / "pending.json"


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def enabled() -> bool:
    return bool(getattr(config, "INTERCEPTS", True))


def classify(tool: str, args: dict) -> dict:
    """Would this change something outside, and what exactly?"""
    tool = (tool or "").strip()
    if tool in OWNED_ELSEWHERE:
        return {"hold": False,
                "why": f"guarded by {OWNED_ELSEWHERE[tool]} already"}
    reason = EXTERNAL.get(tool)
    if not reason:
        return {"hold": False, "why": "reads or works locally"}
    return {"hold": True, "why": reason}


def summarise(tool: str, args: dict) -> str:
    """The line you actually judge it by.

    A tool name is not enough to decide on: what matters is the recipient,
    the subject, the path, the command."""
    a = args or {}
    if tool in ("send_email", "send_message", "job_apply"):
        body = str(a.get("body") or a.get("content") or "")
        return (f"to {a.get('to', '(no address)')} · "
                f"“{str(a.get('subject', '(no subject)'))[:60]}” · "
                f"{len(body)} characters")
    if tool == "run_command":
        return str(a.get("command", ""))[:300]
    if tool in ("apply_on_portal", "submit_form"):
        return f"{a.get('url', '(no url)')} — {len(a)} field(s)"
    if tool == "delete_file":
        return str(a.get("path", ""))[:200]
    if tool == "http_post":
        return f"{a.get('url', '')} · {len(json.dumps(a.get('json', {})))} bytes"
    return json.dumps(a, default=str)[:280]


def pending() -> list:
    try:
        data = json.loads(_path().read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    _path().write_text(json.dumps(items, indent=2, default=str), "utf-8")


def hold(tool: str, args: dict, context: str = "",
         source: str = "chat") -> dict:
    """Queue a call for review. Returns the record, including its id."""
    item = {
        "id": uuid.uuid4().hex[:10],
        "at": _iso(), "tool": tool, "args": args or {},
        "summary": summarise(tool, args),
        "why_held": classify(tool, args)["why"],
        "context": (context or "")[:300],
        "source": source,          # chat, schedule, crew, jobscout…
        "state": "waiting",
    }
    _save(pending() + [item])
    _audit("held", f"{tool}: {item['summary'][:120]}")
    return item


def decide(intercept_id: str, approve: bool, note: str = "") -> dict:
    items = pending()
    hit = next((i for i in items if i["id"] == intercept_id), None)
    if hit is None:
        return {"ok": False, "error": "no such intercept"}
    if hit["state"] != "waiting":
        return {"ok": False,
                "error": f"already {hit['state']} — nothing decided twice"}
    hit["state"] = "approved" if approve else "refused"
    hit["decided_at"] = _iso()
    hit["note"] = (note or "")[:300]
    _save(items)
    _audit("approved" if approve else "refused",
           f"{hit['tool']}: {hit['summary'][:120]}")
    return {"ok": True, **hit}


def state_of(intercept_id: str) -> str:
    hit = next((i for i in pending() if i["id"] == intercept_id), None)
    return hit["state"] if hit else "gone"


def clear_decided(keep: int = 40) -> dict:
    """Keep the record, bounded. A decision you can't look back at is no
    record at all."""
    items = pending()
    waiting = [i for i in items if i["state"] == "waiting"]
    decided = [i for i in items if i["state"] != "waiting"][-keep:]
    _save(waiting + decided)
    return {"ok": True, "waiting": len(waiting), "kept": len(decided)}


def summary() -> dict:
    items = pending()
    waiting = [i for i in items if i["state"] == "waiting"]
    return {"waiting": len(waiting), "items": items[::-1][:60],
            "enabled": enabled(),
            "note": ("Reads and local work run without asking. Anything that "
                     "leaves this machine waits here with its exact inputs.")}


def _audit(name: str, summary_text: str) -> None:
    try:
        from . import audit
        audit.record("intercept", name=name, summary=summary_text[:200])
    except Exception:
        pass
