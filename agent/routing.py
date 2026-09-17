"""Routing — which engine should answer this, and why.

Two faults this replaces.

**Auto reached for Claude first regardless.** The chain was built in a fixed
"quality order" with Anthropic at the head, so choosing another cloud engine
as your default changed the label in the top bar and not much else. Whatever
you have chosen is now the cloud tier; Claude is one option in a list, not the
list.

**Everything went to the same engine.** Parsing an advert, summarising a
thread, and planning a refactor are different jobs with different costs. A
7B model handles the first two and will not handle the third, and paying a
flagship model to reformat a table is money set on fire. So the task decides
the tier, and the tier decides the engine — with the choice recorded, because
routing you cannot see is routing you cannot trust.

The rules are ordinary data. They can be read, changed, and turned off, and
when routing does something surprising the panel tells you which rule fired.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Tiers, from cheapest to most capable. A tier is a role, not a product.
TIERS = ("local", "cloud", "reasoning")

# The rules, in order. First match wins. `tier` is what it needs; `why` is
# what the panel shows when it fires.
DEFAULT_RULES = [
    {"when": "attachment", "tier": "cloud",
     "why": "images and files need a model that can see them"},
    {"when": "long_input", "tier": "cloud",
     "why": "more context than a small model holds"},
    {"when": "code", "tier": "reasoning",
     "why": "code is where a small model quietly gets it wrong"},
    {"when": "build", "tier": "reasoning",
     "why": "changing this app's own source"},
    {"when": "reason", "tier": "reasoning",
     "why": "analysis and comparison need the stronger model"},
    {"when": "money", "tier": "cloud",
     "why": "applications and client work — worth the better engine"},
    {"when": "tools", "tier": "cloud",
     "why": "tool use goes wrong in ways that cost something"},
    {"when": "summarise", "tier": "local",
     "why": "summarising is what small models are good at"},
    {"when": "extract", "tier": "local", "why": "extraction is local work"},
    {"when": "reformat", "tier": "local", "why": "reformatting is local work"},
    {"when": "classify", "tier": "local",
     "why": "classification is local work"},
    {"when": "translate", "tier": "local", "why": "translation is local work"},
    {"when": "short", "tier": "local",
     "why": "short and simple enough to try locally"},
]


def _path() -> Path:
    d = config.AGENT_HOME / "routing"
    d.mkdir(parents=True, exist_ok=True)
    return d / "rules.json"


def rules() -> list:
    try:
        data = json.loads(_path().read_text("utf-8"))
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return [dict(r) for r in DEFAULT_RULES]


def save_rules(items: list) -> list:
    clean = []
    for r in items or []:
        if not isinstance(r, dict) or not r.get("when"):
            continue
        tier = r.get("tier")
        if tier not in TIERS:
            continue
        clean.append({"when": str(r["when"])[:40], "tier": tier,
                      "why": str(r.get("why", ""))[:160],
                      "off": bool(r.get("off"))})
    _path().write_text(json.dumps(clean, indent=2), "utf-8")
    return clean


def reset_rules() -> list:
    return save_rules([dict(r) for r in DEFAULT_RULES])


# --------------------------------------------------------------------------- #
#  which engine serves a tier
# --------------------------------------------------------------------------- #
def tier_engines(inventory: dict = None, default_engine: str = None) -> dict:
    """The engine that fills each tier, on this machine, right now.

    `cloud` is YOUR chosen engine. That is the whole point of choosing one —
    the previous version put Anthropic at the head of a fixed quality order,
    so the choice changed a label and little else.
    """
    from . import engines
    inv = inventory if inventory is not None else engines.inventory()
    chosen = (default_engine
              if default_engine is not None
              else getattr(config, "DEFAULT_ENGINE", "") or "")
    cloud_names = [e["name"] for e in inv.get("cloud", [])]
    local_names = [e["name"] for e in inv.get("local", [])]

    cloud = ""
    if chosen and chosen.lower() not in ("auto", ""):
        # honour the choice whichever tier it belongs to
        if chosen in cloud_names or chosen in local_names:
            cloud = chosen
    if not cloud:
        cloud = cloud_names[0] if cloud_names else (
            local_names[0] if local_names else "")

    local = local_names[0] if local_names else cloud

    # "reasoning" is the strongest thing available: the chosen cloud engine
    # unless a bigger sibling is configured
    reasoning = cloud
    for name in cloud_names:
        if any(k in name.lower() for k in ("opus", "r1", "pro", "reason",
                                           "70b", "72b", "large")):
            reasoning = name
            break
    return {"local": local, "cloud": cloud, "reasoning": reasoning,
            "chosen": chosen,
            "cloud_options": cloud_names, "local_options": local_names}


def decide(text: str, has_attachments: bool = False, inventory: dict = None,
           default_engine: str = None) -> dict:
    """Which engine, and which rule said so."""
    from . import turbo
    cls = turbo.classify_task(text, has_attachments)
    cat = cls["category"]
    if has_attachments:
        cat = "attachment"
    tiers = tier_engines(inventory, default_engine)

    for r in rules():
        if r.get("off") or r.get("when") != cat:
            continue
        engine = tiers.get(r["tier"]) or tiers["cloud"]
        return {"engine": engine, "tier": r["tier"], "category": cat,
                "rule": r.get("when"), "why": r.get("why", ""),
                "matched": True, "tiers": tiers}
    # nothing matched: the chosen engine, which is the honest default
    return {"engine": tiers["cloud"], "tier": "cloud", "category": cat,
            "rule": "", "why": "no rule matched, so your chosen engine",
            "matched": False, "tiers": tiers}


def chain_for(text: str, has_attachments: bool = False,
              inventory: dict = None, default_engine: str = None) -> dict:
    """The engine to try, then what to fall back to.

    Fallback is still ordered by what can actually survive the failure, but
    it starts from the routed engine rather than from a fixed favourite."""
    d = decide(text, has_attachments, inventory, default_engine)
    t = d["tiers"]
    chain, seen = [], set()
    for name in ([d["engine"]] + [t["cloud"], t["reasoning"], t["local"]]
                 + t["cloud_options"] + t["local_options"]):
        if name and name not in seen:
            seen.add(name)
            chain.append(name)
    return {**d, "chain": chain}


def explain(text: str, has_attachments: bool = False) -> str:
    """One sentence, for the console and the panel."""
    d = decide(text, has_attachments)
    return (f"{d['engine']} ({d['tier']}) — {d['why']}"
            if d["matched"] else
            f"{d['engine']} — {d['why']}")


def log_decision(d: dict) -> None:
    """Routing you cannot see is routing you cannot trust."""
    try:
        p = config.AGENT_HOME / "routing" / "decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"),
                "engine": d.get("engine"), "tier": d.get("tier"),
                "category": d.get("category"), "rule": d.get("rule"),
            }) + "\n")
    except Exception:
        pass


def recent_decisions(n: int = 40) -> list:
    p = config.AGENT_HOME / "routing" / "decisions.jsonl"
    try:
        lines = p.read_text("utf-8").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def usage_summary() -> dict:
    """Where the work actually went — which is how you check the rules are
    doing what you meant rather than what you wrote."""
    by_engine, by_tier = {}, {}
    for d in recent_decisions(400):
        e, t = d.get("engine") or "?", d.get("tier") or "?"
        by_engine[e] = by_engine.get(e, 0) + 1
        by_tier[t] = by_tier.get(t, 0) + 1
    total = sum(by_engine.values())
    return {"total": total, "by_engine": by_engine, "by_tier": by_tier,
            "local_share": (round(100.0 * by_tier.get("local", 0) / total)
                            if total else 0)}


# --------------------------------------------------------------------------- #
#  One decision
#
#  Routing and Turbo were asking the same question — "can a small model do
#  this?" — with two classifiers, in two places, over the same variable. That
#  is why routing had to stay advisory: whichever wrote `force` last silently
#  disabled the other.
#
#  So there is one plan. It says which engine, and whether this is a local
#  attempt that may escalate. Turbo becomes what it always was underneath: the
#  ESCALATION policy for work routed local, not a second router.
# --------------------------------------------------------------------------- #
def plan(text: str, has_attachments: bool = False, inventory: dict = None,
         default_engine: str = None, turbo_on: bool = None) -> dict:
    """The single answer: engine, tier, and whether it may escalate."""
    from . import turbo as _turbo
    if turbo_on is None:
        turbo_on = bool(getattr(config, "TURBO", False))

    d = decide(text, has_attachments, inventory, default_engine)
    tiers = d["tiers"]
    local_available = bool(tiers.get("local_options"))

    # Routing says local. Turbo's learning gets a veto, because it knows what
    # has actually failed here — a category that keeps escalating should stop
    # being tried, and that is a fact about this machine, not a rule.
    if d["tier"] == "local":
        if not local_available:
            return {**d, "engine": tiers["cloud"], "tier": "cloud",
                    "escalate": False,
                    "why": "no local engine, so your chosen one",
                    "turbo": False}
        verdict = _turbo.category_verdict(d["category"])
        if not verdict["try_local"]:
            return {**d, "engine": tiers["cloud"], "tier": "cloud",
                    "escalate": False, "turbo": False,
                    "why": verdict["why"]}
        # attachments and images can't go local whatever the rule says
        if has_attachments:
            return {**d, "engine": tiers["cloud"], "tier": "cloud",
                    "escalate": False, "turbo": False,
                    "why": "attachments need the stronger engine"}
        return {**d, "engine": tiers["local"], "tier": "local",
                "escalate": bool(turbo_on and tiers["cloud"]),
                "turbo": bool(turbo_on),
                "escalate_to": tiers["cloud"]}

    return {**d, "escalate": False, "turbo": False}


def worth_routing(inventory: dict = None,
                  default_engine: str = None) -> tuple:
    """Is there actually anything to route between?

    Counting engines wasn't the right test. Two cloud engines with no local
    model and no stronger sibling means every tier resolves to the same
    place — so overriding Auto changes the plumbing and nothing else, which
    is how a change with no benefit still manages to break something.
    """
    t = tier_engines(inventory, default_engine)
    if t["local_options"] and t["local"] != t["cloud"]:
        return True, "a local engine can take the light work"
    if t["reasoning"] and t["reasoning"] != t["cloud"]:
        return True, "a stronger engine is available for hard work"
    return False, ("every tier resolves to the same engine, so routing would "
                   "change nothing")


def describe_plan(p: dict) -> str:
    if p.get("escalate"):
        return (f"{p['engine']} first ({p['why']}), escalating to "
                f"{p.get('escalate_to')} if the answer isn't usable")
    return f"{p['engine']} ({p['tier']}) — {p['why']}"
