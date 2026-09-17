"""Auto-resume — a bounded sweep that nudges *stuck* tasks forward on its own.

This is the most autonomous piece in Agent Jo, so it's fenced hard against the
two ways it could go wrong: looping forever on a task it can't finish, and
fighting with work you're actively doing. The fence:

  • OFF until you arm it (separate switch), with its own kill switch.
  • Only touches tasks idle for >= idle_minutes (won't barge into live work).
  • Per-task ATTEMPT CAP: if a resume makes no progress, the task's counter
    ticks up; after max_attempts with no progress it is left alone. Progress
    (a step resolved or correctly marked blocked) RESETS the counter, so a task
    that keeps advancing keeps getting help, but a true stall stops quickly.
  • Skips tasks whose only remaining work is BLOCKED on you (those need a human,
    not another agent pass).
  • max_per_sweep caps how many tasks one sweep touches.
  • Sending stays behind the outreach auto-pilot fence (allowlist + arm), so a
    resumed task can't quietly email anyone you didn't approve.
  • Every attempt is written to an audit log.

The sweep takes an injected `runner(task)->reply` callback, so the orchestration
(candidate selection, attempt accounting, progress detection) is testable without
a live model.
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import config

_LOCK = threading.Lock()
_DEFAULTS = {"enabled": False, "idle_minutes": 30, "max_attempts": 3,
             "max_per_sweep": 3, "full_access": False, "cadence": "hourly",
             "schedule_id": None}


def _cfg_path():
    return config.AGENT_HOME / "autoresume.json"


def _state_path():
    return config.AGENT_HOME / "autoresume_state.json"


def _log_path():
    return config.AGENT_HOME / "autoresume_log.jsonl"


# --------------------------------------------------------------------------- #
#  config + per-task state
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        cfg.update(json.loads(_cfg_path().read_text("utf-8")))
    except Exception:
        pass
    return cfg


def save_config(data: dict) -> dict:
    cfg = load_config()
    for k in ("enabled", "full_access"):
        if k in data:
            cfg[k] = bool(data[k])
    if "idle_minutes" in data:
        cfg["idle_minutes"] = max(1, min(1440, int(data["idle_minutes"] or 30)))
    if "max_attempts" in data:
        cfg["max_attempts"] = max(1, min(10, int(data["max_attempts"] or 3)))
    if "max_per_sweep" in data:
        cfg["max_per_sweep"] = max(1, min(10, int(data["max_per_sweep"] or 3)))
    if "cadence" in data and data["cadence"]:
        cfg["cadence"] = str(data["cadence"])
    if "schedule_id" in data:
        cfg["schedule_id"] = data["schedule_id"]
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), "utf-8")
    return status()


def status() -> dict:
    c = load_config()
    return {k: c.get(k) for k in
            ("enabled", "idle_minutes", "max_attempts", "max_per_sweep",
             "full_access", "cadence", "schedule_id")}


def pause() -> dict:
    """Kill switch: disarm auto-resume."""
    save_config({"enabled": False})
    _audit({"event": "paused"})
    return status()


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text("utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            p.write_text(json.dumps(state), "utf-8")
    except Exception:
        pass


def reset_task_state(task_id: int) -> None:
    """Forget a task's attempt history (call when the user touches the task)."""
    st = _load_state()
    if str(task_id) in st:
        st.pop(str(task_id), None)
        _save_state(st)


def _audit(entry: dict) -> None:
    try:
        entry = dict(entry)
        entry["ts"] = time.time()
        with _LOCK, open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def recent_log(n: int = 50) -> list:
    try:
        lines = _log_path().read_text("utf-8").splitlines()[-max(1, n):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return list(reversed(out))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
#  progress accounting
# --------------------------------------------------------------------------- #
def _advanced(task: dict) -> int:
    """How many steps have moved out of the unstarted state (done/skipped/failed/
    blocked). Used to tell whether a resume actually moved the needle."""
    return sum(1 for s in task["steps"]
               if s["status"] not in ("pending", "in_progress"))


def candidates(memory) -> list[dict]:
    """Eligible stuck tasks: idle long enough, still have an ACTIONABLE next step
    (not only user-blocked work), and not past the no-progress attempt cap."""
    cfg = load_config()
    st = _load_state()
    out = []
    for t in memory.stuck_tasks(idle_minutes=cfg["idle_minutes"]):
        if not t.get("next_step"):
            continue                       # only blocked/awaiting-user work left
        attempts = int((st.get(str(t["id"])) or {}).get("attempts", 0))
        if attempts >= cfg["max_attempts"]:
            continue                       # gave up after repeated no-progress
        out.append(t)
    return out


def build_prompt(task) -> str:
    from .memory import format_task
    nxt = task.get("next_step") or {}
    return (
        f"Auto-resume: this task stalled and you're picking it back up.\n\n"
        f"{format_task(task)}\n\n"
        f"Continue from step {nxt.get('seq', '?')}: {nxt.get('description', '')}. "
        f"Do the next concrete action now and verify it before marking it done. "
        f"If a step truly needs the user (a login, an install, a credential, a "
        f"decision), mark it blocked with the exact handoff and stop — don't spin. "
        f"When everything real is verified, complete the task."
    )


def sweep(memory, runner) -> dict:
    """Resume up to max_per_sweep stuck tasks. `runner(task) -> reply` performs the
    actual agent turn. Returns a report. Honours the arm switch and attempt cap."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "auto-resume is disarmed", "resumed": 0,
                "progressed": 0, "stalled": 0, "results": []}
    picked = candidates(memory)[:cfg["max_per_sweep"]]
    st = _load_state()
    resumed = progressed = stalled = 0
    results = []
    for t in picked:
        tid = str(t["id"])
        before = _advanced(t)
        try:
            runner(t)
        except Exception as exc:
            _audit({"event": "error", "task": t["id"], "error": f"{type(exc).__name__}: {exc}"})
            results.append({"task": t["id"], "outcome": "error"})
            continue
        resumed += 1
        after_task = memory.get_task(t["id"])
        after = _advanced(after_task) if after_task else before
        done = bool(after_task) and after_task["status"] != "active"
        rec = st.get(tid) or {"attempts": 0}
        if after > before or done:
            rec["attempts"] = 0            # progress -> keep helping next time
            progressed += 1
            outcome = "completed" if done else "progressed"
        else:
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            stalled += 1
            outcome = "stalled"
            if rec["attempts"] >= cfg["max_attempts"]:
                outcome = "gave_up"
        rec["last_ts"] = time.time()
        rec["last_advanced"] = after
        st[tid] = rec
        _audit({"event": outcome, "task": t["id"], "title": t.get("title", ""),
                "attempts": rec["attempts"]})
        results.append({"task": t["id"], "title": t.get("title", ""), "outcome": outcome})
    _save_state(st)
    return {"ok": True, "resumed": resumed, "progressed": progressed,
            "stalled": stalled, "candidates": len(picked), "results": results}
