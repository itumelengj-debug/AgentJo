"""Collaboration — Solver + Reviewer across the local/cloud divide.

Opt-in "second opinion" for a single turn: after the solver produces an answer,
a reviewer running on the OPPOSITE tier critiques it, and the solver gets one
chance to revise. The point of crossing tiers is independence — a local model
checking a cloud answer (free), or a cloud model checking a local answer
(higher quality) — so the review is a genuine second perspective, not the same
model agreeing with itself.

This module is pure orchestration: it's handed two callables,

    solver(prompt) -> str      # produce / revise an answer
    reviewer(prompt) -> str    # critique an answer

so it's fully testable with fakes and knows nothing about engines, HTTP, or the
UI. run_turn wires the real model calls in.

Flow:
  1. solver answers (this already happened — we're passed the draft)
  2. reviewer critiques on the opposite tier
  3. if the reviewer is satisfied, keep the draft (cheap path, no rewrite)
  4. otherwise the solver revises ONCE, given the critique
"""
from __future__ import annotations

# A reviewer signals "no changes needed" by leading with this token, so we can
# skip a pointless revision (and its cost) when the draft already holds up.
_OK_TOKEN = "LGTM"

REVIEW_SYSTEM = (
    "You are a meticulous reviewer on a different model than the one that wrote "
    "the draft below. Your job is a genuine second opinion, not a rubber stamp. "
    "Check the draft for: factual or logical errors, missed parts of the "
    "request, unsafe or incorrect instructions, and unsupported claims. Be "
    "specific and brief.\n"
    f"If the draft is correct and complete, reply with exactly '{_OK_TOKEN}' and "
    "nothing else. Otherwise, list only the concrete problems and what to fix — "
    "do NOT rewrite the answer yourself."
)


def build_review_prompt(user_input: str, draft: str) -> str:
    return (f"The user asked:\n{user_input}\n\n"
            f"Another model drafted this answer:\n---\n{draft}\n---\n\n"
            f"Review it per your instructions.")


def build_revision_prompt(user_input: str, draft: str, critique: str) -> str:
    return (f"A reviewer found issues with your previous answer. Produce an "
            f"improved final answer that fixes them. Keep what was already "
            f"correct; don't mention the review process.\n\n"
            f"Original request:\n{user_input}\n\n"
            f"Your previous answer:\n---\n{draft}\n---\n\n"
            f"Reviewer's findings:\n---\n{critique}\n---\n\n"
            f"Improved final answer:")


def review_satisfied(critique: str) -> bool:
    """True when the reviewer approved with no substantive changes."""
    c = (critique or "").strip()
    if not c:
        return True                    # empty critique = nothing to fix
    head = c.split("\n", 1)[0].strip().upper().strip(".!*_ ")
    return head.startswith(_OK_TOKEN)


def collaborate(user_input: str, draft: str, reviewer, solver) -> dict:
    """Run the review (and one revision if needed). Returns a report dict:
        {answer, revised, critique, approved}
    Fail-safe: any exception falls back to the original draft, so enabling the
    second opinion can never make a turn worse than not having it."""
    result = {"answer": draft, "revised": False, "critique": "",
              "approved": True}
    try:
        critique = reviewer(build_review_prompt(user_input, draft)) or ""
    except Exception:
        return result                  # reviewer unavailable -> keep the draft
    result["critique"] = critique.strip()
    if review_satisfied(critique):
        record("approved")
        return result                  # draft holds up; no rewrite, no extra cost
    substantive, why = critique_is_substantive(critique)
    if not substantive:
        # a rewrite costs a full generation; style notes don't justify one
        result["skipped_reason"] = why
        record("nitpick_ignored")
        return result
    result["approved"] = False
    try:
        improved = solver(build_revision_prompt(user_input, draft, critique))
    except Exception:
        return result                  # revision failed -> keep the draft
    ok, why = accept_revision(draft, improved)
    if not ok:
        result["skipped_reason"] = why
        record("revision_rejected")
        return result                  # the original was better
    result["answer"] = improved.strip()
    result["revised"] = True
    record("revised")
    return result


# =========================================================================== #
#  Efficiency: don't spend a review call that can't change the answer, and
#  never let a revision make things worse.
#
#  As originally built, switching the second opinion on reviewed EVERY turn —
#  including "thanks" and one-line answers — and accepted whatever the revision
#  produced. Both cost money for nothing, and the second was capable of
#  actively degrading a good answer.
# =========================================================================== #
import json as _json
import re as _re
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path as _Path

# Turns where a reviewer has nothing to work with.
_TRIVIAL = _re.compile(
    r"^(hi|hey|hello|thanks?|thank you|ok(ay)?|got it|cool|nice|yes|no|sure|"
    r"morning|afternoon|evening)\b[\s!.,]*$", _re.I)

# Deliberately low. The first version skipped anything under 180 characters
# to save the call — but "The capital of Australia is Sydney." is 35 characters
# and wrong, which is precisely what a second opinion exists to catch. The
# right test is whether the answer makes a CHECKABLE CLAIM, not whether it is
# long.
MIN_REVIEWABLE_CHARS = 25

