"""Skills — running the ones you've adopted, on purpose.

Adopted skills already reach the model: every one is pasted into the system
prompt on every turn under "Skills the user has taught you". That works, but
it is entirely passive. You cannot see what you have, you cannot ask for one
by name, and nothing records whether a skill has ever actually been used —
the `times_used` column has existed since the beginning and has never been
incremented once.

So a skill adopted from a trend, or taught six weeks ago, sits in the prompt
forever with no evidence it earns its place.

This gives them a front door:

  • list them with their usage, so dead weight is visible
  • run one deliberately, with your own input, and see the result attributed
  • count each run, so "which of these do I actually use" has an answer

Running a skill is not a new kind of execution — it's an ordinary turn with
the skill's steps put first and the model told to follow them. That keeps
tools, permissions, memory and the audit trail exactly as they are for any
other turn, rather than inventing a second path with its own holes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]


def _meta_path() -> Path:
    d = config.AGENT_HOME
    d.mkdir(parents=True, exist_ok=True)
    return d / "skill_usage.json"


def _meta() -> dict:
    try:
        return json.loads(_meta_path().read_text("utf-8"))
    except Exception:
        return {}


def _save_meta(m: dict) -> None:
    try:
        _meta_path().write_text(json.dumps(m), "utf-8")
    except Exception:
        pass


def listing(memory) -> list:
    """Every skill, with enough context to judge whether to keep it."""
    meta = _meta()
    out = []
    for s in memory.get_skills():
        slug = _slug(s["name"])
        m = meta.get(slug) or {}
        out.append({
            "name": s["name"],
            "slug": slug,
            "description": s.get("description") or "",
            "instructions": s.get("instructions") or "",
            "steps": _steps(s.get("instructions") or ""),
            "times_used": int(s.get("times_used") or 0) + int(
                m.get("runs") or 0),
            "last_used": m.get("last_used", ""),
            "created_at": s.get("created_at", ""),
        })
    out.sort(key=lambda x: (-x["times_used"], x["name"]))
    return out


def _steps(instructions: str) -> list:
    """Split numbered or bulleted instructions so the panel can show what a
    skill will actually do before you run it."""
    text = (instructions or "").strip()
    if not text:
        return []
    parts = re.split(r"(?:\n|^)\s*(?:\d+[.)]\s*|[-*•]\s+)", text)
    steps = [p.strip() for p in parts if p.strip()]
    return steps[:12] if len(steps) > 1 else []


def find(memory, name: str):
    want = _slug(name)
    for s in listing(memory):
        if s["slug"] == want or s["name"].lower() == (name or "").lower():
            return s
    return None


RUN_HEADER = (
    "Follow this saved skill exactly. It was written for this kind of "
    "request, so prefer its steps over your own approach; if a step genuinely "
    "cannot apply here, say which and why rather than quietly skipping it.\n\n"
    "SKILL: {name}\n"
    "WHEN TO USE: {description}\n"
    "STEPS:\n{instructions}\n")


def build_run_prompt(skill: dict, user_input: str = "") -> str:
    """An ordinary turn, with the skill's steps in front of it."""
    body = RUN_HEADER.format(name=skill["name"],
                             description=skill.get("description") or "—",
                             instructions=skill.get("instructions") or "—")
    task = (user_input or "").strip()
    if task:
        return body + "\nAPPLY IT TO:\n" + task
    return (body + "\nThe user ran this skill without extra input. If the "
                   "skill needs something specific to work on, ask for that "
                   "one thing rather than guessing.")


def mark_used(memory, name: str) -> dict:
    """Record that a skill actually ran. Kept in a side file as well as the
    column so an older database without the column still counts."""
    slug = _slug(name)
    meta = _meta()
    entry = meta.setdefault(slug, {"runs": 0})
    entry["runs"] = int(entry.get("runs") or 0) + 1
    entry["last_used"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")
    _save_meta(meta)
    try:
        memory.conn.execute(
            "UPDATE skills SET times_used = COALESCE(times_used, 0) + 1 "
            "WHERE name = ?", (slug,))
        memory.conn.commit()
    except Exception:
        pass
    try:
        from . import audit
        audit.record("skill", name=name, summary="ran on request")
    except Exception:
        pass
    return entry


def unused(memory, min_age_days: int = 14) -> list:
    """Skills that have never run. Not a judgement — some are situational —
    but worth being able to see."""
    return [s for s in listing(memory) if not s["times_used"]]
