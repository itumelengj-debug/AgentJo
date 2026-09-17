"""Crew — persistent specialist agents for Symbolic Synapse.

Sub-agents (already in the app) are ephemeral: spawned for one objective,
then gone. A CREW MEMBER is different — it persists:

  • a standing BRIEF (who it is, what it owns, how it works)
  • its own ENGINE (a cheap local model for grinding, cloud for reasoning)
  • its own WORKSPACE folder, which is the boundary of its autonomy
  • its own MEMORY CATEGORY, so its findings accumulate across runs
  • its own SCHEDULE, so it works while you don't

Two ways work reaches a specialist:

  DISPATCH — describe the job; the dispatcher picks the right member by
             matching against each brief (with a cheap keyword prior so a
             local model's opinion can't route nonsense), then runs it.
  SCHEDULE — the member wakes on its own cadence and files a report.

Autonomy is scoped, not absolute: a member runs with auto-approve ONLY for
writes inside its own workspace folder. It cannot touch the rest of your
disk unattended — the permission is granted per-run and is exactly the
workspace path, nothing broader.

Every run appends to the member's own log and to the audit trail, so "what
has the crew been doing" always has a precise answer.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_LOG = 40


def _root() -> Path:
    d = config.AGENT_HOME / "crew"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]


# --------------------------------------------------------------------------- #
#  the default crew — Symbolic Synapse (AI / BI / data consultancy)
# --------------------------------------------------------------------------- #
DEFAULT_CREW = [
    {
        "name": "Delivery",
        "role": "Client delivery engineer",
        "brief": (
            "You own client deliverables for Symbolic Synapse: BI dashboards, "
            "data pipelines, and quality assurance of what goes out the door. "
            "Typical work: build or check a Medallion pipeline with the "
            "pipeline_* tools and prove TO-BE against AS-IS; sanity-check "
            "numbers in a deliverable before it reaches a client; document "
            "what a model or report actually does. Be exacting about data "
            "correctness — a wrong number in a client dashboard costs trust. "
            "Flag assumptions explicitly rather than silently guessing."),
        "keywords": ["dashboard", "pipeline", "bi", "power bi", "etl",
                     "sql", "data quality", "qa", "deliverable", "report",
                     "medallion", "warehouse", "dax", "model"],
        "engine": "Auto",
        "schedule": None,
    },
    {
        "name": "BizDev",
        "role": "Business development researcher",
        "brief": (
            "You find and qualify opportunities for Symbolic Synapse (AI, BI "
            "and data consultancy, Johannesburg). Research prospects, "
            "understand what they'd actually buy, and draft outreach and "
            "proposal material. Ground every claim about a company in "
            "something you actually fetched — never invent headcount, tech "
            "stack or contacts. Qualify honestly: say when a prospect is a "
            "poor fit. Drafts are drafts: never send anything, leave it for "
            "human review."),
        "keywords": ["lead", "prospect", "proposal", "outreach", "pitch",
                     "client", "sales", "quote", "rfp", "bid", "pipeline "
                     "of work", "marketing"],
        "engine": "Auto",
        "schedule": None,
    },
    {
        "name": "Ops",
        "role": "Practice operations",
        "brief": (
            "You keep Symbolic Synapse running: project status, timesheets, "
            "invoicing prep, admin follow-ups. Produce short, factual status "
            "summaries — what moved, what's stuck, what needs a decision. "
            "Prefer a table over prose. Never invent a number: if you don't "
            "have the data, say what's missing and where it would come "
            "from."),
        "keywords": ["invoice", "billing", "timesheet", "status", "admin",
                     "project", "hours", "budget", "expense", "schedule",
                     "meeting", "action items"],
        "engine": "Auto",
        "schedule": None,
    },
    {
        "name": "Intel",
        "role": "Market and competitive intelligence",
        "brief": (
            "You watch the market for Symbolic Synapse: public tenders "
            "(especially SA government eTenders), competitor moves, and "
            "trends in AI/BI/data that change what clients will ask for. "
            "Report what is NEW since last time and why it matters to a "
            "small SA consultancy — not a general summary of the industry. "
            "Cite sources. Distinguish confirmed fact from your own "
            "inference."),
        "keywords": ["tender", "etender", "competitor", "market", "trend",
                     "news", "industry", "rfq", "government", "opportunity",
                     "research"],
        "engine": "Auto",
        "schedule": None,
    },
]


def _members_path() -> Path:
    return _root() / "members.json"


def members() -> list:
    try:
        data = json.loads(_members_path().read_text("utf-8"))
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    save_members(DEFAULT_CREW)
    return list(DEFAULT_CREW)


def save_members(ms: list) -> None:
    _members_path().write_text(json.dumps(ms, indent=2), "utf-8")


def get(name: str) -> dict | None:
    want = _slug(name)
    for m in members():
        if _slug(m["name"]) == want:
            return m
    return None


def upsert(member: dict) -> dict:
    ms = members()
    slug = _slug(member.get("name", ""))
    if not slug:
        return {"ok": False, "error": "a member needs a name"}
    for i, m in enumerate(ms):
        if _slug(m["name"]) == slug:
            ms[i] = {**m, **member}
            save_members(ms)
            return {"ok": True, "member": ms[i]}
    ms.append({"engine": "Auto", "keywords": [], "schedule": None, **member})
    save_members(ms)
    return {"ok": True, "member": ms[-1]}


def remove(name: str) -> dict:
    ms = members()
    keep = [m for m in ms if _slug(m["name"]) != _slug(name)]
    if len(keep) == len(ms):
        return {"ok": False, "error": "no such member"}
    save_members(keep)
    return {"ok": True}


def workspace(name: str) -> Path:
    d = _root() / _slug(name) / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def category(name: str) -> str:
    return f"crew:{_slug(name)}"


# --------------------------------------------------------------------------- #
#  dispatch — pick the right specialist for a job
# --------------------------------------------------------------------------- #
def _keyword_scores(task: str) -> dict:
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (task or "").lower()) + " "
    out = {}
    for m in members():
        hits = sum(1 for k in (m.get("keywords") or [])
                   if f" {k} " in low or f" {k}s " in low)
        out[m["name"]] = hits
    return out


_ROUTE_SYSTEM = (
    "You route one task to exactly one specialist. Reply with ONLY the "
    "specialist's name, nothing else — no punctuation, no explanation.")


def choose(task: str, brain=None, model=None) -> dict:
    """Pick a member. Keyword prior first; the engine breaks ties or handles
    tasks the keywords don't cover. A model that answers with something
    unknown is ignored rather than trusted."""
    ms = members()
    if not ms:
        return {"name": "", "why": "no crew members defined"}
    scores = _keyword_scores(task)
    best = max(scores.values()) if scores else 0
    leaders = [n for n, s in scores.items() if s == best and s > 0]
    if len(leaders) == 1:
        return {"name": leaders[0],
                "why": f"matched {best} keyword(s) for {leaders[0]}"}
    if brain is not None:
        roster = "\n".join(
            f"- {m['name']}: {m.get('role', '')} — {m.get('brief', '')[:220]}"
            for m in ms)
        try:
            kw = {"model": model} if model else {}
            resp = brain.chat(
                [{"role": "user",
                  "content": f"SPECIALISTS:\n{roster}\n\nTASK:\n{task}\n\n"
                             f"Which specialist? Name only."}],
                [_ROUTE_SYSTEM], None, **kw)
            said = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
            said = _slug(said.strip().split("\n")[0])
            for m in ms:
                if _slug(m["name"]) == said or said.startswith(
                        _slug(m["name"])):
                    return {"name": m["name"], "why": "chosen by the engine"}
        except Exception:
            pass
    if leaders:
        return {"name": leaders[0], "why": "keyword match (tie broken by "
                                           "roster order)"}
    return {"name": ms[0]["name"],
            "why": "no clear match — fell back to the first member"}


# --------------------------------------------------------------------------- #
#  running a member
# --------------------------------------------------------------------------- #
def _log_path(name: str) -> Path:
    return _root() / _slug(name) / "runs.jsonl"


def log(name: str, n: int = MAX_LOG) -> list:
    try:
        lines = _log_path(name).read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines[-n * 2:]):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
        if len(out) >= n:
            break
    return out


def recent_runs(n: int = 20) -> list:
    runs = []
    for m in members():
        for r in log(m["name"], n):
            runs.append({**r, "member": m["name"]})
    runs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return runs[:n]


# `tools` registers itself here at import. crew is CALLED BY tools, so crew
# importing tools back was the cycle — and a cycle means neither module can
# be read, tested or changed alone. A hook keeps the arrow pointing one way
# while every existing caller carries on working unchanged.
_DISPATCH = {"execute": None, "tool_defs": None}


def use_dispatcher(execute, tool_defs_fn) -> None:
    """Called by `tools` on import."""
    _DISPATCH["execute"] = execute
    _DISPATCH["tool_defs"] = tool_defs_fn


def _dispatcher():
    if _DISPATCH["execute"] is None:
        raise RuntimeError(
            "No tool dispatcher registered. `agent.tools` does this on "
            "import; import it before running a crew member.")
    return _DISPATCH["execute"], _DISPATCH["tool_defs"]()


def run(name: str, task: str, brain, memory, console, session_id: str = "",
        model=None,
        execute=None, tool_defs=None) -> dict:
    """Run one specialist on one task, inside its own workspace, with its
    own standing brief and accumulated memory."""
    m = get(name)
    if m is None:
        return {"ok": False, "error": f"no crew member named '{name}'"}
    ws = workspace(m["name"])
    cat = category(m["name"])

    # its own memory: what this specialist has learned before
    try:
        prior = memory.search_memories(task, limit=6)
        prior = [p for p in prior if p.get("category") == cat][:4]
    except Exception:
        prior = []
    prior_txt = ("\n".join("- " + p["content"] for p in prior)
                 if prior else "(nothing yet)")

    context = (
        f"YOU ARE: {m['name']} — {m.get('role', '')}\n\n"
        f"STANDING BRIEF:\n{m.get('brief', '')}\n\n"
        f"YOUR WORKSPACE (the ONLY folder you may write to):\n{ws}\n"
        "Save any file you produce there. You have write access to that "
        "folder without asking; you must NOT write anywhere else.\n\n"
        f"WHAT YOU LEARNED PREVIOUSLY:\n{prior_txt}\n\n"
        "End with a short report: what you did, what you found, what needs "
        "a human decision.")

    # scoped autonomy: auto-approve applies to this workspace only
    try:
        memory.add_permission("write_dir", str(ws))
    except Exception:
        pass

    # The dispatcher arrives as an argument. `tools` calls `crew`, so `tools`
    # supplies it — reaching back the other way is what made these two a
    # cycle, and a cycle means neither can be read or tested alone.
    from . import subagent
    if execute is None or tool_defs is None:
        execute, tool_defs = _dispatcher()
    t0 = time.time()
    try:
        report = subagent.run_subagent(
            brain, memory, console, task, context,
            auto_approve=True, session_id=session_id,
            model=model or _engine_token(m),
            tier_label=f"crew:{m['name']}",
            execute=execute, tool_defs=tool_defs)
        ok = not report.startswith("Sub-agent error")
    except Exception as exc:
        report, ok = f"{type(exc).__name__}: {exc}", False

    entry = {"ts": round(time.time(), 3), "iso": _iso(), "task": task[:300],
             "ok": ok, "seconds": round(time.time() - t0, 1),
             "report": report[:4000]}
    p = _log_path(m["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    # the specialist's own accumulating memory
    if ok:
        try:
            memory.add_memory(f"[{m['name']}] {task[:120]} → "
                              f"{report[:280]}", cat)
        except Exception:
            pass
    try:
        from . import audit
        audit.record("crew", name=m["name"], ok=ok, detail=task[:160],
                     summary=report[:200])
    except Exception:
        pass
    return {"ok": ok, "member": m["name"], "report": report,
            "seconds": entry["seconds"], "workspace": str(ws)}


def set_schedule(name: str, spec: dict | None, memory, scheduler) -> dict:
    """Give a member its own cadence (or clear it). spec:
    {"kind": "daily"|"weekly", "time": "07:30", "dow": 0, "task": "..."}"""
    m = get(name)
    if m is None:
        return {"ok": False, "error": "no such member"}
    old = (m.get("schedule") or {}).get("id")
    if old:
        try:
            memory.delete_schedule(int(old))
        except Exception:
            pass
    if not spec:
        upsert({**m, "schedule": None})
        return {"ok": True, "schedule": None}
    kind = spec.get("kind", "weekly")
    time_str = spec.get("time", "07:30")
    task = spec.get("task") or f"Run your standing brief for {m['name']}."
    spec_json = scheduler.make_spec(
        kind, time_str=time_str, n=30,
        **({"dow": int(spec.get("dow", 0))} if kind == "weekly" else {}))
    nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    sid = memory.create_schedule(
        f"Crew — {m['name']}", task, spec_json, m.get("engine", "Auto"),
        False, nxt, action="crew", payload=json.dumps(
            {"member": m["name"], "task": task}))
    saved = {"id": sid, "kind": kind, "time": time_str,
             "dow": spec.get("dow", 0), "task": task}
    upsert({**m, "schedule": saved})
    return {"ok": True, "schedule": saved}


def _engine_token(m: dict):
    """None means 'the brain's default'; anything else pins the member."""
    eng = (m.get("engine") or "Auto").strip()
    return None if eng in ("", "Auto") else eng