# Pure acknowledgements assert nothing, so there is nothing to review.
_ACK = _re.compile(
    r"^(done|ok(ay)?|saved|created|updated|deleted|removed|added|sent|"
    r"finished|complete[d]?|got it|sure|no problem|here you go)\b"
    r"[^.!?\n]{0,60}[.!]?$", _re.I)


def should_review(user_input: str, draft: str) -> tuple[bool, str]:
    """Is this turn worth a review call? Deterministic and cheap — the whole
    point is to avoid paying to find out."""
    d = (draft or "").strip()
    u = (user_input or "").strip()
    if not d:
        return False, "nothing to review"
    if _TRIVIAL.match(u):
        return False, "a pleasantry — nothing to check"
    if len(d) < MIN_REVIEWABLE_CHARS:
        return False, "too short to contain a claim"
    if _ACK.match(d.strip()):
        return False, "an acknowledgement — it asserts nothing to check"
    if d.startswith(("```", "$ ", "> ")) and len(d.splitlines()) < 4:
        return False, "a bare snippet, not a claim to check"
    return True, ""


# A critique that only wants a different tone isn't worth a rewrite: the
# rewrite costs a full generation and risks losing correct content.
_STYLE_ONLY = _re.compile(
    r"^(consider|you could|it might|optionally|as a minor|nitpick|style|"
    r"tone|formatting|wording|phrasing)\b", _re.I)
_SUBSTANTIVE = _re.compile(
    r"\b(wrong|incorrect|error|inaccurate|missing|omits?|fails? to|"
    r"contradicts?|unsupported|unsafe|dangerous|misleading|out of date|"
    r"doesn'?t answer|does not answer|hallucinat|fabricat|invented|"
    r"no evidence|not in the (?:document|source|context))\b", _re.I)


def critique_is_substantive(critique: str) -> tuple[bool, str]:
    """Distinguish 'this is wrong' from 'you could phrase it differently'."""
    c = (critique or "").strip()
    if not c:
        return False, "no critique"
    if _SUBSTANTIVE.search(c):
        return True, ""
    lines = [ln.strip("-*• ").strip() for ln in c.splitlines() if ln.strip()]
    if lines and all(_STYLE_ONLY.match(ln) for ln in lines[:3]):
        return False, "only stylistic suggestions — not worth a rewrite"
    if len(c) < 40:
        return False, "critique too vague to act on"
    return True, ""


def accept_revision(draft: str, revised: str) -> tuple[bool, str]:
    """A revision has to be demonstrably not-worse. A reviewer's complaint can
    push a model into truncating, refusing, or dropping most of a correct
    answer; silently shipping that is worse than ignoring the critique."""
    d, r = (draft or "").strip(), (revised or "").strip()
    if not r:
        return False, "the revision came back empty"
    try:
        from . import turbo
        # min_chars=1: a revision is judged against the DRAFT, not against an
        # absolute length. Correcting "Sydney" to "Canberra" is precisely what
        # a second opinion is for, and an absolute minimum would throw it away.
        # The relative rule below still catches a revision that guts a long
        # answer.
        g = turbo.gate(r, min_chars=1)
        if not g["ok"]:
            return False, "the revision failed basic checks: " + \
                          "; ".join(g["reasons"])
    except Exception:
        pass
    if len(r) < len(d) * 0.5 and len(d) > 250:
        return False, ("the revision dropped more than half the answer — "
                       "keeping the original")
    if r == d:
        return False, "the revision was identical"
    return True, ""


# --------------------------------------------------------------------------- #
#  accounting, so the feature can be judged rather than assumed
# --------------------------------------------------------------------------- #
def _stats_path():
    from . import config
    d = config.AGENT_HOME / "collaborate"
    d.mkdir(parents=True, exist_ok=True)
    return d / "stats.json"


def _read_stats() -> dict:
    try:
        return _json.loads(_stats_path().read_text("utf-8"))
    except Exception:
        return {"skipped": 0, "reviewed": 0, "approved": 0,
                "revised": 0, "revision_rejected": 0, "nitpick_ignored": 0}


def record(event: str) -> None:
    s = _read_stats()
    s[event] = s.get(event, 0) + 1
    s["updated"] = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        _stats_path().write_text(_json.dumps(s), "utf-8")
    except Exception:
        pass


def stats() -> dict:
    s = _read_stats()
    total = s.get("reviewed", 0)
    s["reviews"] = total
    s["change_rate"] = round(100.0 * s.get("revised", 0) / total, 1) \
        if total else 0.0
    if total >= 10 and s["change_rate"] < 5:
        s["advice"] = (f"{total} reviews and only {s.get('revised', 0)} "
                       f"changed the answer. The reviewer is mostly agreeing "
                       f"— you may be paying for reassurance.")
    elif total >= 10:
        s["advice"] = (f"{s['change_rate']:.0f}% of reviews improved the "
                       f"answer.")
    else:
        s["advice"] = "Not enough reviews yet to judge."
    return s
