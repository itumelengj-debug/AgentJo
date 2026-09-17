"""Turbo — the local model does the work, the cloud one is called only when
the local answer isn't good enough.

Most turns don't need a frontier model. Summarising, extracting, reformatting,
answering from retrieved context, routine tool driving — a 7B model handles
these. A minority genuinely need the stronger engine. Paying cloud rates for
all of them to cover the minority is how the bill gets large.

So: the local engine answers first, a **deterministic** gate reads that answer,
and only a failed gate escalates — and when it escalates, the cloud engine is
given the local draft to improve rather than a blank page, so it isn't
re-deriving work that was already done.

Two things this is careful about.

**The gate never asks a model whether the answer was good.** That would cost a
call to save a call, and a model's opinion of its own output is not reliable
enough to spend money on. Every check here is mechanical: emptiness, truncation,
refusal language, broken JSON when JSON was required, looping, and a
placeholder left behind. Each one is a thing that is *observably* wrong rather
than a judgement about quality.

**Escalating costs more than going straight to cloud.** You pay the local pass
(free) plus the cloud pass, and the cloud pass now carries the draft too. Turbo
only wins if the local model clears the gate often. The savings figure is
therefore reported honestly — turns saved, turns escalated — so it can be
judged rather than assumed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

# things that are observably wrong, not matters of taste
_REFUSAL = re.compile(
    r"\b(i (?:cannot|can't|am unable to|do not have the ability)"
    r"|as an ai\b|i'm just an ai|i don't have access to"
    r"|i'm sorry,? but i (?:cannot|can't))", re.I)
_PLACEHOLDER = re.compile(
    r"(\[(?:insert|your|todo|placeholder|xxx)[^\]]*\]"
    r"|\byour[_ ]name[_ ]here\b|<[A-Z_]{3,}>|TODO:)", re.I)
_TRUNCATED = re.compile(r"[A-Za-z0-9,;:\-\(\[]$")


def _dir() -> Path:
    d = config.AGENT_HOME / "turbo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enabled() -> bool:
    return bool(getattr(config, "TURBO", False))


def _looping(text: str) -> bool:
    """A small model that has lost the thread repeats itself. Three identical
    non-trivial lines, or the same sentence twice at length, is a failure the
    user shouldn't have to notice."""
    lines = [ln.strip() for ln in (text or "").splitlines() if len(ln.strip()) > 25]
    if len(lines) >= 3:
        for ln in set(lines):
            if lines.count(ln) >= 3:
                return True
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "")
                 if len(s.strip()) > 60]
    return len(sentences) != len(set(sentences)) and len(sentences) >= 2


def gate(reply: str, *, expect_json: bool = False,
         min_chars: int = 40) -> dict:
    """Is this answer usable? Deterministic — no engine is consulted."""
    text = (reply or "").strip()
    reasons = []

    if not text:
        reasons.append("the local model returned nothing")
    elif len(text) < min_chars and not expect_json:
        reasons.append("the answer is too short to be a real one")

    if expect_json and text:
        body = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            json.loads(body[body.find("{"):body.rfind("}") + 1]
                       if "{" in body else body)
        except Exception:
            reasons.append("JSON was required and it didn't produce valid JSON")

    if text and _REFUSAL.search(text):
        reasons.append("the local model refused or claimed it couldn't")
    if text and _PLACEHOLDER.search(text):
        reasons.append("the answer still contains a placeholder")
    if text and _looping(text):
        reasons.append("the answer repeats itself")
    if len(text) > 200 and _TRUNCATED.search(text):
        reasons.append("the answer stops mid-sentence")

    return {"ok": not reasons, "reasons": reasons,
            "chars": len(text)}


ESCALATION_NOTE = (
    "A smaller model produced the draft below. It didn't pass the checks "
    "({why}). Answer the user's request properly. Use anything correct in the "
    "draft, discard the rest, and don't mention the draft or that this was "
    "a second attempt.\n\n--- DRAFT ---\n{draft}\n--- END DRAFT ---")