def dispatch(task: str, brain, memory, console, session_id: str = "",
             execute=None, tool_defs=None,
             model=None) -> dict:
    pick = choose(task, brain=brain, model=model)
    if not pick["name"]:
        return {"ok": False, "error": pick["why"]}
    res = run(pick["name"], task, brain, memory, console,
              session_id=session_id, execute=execute, tool_defs=tool_defs)
    return {**res, "routed_why": pick["why"]}


# =========================================================================== #
#  Handoffs — specialists that pass work to each other
#
#  Four specialists working alone are four assistants. The value of a crew is
#  the HANDOFF: Intel finds a tender, BizDev decides whether it's worth
#  bidding and drafts the approach, Delivery scopes what building it would
#  actually take. Each step sees what the previous one produced, so the last
#  report is grounded in real prior work rather than a fresh guess.
#
#  Two guardrails, because chained autonomy is where multi-agent systems go
#  wrong:
#
#    DEPTH CAP — a chain is a fixed list of steps, and a member may appear
#    once. There is no dynamic "and now hand off to whoever" that could loop
#    two specialists into each other burning tokens until a budget stops it.
#
#    TRUNCATED CARRY — each step receives the previous step's report trimmed
#    to a bounded size. Otherwise step four carries the whole transcript of
#    one, two and three, and the context (and the bill) grows quadratically.
# =========================================================================== #
MAX_CHAIN_STEPS = 5
CARRY_CHARS = 2500

