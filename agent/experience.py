"""Experience — the agent learns from its own work.

Three mechanisms, all deterministic and free (no model call needed):

  • PLAYBOOKS  — when a task completes, its steps + verification notes are
    distilled into a reusable playbook. Future related requests get the playbook
    injected: "you've done this before; here's what worked."
  • LESSONS    — when a step blocks on the user or a task fails/abandons, the
    reason is recorded. Future related requests get the lesson injected:
    "last time this needed X / broke on Y — handle that up front."
  • RECALL     — significant-word matching over past conversation messages, so
    the agent can connect today's question to something discussed weeks ago
    (injected when clearly relevant, and searchable on demand via the
    recall_conversations tool).

Distillation is mechanical on purpose: it works offline, costs nothing, is
testable, and never blocks a turn. The scoring is shared word-overlap so all
three inject only when the match is real (>= MIN_OVERLAP significant words).
"""
from __future__ import annotations

import re

MIN_OVERLAP = 2          # significant words that must match before we inject
_WORD = re.compile(r"[a-z0-9][a-z0-9\-_/.]{3,}")
_STOP = {
    "this", "that", "with", "from", "have", "will", "your", "what", "when",
    "where", "which", "then", "them", "they", "their", "there", "here",
    "please", "into", "onto", "about", "after", "before", "make", "made",
    "need", "want", "just", "like", "some", "also", "very", "over", "under",
    "each", "every", "task", "step", "steps", "done", "using", "used",
}


def sig_words(text: str) -> set:
    """Lowercased significant words (4+ chars, minus stopwords)."""
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def overlap(query_words: set, text: str) -> int:
    return len(query_words & sig_words(text))


# --------------------------------------------------------------------------- #
#  distillation (mechanical, deterministic)
# --------------------------------------------------------------------------- #
def distill_playbook(task: dict) -> dict | None:
    """Turn a finished task into a compact, reusable playbook. Only steps that
    actually happened (done/skipped/failed) contribute; verification notes ride
    along because they encode *how you know it worked*."""
    if not task:
        return None
    steps = []
    for s in task.get("steps", []):
        if s.get("status") not in ("done", "skipped", "failed"):
            continue
        line = (s.get("description") or "").strip()
        note = (s.get("note") or "").strip()
        if s.get("status") == "skipped":
            line += " (optional — was skipped)"
        elif s.get("status") == "failed":
            line += f" (failed before: {note[:80]})" if note else " (failed before)"
        elif note:
            line += f" — verify: {note[:100]}"
        steps.append(line[:220])
    if len(steps) < 2:
        return None                    # one-step tasks don't need a playbook
    title = (task.get("title") or "").strip()[:120]
    return {"title": title,
            "trigger": " ".join(sorted(sig_words(title)))[:200],
            "steps": "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))[:2000],
            "source_task": task.get("id")}


def lesson_from_blocked(task: dict, step_desc: str, needs: str) -> dict:
    ctx = (task.get("title") or "").strip()
    return {"context": ctx[:200],
            "lesson": (f"Step '{(step_desc or '').strip()[:120]}' needed the user: "
                       f"{(needs or '').strip()[:200]}. Surface this requirement "
                       f"up front next time.")[:400],
            "source": f"task#{task.get('id')}"}


def lesson_from_failed_task(task: dict, summary: str, status: str) -> dict:
    ctx = (task.get("title") or "").strip()
    return {"context": ctx[:200],
            "lesson": (f"A previous attempt ended '{status}': "
                       f"{(summary or '').strip()[:250]}")[:400],
            "source": f"task#{task.get('id')}"}


# --------------------------------------------------------------------------- #
#  hooks called from the tool layer (fail-safe: never break the tool result)
# --------------------------------------------------------------------------- #
def on_task_finished(memory, task: dict, summary: str, status: str) -> None:
    try:
        if status == "completed":
            pb = distill_playbook(task)
            if pb:
                memory.add_playbook(**pb)
        else:
            ls = lesson_from_failed_task(task, summary, status)
            memory.add_lesson(**ls)
    except Exception:
        pass


def on_step_blocked(memory, task: dict, step_desc: str, needs: str) -> None:
    try:
        memory.add_lesson(**lesson_from_blocked(task, step_desc, needs))
    except Exception:
        pass
