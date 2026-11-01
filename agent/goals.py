"""Goals — outcomes it works toward over days, not instructions it runs once.

Everything autonomous here used to be shaped like a chat turn. A schedule
re-ran the same instruction; a task plan was a list of steps written in one
turn; auto-resume nudged a stalled plan. None of it held an *outcome* — "five
interviews by the end of the month", "the nightly build green again" — and
asked, run after run, whether the work was getting any closer.

A goal does. It carries:

  objective        what you want, in your words
  success          how you'd know it's done; the agent may only claim
                   'achieved' with evidence against this
  deadline         optional; past it, the goal stops and tells you
  budget_usd       optional; the goal's own spending ceiling, separate from
                   the monthly one. When it's spent, the goal stops.
  runs_per_day     how often it may work on this (default 4)

A managed schedule ("Pursue goals") sweeps the active goals. Each run gets a
longer budget than a chat turn (GOAL_MAX_ROUNDS tool calls, GOAL_TIME_LIMIT
seconds), the goal's history so far, and the state of the task plans it
made. It ends by writing a CHECKPOINT with update_goal: what's done, what's
next. The next run starts from that checkpoint. A run cut off by a limit
writes one automatically, so long work is handed on rather than lost.

The fence, the same shape as auto-resume's:

  • a run that records no progress is a stall; three in a row and the goal
    stops as 'stalled' and tells you, instead of spending forever
  • 'achieved' needs evidence; 'blocked' needs exactly what you must do
  • the goal's budget and deadline are checked before every run
  • anything leaving the machine still waits for you (agent/intercepts.py)
  • every run and every status change is audited, and the ones that need
    you are sent to your phone (agent/notify.py)

Storage is one JSON file. The sweep takes an injected runner, so the
orchestration is testable without a model.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

STATUSES = ("active", "paused", "achieved", "blocked", "stalled",
            "abandoned", "expired", "budget")
OPEN = ("active",)
GOAL_MAX_ROUNDS = int(os.environ.get("AGENT_GOAL_MAX_ROUNDS", "40"))
GOAL_TIME_LIMIT = int(os.environ.get("AGENT_GOAL_TIME_LIMIT", "1200"))
STALL_LIMIT = 3
MAX_PER_SWEEP = 2
MAX_NOTES = 60
_LOCK = threading.RLock()


def _path() -> Path:
    return Path(config.AGENT_HOME) / "goals.json"


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _load() -> dict:
    try:
        d = json.loads(_path().read_text("utf-8"))
        if isinstance(d, dict) and isinstance(d.get("goals"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"next_id": 1, "goals": []}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), "utf-8")
    tmp.replace(p)


def _audit(name: str, **fields) -> None:
    try:
        from . import audit
        audit.record("goal", name=name, **fields)
    except Exception:
        pass


def _notify(goal: dict, what: str, detail: str = "") -> None:
    try:
        from . import notify
        notify.goal_event(goal, what, detail)
    except Exception:
        pass


def _deadline_ts(deadline: str) -> float | None:
    """'2026-11-30' means the end of that day, local time."""
    if not deadline:
        return None
    try:
        d = datetime.strptime(deadline.strip()[:10], "%Y-%m-%d")
        return d.replace(hour=23, minute=59, second=59).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def create(title: str, objective: str, success: str, *, deadline: str = "",
           budget_usd: float = 0.0, runs_per_day: int = 4,
           full_access: bool = False, engine: str = "Auto", checks=None) -> dict:
    from . import checks as _checks
    title = (title or "").strip()[:120]
    cs = _checks.normalise(checks)
    bad = [_checks.problem(c) for c in cs if _checks.problem(c)]
    if bad:
        raise ValueError(f"a check can't run: {bad[0]}")
    objective = (objective or "").strip()[:2000]
    success = (success or "").strip()[:1000]
    if not title or not objective:
        raise ValueError("a goal needs a title and an objective")
    if not success:
        raise ValueError("say how you'd know it's done (success criteria)")
    if deadline and _deadline_ts(deadline) is None:
        raise ValueError("deadline must look like 2026-11-30")
    with _LOCK:
        d = _load()
        g = {"id": d["next_id"], "title": title, "objective": objective,
             "success": success, "deadline": (deadline or "")[:10],
             "budget_usd": max(0.0, float(budget_usd or 0)), "spent_usd": 0.0,
             "runs_per_day": max(1, min(48, int(runs_per_day or 4))),
             "full_access": bool(full_access), "engine": engine or "Auto",
             "status": "active", "stalls": 0, "task_ids": [],
             "notes": [], "runs": [], "created_at": _iso(), "last_run": 0.0,
             "checks": cs}
        d["next_id"] += 1
        d["goals"].append(g)
        _save(d)
    _audit("created", goal=g["id"], title=title)
    return g


def get(goal_id: int) -> dict | None:
    return next((g for g in _load()["goals"] if g["id"] == int(goal_id)), None)


def list_goals(status: str = "all") -> list[dict]:
    gs = _load()["goals"]
    if status and status != "all":
        gs = [g for g in gs if g["status"] == status]
    return gs


def _mutate(goal_id: int, fn) -> dict | None:
    with _LOCK:
        d = _load()
        g = next((x for x in d["goals"] if x["id"] == int(goal_id)), None)
        if g is None:
            return None
        fn(g)
        g["notes"] = g["notes"][-MAX_NOTES:]
        g["runs"] = g["runs"][-MAX_NOTES:]
        _save(d)
        return g


def note(goal_id: int, kind: str, text: str) -> dict | None:
    return _mutate(goal_id, lambda g: g["notes"].append(
        {"at": _iso(), "ts": time.time(), "kind": kind, "text": (text or "")[:1500]}))


def set_status(goal_id: int, status: str, why: str = "") -> dict | None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")

    def _f(g):
        g["status"] = status
        if status == "active":
            g["stalls"] = 0
        if why:
            g["notes"].append({"at": _iso(), "ts": time.time(), "kind": status,
                               "text": why[:1500]})
    g = _mutate(goal_id, _f)
    if g:
        _audit("status", goal=g["id"], status=status, why=why[:200])
    return g


def delete(goal_id: int) -> bool:
    with _LOCK:
        d = _load()
        n = len(d["goals"])
        d["goals"] = [g for g in d["goals"] if g["id"] != int(goal_id)]
        _save(d)
        return len(d["goals"]) < n


# --------------------------------------------------------------------------- #
# What the agent calls (the update_goal tool)
# --------------------------------------------------------------------------- #
def record_update(goal_id: int, status: str, note_text: str, *,
                  evidence: str = "", needs: str = "", task_id: int | None = None,
                  checks=None, memory=None, auto_approve: bool = False) -> str:
    g = get(goal_id)
    if g is None:
        return "Error: no such goal. Use list_goals to check ids."
    if g["status"] not in OPEN:
        return f"Refused: goal #{goal_id} is {g['status']}, not active."
    note_text = (note_text or "").strip()
    if not note_text:
        return "Refused: say what was done and what comes next in 'note'."
    if task_id:
        _mutate(goal_id, lambda x: x["task_ids"].append(int(task_id))
                if int(task_id) not in x["task_ids"] else None)
    if status == "progressing":
        note(goal_id, "checkpoint", note_text)
        _mutate(goal_id, lambda x: x.update(stalls=0))
        return (f"Checkpoint saved for goal #{goal_id}. The next run starts from it.")
    if status == "achieved":
        from . import checks as _checks
        new = _checks.normalise(checks)
        bad = [_checks.problem(c) for c in new if _checks.problem(c)]
        if bad:
            return f"Error: a check can't run: {bad[0]}."
        all_checks = (_checks.normalise(g.get("checks")) + new)[:_checks.MAX_CHECKS]
        if all_checks:
            # the goal's own checks decide, not the wording of the evidence
            res = _checks.run_all(all_checks, memory=memory, auto_approve=auto_approve)
            if not res["ok"]:
                note(goal_id, "checkpoint", f"Claimed achieved, but the app's checks failed: {res['text']}")
                return ("Refused: the app checked and the goal isn't met: " + res["text"]
                        + ". Keep working (record a 'progressing' checkpoint), or 'blocked' if "
                        "it's waiting on the user.")
            evidence = (evidence + "\n" if evidence else "") + "Checked by the app: " + res["text"]
        elif len((evidence or "").strip()) < 20:
            return ("Refused: 'achieved' needs 'evidence' — what you checked, "
                    "criterion by criterion, against: " + g["success"]
                    + ". Better: pass 'checks' the app can run itself.")
        g = set_status(goal_id, "achieved", f"{note_text}\nEvidence: {evidence}")
        _notify(g, "achieved", evidence[:500])
        return f"Goal #{goal_id} marked achieved."
    if status == "blocked":
        if not (needs or "").strip():
            return "Refused: 'blocked' needs 'needs' — exactly what the user must do."
        g = set_status(goal_id, "blocked", f"{note_text}\nNEEDS USER: {needs}")
        _notify(g, "blocked", f"Needs: {needs[:400]}")
        return (f"Goal #{goal_id} is paused until the user acts: {needs}. "
                f"Tell them clearly, then stop.")
    if status == "abandoned":
        g = set_status(goal_id, "abandoned", note_text)
        _notify(g, "given up", note_text[:400])
        return f"Goal #{goal_id} abandoned."
    return "Error: status must be progressing, achieved, blocked or abandoned."


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def runs_today(g: dict, now: float | None = None) -> int:
    now = now or time.time()
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    return sum(1 for r in g.get("runs", [])
               if datetime.fromtimestamp(r.get("ts", 0)).strftime("%Y-%m-%d") == day)


def is_due(g: dict, now: float | None = None) -> bool:
    now = now or time.time()
    if g["status"] not in OPEN:
        return False
    if runs_today(g, now) >= g["runs_per_day"]:
        return False
    gap = 86400 / max(1, g["runs_per_day"])
    return now - float(g.get("last_run") or 0) >= gap * 0.9


def _fenced(g: dict, now: float) -> str:
    """The reason a goal must stop before running, or ''."""
    dl = _deadline_ts(g.get("deadline", ""))
    if dl and now > dl:
        return "expired"
    if g.get("budget_usd") and g.get("spent_usd", 0) >= g["budget_usd"]:
        return "budget"
    return ""


def build_prompt(g: dict, memory=None) -> str:
    lines = [
        f"[goal #{g['id']}] You are working, unattended, toward a goal the user set.",
        "",
        f"GOAL: {g['title']}",
        f"Objective: {g['objective']}",
        f"Done when: {g['success']}",
    ]
    if g.get("checks"):
        from . import checks as _checks
        lines.append("Before 'achieved' is accepted, the app itself checks: "
                     + "; ".join(_checks.describe(c) for c in g["checks"]))
    if g.get("deadline"):
        lines.append(f"Deadline: {g['deadline']}")
    if g.get("budget_usd"):
        lines.append(f"Budget left: ${max(0.0, g['budget_usd'] - g['spent_usd']):.2f} "
                     f"of ${g['budget_usd']:.2f}")
    notes = [n for n in g.get("notes", [])][-8:]
    if notes:
        lines += ["", "Where things stand (oldest first; the last checkpoint is where you resume):"]
        lines += [f"- {n['at']} [{n['kind']}] {n['text'][:600]}" for n in notes]
    else:
        lines += ["", "This is the first run on this goal. Start by making a task plan "
                  "(create_task_plan) for the next concrete steps."]
    if memory is not None and g.get("task_ids"):
        lines += ["", "Task plans made for this goal:"]
        for tid in g["task_ids"][-5:]:
            try:
                t = memory.get_task(int(tid))
            except Exception:
                t = None
            if t:
                done = sum(1 for s in t["steps"] if s["status"] == "done")
                lines.append(f"- task #{t['id']} '{t['title']}' — {t['status']}, "
                             f"{done}/{len(t['steps'])} steps done")
    lines += [
        "",
        "How to work:",
        "1. Pick up from the last checkpoint. Do real work toward the goal with your tools; "
        "make or advance task plans (pass task_id to update_goal to link a plan).",
        "2. Before you finish this run, ALWAYS call update_goal:",
        "   - status 'progressing' with a note saying what you did and what the next run should do;",
        "   - 'achieved' ONLY with evidence checked against each 'done when' criterion;",
        "   - 'blocked' with 'needs' if it can't continue without the user;",
        "   - 'abandoned' if the goal is impossible as stated, saying why.",
        "3. Anything that leaves this machine (email, posts, forms) will wait for the user's "
        "approval — queue it and carry on with what doesn't depend on it.",
        f"You have up to {GOAL_MAX_ROUNDS} tool calls and {GOAL_TIME_LIMIT // 60} minutes this run.",
    ]
    return "\n".join(lines)


def run_one(g: dict, runner, memory=None, cost_fn=None, now: float | None = None) -> dict:
    """One run on one goal. runner(goal, prompt) -> (reply, info)."""
    now = now or time.time()
    why = _fenced(g, now)
    if why:
        msg = ("The deadline passed." if why == "expired"
               else f"Its budget of ${g['budget_usd']:.2f} is spent.")
        g2 = set_status(g["id"], why, msg)
        _notify(g2 or g, "stopped", msg)
        return {"goal": g["id"], "ran": False, "status": why}
    before = len([n for n in g.get("notes", []) if n["kind"] in ("checkpoint",) + STATUSES])
    _mutate(g["id"], lambda x: x.update(last_run=now))
    prompt = build_prompt(g, memory)
    t0 = time.time()
    try:
        reply, info = runner(g, prompt)
        err = ""
    except Exception as exc:
        reply, info, err = "", {}, f"{type(exc).__name__}: {exc}"[:300]
    info = info or {}
    cost = 0.0
    if cost_fn:
        try:
            cost = float(cost_fn(info) or 0.0)
        except Exception:
            cost = 0.0
    stop = info.get("stop_reason") or ("error" if err else "")
    after_g = get(g["id"]) or g
    after = len([n for n in after_g.get("notes", []) if n["kind"] in ("checkpoint",) + STATUSES])
    progressed = after > before

    def _f(x):
        x["spent_usd"] = round(float(x.get("spent_usd", 0)) + cost, 6)
        x["runs"].append({"ts": now, "at": _iso(now), "secs": round(time.time() - t0, 1),
                          "usd": round(cost, 6), "stop": stop, "progressed": progressed,
                          "error": err})
        if x["status"] in OPEN:
            if stop in ("round_limit", "timeout"):
                # handed on, not lost: the next run resumes from here
                x["notes"].append({"at": _iso(), "ts": time.time(), "kind": "checkpoint",
                                   "text": f"(automatic) This run hit its {stop.replace('_', ' ')} "
                                           f"before finishing. Last words: "
                                           f"{' '.join((reply or '').split())[-500:]}"})
            if progressed:
                x["stalls"] = 0
            else:
                x["stalls"] = int(x.get("stalls", 0)) + 1
    g = _mutate(g["id"], _f) or g
    _audit("run", goal=g["id"], usd=round(cost, 4), stop=stop or "ok",
           progressed=progressed, error=err[:120])
    if g["status"] in OPEN and g.get("budget_usd") and g["spent_usd"] >= g["budget_usd"]:
        set_status(g["id"], "budget", f"Spent ${g['spent_usd']:.2f} of ${g['budget_usd']:.2f}.")
        _notify(g, "out of budget", f"Spent ${g['spent_usd']:.2f} of ${g['budget_usd']:.2f}.")
    elif g["status"] in OPEN and g["stalls"] >= STALL_LIMIT:
        msg = (f"{g['stalls']} runs in a row recorded no progress"
               + (f" (last error: {err})" if err else "") + ". Stopped so it doesn't keep spending.")
        set_status(g["id"], "stalled", msg)
        _notify(g, "stalled", msg)
    return {"goal": g["id"], "ran": True, "progressed": progressed, "usd": cost,
            "stop": stop, "error": err}


def sweep(runner, memory=None, cost_fn=None, now: float | None = None,
          max_per_sweep: int = MAX_PER_SWEEP) -> dict:
    now = now or time.time()
    out = []
    # fences first, for every open goal, so an expired one stops even when
    # it isn't due to run
    for g in list_goals("active"):
        if _fenced(g, now):
            out.append(run_one(g, runner, memory, cost_fn, now))
    due = sorted((g for g in list_goals("active") if is_due(g, now)),
                 key=lambda g: g.get("last_run") or 0)[:max_per_sweep]
    for g in due:
        out.append(run_one(g, runner, memory, cost_fn, now))
    return {"ok": True, "ran": sum(1 for r in out if r.get("ran")),
            "progressed": sum(1 for r in out if r.get("progressed")), "results": out}


def summary_line(g: dict) -> str:
    bits = [f"#{g['id']} {g['title']} — {g['status']}"]
    if g.get("deadline"):
        bits.append(f"due {g['deadline']}")
    if g.get("budget_usd"):
        bits.append(f"${g['spent_usd']:.2f}/${g['budget_usd']:.2f}")
    bits.append(f"{len(g.get('runs', []))} run(s)")
    last = next((n for n in reversed(g.get("notes", [])) if n["kind"] == "checkpoint"), None)
    s = " · ".join(bits)
    if last:
        s += f"\n  last checkpoint: {last['text'][:300]}"
    return s


SCHEDULE_NAME = "Pursue goals"


def ensure_schedule(memory) -> int | None:
    """One managed hourly schedule drives every goal (each goal's own
    runs_per_day decides whether a given sweep touches it). Turning that
    schedule off pauses all goals at once."""
    try:
        for s in memory.list_schedules():
            if s.get("action") == "goals":
                return s["id"]
        from . import scheduler
        spec = scheduler.make_spec("hourly", time_str="07:00", n=60, dow=0)
        sid = memory.create_schedule(SCHEDULE_NAME, "Work toward active goals within their fences",
                                     spec, "Auto", False,
                                     scheduler.next_run(scheduler.parse_spec(spec)),
                                     action="goals", payload="{}")
        _audit("schedule", schedule=sid)
        return sid
    except Exception:
        return None