# Ready-made chains for the work this consultancy actually does.
DEFAULT_CHAINS = {
    "opportunity": {
        "name": "opportunity",
        "description": ("Find something worth bidding on, qualify it "
                        "honestly, then scope what delivering it takes."),
        "steps": [
            {"member": "Intel",
             "instruction": "Find and summarise the most relevant current "
                            "opportunities. Cite sources. Say what is new."},
            {"member": "BizDev",
             "instruction": "From Intel's findings, pick the ones genuinely "
                            "worth pursuing and say plainly which are not, "
                            "and why. Draft an approach for the best one."},
            {"member": "Delivery",
             "instruction": "For the opportunity BizDev selected, scope what "
                            "building it would actually take: the work, the "
                            "data needed, the risks, and what you'd want "
                            "clarified before committing."},
        ],
    },
    "pursue": {
        "name": "pursue",
        "description": "Qualify a specific opportunity, then scope it.",
        "steps": [
            {"member": "BizDev",
             "instruction": "Qualify this opportunity honestly — fit, "
                            "competition, what would make us lose it. Draft "
                            "an approach if it's worth pursuing."},
            {"member": "Delivery",
             "instruction": "Scope the delivery: work involved, data needed, "
                            "risks, and open questions."},
        ],
    },
    "review": {
        "name": "review",
        "description": "Delivery builds or checks it; Ops turns it into "
                       "status and next actions.",
        "steps": [
            {"member": "Delivery",
             "instruction": "Do the technical work or review requested. Be "
                            "exact about correctness and state assumptions."},
            {"member": "Ops",
             "instruction": "Turn Delivery's output into a short status: "
                            "what moved, what's blocked, what needs a "
                            "decision from Itumeleng."},
        ],
    },
}