def escalation_prompt(draft: str, reasons: list) -> str:
    return ESCALATION_NOTE.format(why="; ".join(reasons) or "quality checks",
                                  draft=(draft or "(nothing)")[:4000])


# --------------------------------------------------------------------------- #
#  honest accounting
# --------------------------------------------------------------------------- #
def _stats_path() -> Path:
    return _dir() / "stats.json"


def _read() -> dict:
    try:
        return json.loads(_stats_path().read_text("utf-8"))
    except Exception:
        return {"local_only": 0, "escalated": 0, "saved_usd": 0.0,
                "extra_usd": 0.0, "reasons": {}}


def record(local_only: bool, reasons=None, cloud_cost_estimate: float = 0.0,
           category: str = "") -> dict:
    """Count what actually happened. A turn the local model handled saved
    roughly what the cloud one would have cost; an escalated turn cost the
    cloud pass anyway, plus the draft it had to read. Both are recorded, so
    the feature can be judged on its own numbers."""
    s = _read()
    if local_only:
        s["local_only"] = s.get("local_only", 0) + 1
        s["saved_usd"] = round(s.get("saved_usd", 0.0)
                               + max(0.0, cloud_cost_estimate), 6)
    else:
        s["escalated"] = s.get("escalated", 0) + 1
        s["extra_usd"] = round(s.get("extra_usd", 0.0)
                               + max(0.0, cloud_cost_estimate) * 0.15, 6)
        for r in (reasons or []):
            s.setdefault("reasons", {})
            s["reasons"][r] = s["reasons"].get(r, 0) + 1
    if category:
        cats = s.setdefault("categories", {})
        c = cats.setdefault(category, {"tried": 0, "won": 0})
        c["tried"] += 1
        if local_only:
            c["won"] += 1
    s["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        _stats_path().write_text(json.dumps(s), "utf-8")
    except Exception:
        pass
    return s


def measured_cloud_turn_cost() -> float:
    """What a chat turn on the cloud engine has ACTUALLY cost, this month.

    The saving was reported against a hardcoded 0.004, which made the number
    decorative. This reads the ledger: total spend on chat divided by the
    calls that produced it. Falls back only when there's nothing to measure
    yet, and says so."""
    try:
        from . import costs
        rep = costs.month_report()
        chat = (rep.get("by_feature") or {}).get("chat") or {}
        calls = int(chat.get("calls") or 0)
        usd = float(chat.get("usd") or 0.0)
        if calls >= 5 and usd > 0:
            return usd / calls
    except Exception:
        pass
    return 0.0


def stats() -> dict:
    s = _read()
    total = s.get("local_only", 0) + s.get("escalated", 0)
    s["turns"] = total
    s["hit_rate"] = round(100.0 * s.get("local_only", 0) / total, 1) if total \
        else 0.0
    per_turn = measured_cloud_turn_cost()
    s["measured_cloud_turn_cost"] = round(per_turn, 6)
    if per_turn > 0:
        # a turn the local model finished is a cloud turn you didn't buy;
        # an escalated one still cost the cloud, plus the draft it carried
        s["saved_usd"] = round(s.get("local_only", 0) * per_turn, 4)
        s["extra_usd"] = round(s.get("escalated", 0) * per_turn * 0.15, 4)
        s["basis"] = "measured from this month's chat spend"
    else:
        s["basis"] = ("estimated — not enough cloud turns recorded yet to "
                      "measure what one costs")
    s["net_usd"] = round(s.get("saved_usd", 0.0) - s.get("extra_usd", 0.0), 4)

    cats = []
    for name, c in (s.get("categories") or {}).items():
        tried, won = int(c.get("tried", 0)), int(c.get("won", 0))
        cats.append({"category": name, "tried": tried, "won": won,
                     "rate": round(100.0 * won / tried, 1) if tried else 0.0,
                     "still_trying": category_verdict(name)["try_local"]})
    cats.sort(key=lambda c: -c["tried"])
    s["by_category"] = cats
    dropped = [c["category"] for c in cats if not c["still_trying"]]

    top = sorted((s.get("reasons") or {}).items(), key=lambda kv: -kv[1])
    s["top_reason"] = top[0][0] if top else ""
    if total < 10:
        s["advice"] = (f"{total} turn(s) so far — not enough to judge. "
                       f"Routing sends only small-model work locally, so most "
                       f"turns won't appear here at all.")
    elif s["hit_rate"] < 35:
        s["advice"] = (f"Only {s['hit_rate']:.0f}% of attempted turns finished "
                       f"locally, so most paid for both passes. Either the "
                       f"local model is too small for what's being routed to "
                       f"it, or turn Turbo off."
                       + (f" Already stopped trying: {', '.join(dropped)}."
                          if dropped else ""))
    else:
        s["advice"] = (f"{s['hit_rate']:.0f}% of attempted turns finished "
                       f"locally"
                       + (f", saving about ${s['net_usd']:.2f} this month"
                          if per_turn > 0 else "")
                       + (f". Stopped trying: {', '.join(dropped)}."
                          if dropped else "."))
    return s


def usable(inventory: dict) -> tuple[bool, str]:
    """Turbo needs both kinds. Say plainly when it can't run."""
    if not inventory.get("has_local"):
        return False, ("Turbo needs a local engine to draft with — install "
                       "Ollama and pull a model.")
    if not inventory.get("has_cloud"):
        return False, ("Turbo needs a cloud engine to escalate to. Without "
                       "one, everything already runs locally.")
    return True, ""


# =========================================================================== #
#  Routing — deciding BEFORE spending anything
#
#  The first version tried the local model on every turn. That is the one way
#  this feature can cost more than it saves: a task a 7B model was never going
#  to manage burns a local pass, fails the gate, and then pays the cloud rate
#  anyway. The local pass is free in money but not in time, and the escalation
#  carries the draft, so the cloud call is slightly larger too.
#
#  So: judge the request first, cheaply and deterministically, and only try
#  locally where a small model has a real chance. Then LEARN — if a kind of
#  task keeps escalating, stop trying it.
# =========================================================================== #
import re as _re

# What small models reliably do well: transform text that is already present.
_LOCAL_FRIENDLY = (
    # order matters: "summarise in three bullets" is summarising, not
    # formatting, and the category is what the learning is keyed on
    ("summarise", _re.compile(
        r"\b(summari[sz]e|tl;?dr|key points|main points|in a sentence|"
        r"one paragraph|gist|recap|digest)\b", _re.I)),
    ("extract", _re.compile(
        r"\b(extract|pull out|list the|find all|which ones|what are the|"
        r"names? of|dates?|figures?|addresses)\b", _re.I)),
    ("reformat", _re.compile(
        r"\b(reformat|format|tidy|clean up|bullet|bullets|table|list|"
        r"rewrite as|convert to|to json|to csv|to markdown|title case|"
        r"proofread|fix the grammar|shorten|expand slightly)\b", _re.I)),
    ("classify", _re.compile(
        r"\b(classif|categor|is this|does this|label|tag|sentiment|"
        r"yes or no|true or false)\b", _re.I)),
    ("translate", _re.compile(r"\b(translate|in (afrikaans|zulu|french|"
                              r"german|spanish|portuguese))\b", _re.I)),
)

# What they reliably do badly, or where a wrong answer is expensive.
_CLOUD_ONLY = (
    ("code", _re.compile(
        r"\b(write|refactor|debug|fix|implement|build)\b[^.]{0,40}"
        r"\b(code|function|script|class|module|test|sql|query|regex|"
        r"component|endpoint|migration)\b", _re.I)),
    ("build", _re.compile(
        r"\binto yourself\b|\bself.?improve\b|\badd .{0,30}feature\b|"
        r"\bcreate a (panel|module|endpoint|tab)\b", _re.I)),
    ("reason", _re.compile(
        r"\b(why|analyse|analyze|compare|evaluate|assess|recommend|"
        r"strategy|trade.?offs?|pros and cons|decide|should (i|we)|"
        r"investigate|diagnose|root cause)\b", _re.I)),
    ("money", _re.compile(
        r"\b(apply|application|cover letter|cv\b|proposal|tender|invoice|"
        r"client|contract|negotiat)\b", _re.I)),
    ("tools", _re.compile(
        # "email addresses" is a noun, "email me the report" is an action —
        # matching the bare word sent extraction work to the cloud for nothing
        r"\b(run|execute|delete|install|deploy|schedule|scrape|"
        r"open the browser)\b"
        r"|\bsend\b|\bemail (me|him|her|them|it|the [a-z]+ to)\b"
        r"|\b(forward|reply) (to|it|the)\b", _re.I)),
)

LONG_INPUT_CHARS = 4000


def classify_task(text: str, has_attachments: bool = False) -> dict:
    """Which side of the line is this request on?

    Deterministic on purpose: asking a model which model to use would cost
    the very call this is trying to avoid."""
    s = " ".join(str(text or "").split())
    if not s:
        return {"category": "empty", "local_ok": False,
                "why": "nothing to do"}
    if has_attachments:
        return {"category": "attachment", "local_ok": False,
                "why": "attachments need the stronger engine"}
    if len(s) > LONG_INPUT_CHARS:
        return {"category": "long", "local_ok": False,
                "why": f"{len(s)} characters is more context than the local "
                       f"model handles well"}
    # a clearly local-friendly instruction wins over an incidental keyword:
    # "extract the email addresses" is extraction, not sending email
    for name, rx in _LOCAL_FRIENDLY[:2]:
        if rx.search(s):
            return {"category": name, "local_ok": True,
                    "why": f"{name} is what small models are good at"}
    for name, rx in _CLOUD_ONLY:
        if rx.search(s):
            return {"category": name, "local_ok": False,
                    "why": f"{name} work goes straight to the strong engine"}
    for name, rx in _LOCAL_FRIENDLY[2:]:
        if rx.search(s):
            return {"category": name, "local_ok": True,
                    "why": f"{name} is what small models are good at"}
    # short and unclassified: worth one free attempt
    if len(s) <= 240:
        return {"category": "short", "local_ok": True,
                "why": "short and simple enough to try locally"}
    return {"category": "general", "local_ok": False,
            "why": "not obviously a small-model task"}


# --------------------------------------------------------------------------- #
#  learning from what actually happened
# --------------------------------------------------------------------------- #
GIVE_UP_AFTER = 6          # attempts in a category before judging it
GIVE_UP_BELOW = 0.34       # hit rate under which we stop trying locally


def _cat_stats() -> dict:
    return _read().get("categories") or {}


def category_verdict(category: str) -> dict:
    """Has this kind of task earned another local attempt?"""
    c = (_cat_stats().get(category) or {})
    tried = int(c.get("tried", 0))
    won = int(c.get("won", 0))
    if tried < GIVE_UP_AFTER:
        return {"try_local": True, "rate": None, "tried": tried,
                "why": "not enough attempts yet to judge"}
    rate = won / tried if tried else 0
    if rate < GIVE_UP_BELOW:
        return {"try_local": False, "rate": rate, "tried": tried,
                "why": (f"only {won} of {tried} '{category}' turns finished "
                        f"locally, so this now goes straight to the cloud")}
    return {"try_local": True, "rate": rate, "tried": tried,
            "why": f"{won} of {tried} finished locally"}


def should_try_local(text: str, has_attachments: bool = False) -> dict:
    """The whole decision, before a single token is spent."""
    c = classify_task(text, has_attachments)
    if not c["local_ok"]:
        return {**c, "try_local": False}
    v = category_verdict(c["category"])
    return {**c, "try_local": v["try_local"],
            "why": v["why"] if not v["try_local"] else c["why"],
            "measured_rate": v.get("rate")}