def _chains_path() -> Path:
    return _root() / "chains.json"


def chains() -> dict:
    try:
        data = json.loads(_chains_path().read_text("utf-8"))
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    save_chains(DEFAULT_CHAINS)
    return dict(DEFAULT_CHAINS)


def save_chains(data: dict) -> None:
    _chains_path().write_text(json.dumps(data, indent=2), "utf-8")


def validate_chain(steps: list) -> str:
    """'' when runnable, else why not. Cycles are impossible by construction —
    a member may appear at most once."""
    if not steps:
        return "a chain needs at least one step"
    if len(steps) > MAX_CHAIN_STEPS:
        return f"a chain may have at most {MAX_CHAIN_STEPS} steps"
    seen = set()
    for s in steps:
        who = _slug(str(s.get("member") or ""))
        if not who:
            return "every step needs a member"
        if get(who) is None:
            return f"no crew member named '{s.get('member')}'"
        if who in seen:
            return (f"'{s.get('member')}' appears twice — a member may only "
                    f"take one step, so a chain can't loop")
        seen.add(who)
    return ""


def _audit(name: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("crew", name=name, summary=summary[:250])
    except Exception:
        pass


def _log_chain(entry: dict) -> None:
    try:
        with open(_root() / "chain_runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def chain_runs(n: int = 15) -> list:
    try:
        lines = (_root() / "chain_runs.jsonl").read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
        if len(out) >= n:
            break
    return out


def run_chain(chain: str | list, task: str, brain, memory, console,
              session_id: str = "", model=None,
              execute=None, tool_defs=None) -> dict:
    """Run a handoff chain. Each specialist sees the brief, the original task,
    and what the previous specialist actually produced.

    A failed step stops the chain — passing a failure downstream would have
    the next specialist confidently building on nothing."""
    if isinstance(chain, str):
        spec = chains().get(chain)
        if spec is None:
            return {"ok": False,
                    "error": f"no chain called '{chain}'. Have: "
                             f"{', '.join(chains())}"}
        steps = spec["steps"]
        label = spec["name"]
    else:
        steps = chain
        label = "custom"
    err = validate_chain(steps)
    if err:
        return {"ok": False, "error": err}

    results, carry = [], ""
    for i, step in enumerate(steps, 1):
        who = str(step.get("member") or "")
        instruction = str(step.get("instruction") or "")
        prompt = (f"ORIGINAL REQUEST:\n{task}\n\n"
                  f"YOUR STEP ({i} of {len(steps)}):\n{instruction}")
        if carry:
            prompt += (f"\n\nWHAT THE PREVIOUS SPECIALIST PRODUCED "
                      f"(use it — do not start over):\n{carry}")
        res = run(who, prompt, brain, memory, console,
                  session_id=session_id or f"chain-{label}", model=model,
                  execute=execute, tool_defs=tool_defs)
        results.append({"step": i, "member": who,
                        "ok": res.get("ok", False),
                        "seconds": res.get("seconds"),
                        "report": (res.get("report") or res.get("error", ""))})
        if not res.get("ok"):
            entry = {"at": _iso(), "chain": label, "task": task[:200],
                     "ok": False, "steps": results,
                     "stopped_at": i,
                     "why": f"{who} failed — chain stopped rather than "
                            f"passing nothing downstream"}
            _log_chain(entry)
            _audit("chain", f"{label}: stopped at step {i} ({who})")
            return {"ok": False, **entry}
        carry = (res.get("report") or "")[:CARRY_CHARS]

    entry = {"at": _iso(), "chain": label, "task": task[:200], "ok": True,
             "steps": results, "final": results[-1]["report"] if results
             else ""}
    _log_chain(entry)
    _audit("chain", f"{label}: {len(results)} step(s) completed")
    return {"ok": True, **entry}
