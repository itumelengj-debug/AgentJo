"""Watchdog — noticing when something has quietly stopped working.

Every fault this app has had in real use was found the same way: the person
using it noticed something looked wrong and reported it. The dashboard wasn't
wired to its poll. The health board was shadowed by another route and rendered
nothing. Crew raised NameError on every call for weeks. A scheduled brief
called a model that had been retired on 7 August and failed every morning
after. A watcher whose host stopped resolving failed hourly for two days.

None of that was subtle in hindsight. What they share is that the app had no
opinion about its own *silence*. The health board answers "is this configured
and reachable right now" — a different question. Capabilities answers "has
this ever been used here". Neither notices the dangerous case, which is:

    this was working, it is still switched on, and it has produced
    nothing for a long time.

That is what this watches for. It reads the audit trail backwards, because
the trail already records every kind of real activity, and compares the last
success of each enabled feature against how often it ought to be producing
something.

Two distinctions matter, and getting them wrong would make it noise:

  Never used is NOT broken. A feature you've never turned on has no expected
  cadence; that's Capabilities' job, not this one.

  Failing loudly is NOT silence. Something erroring every hour is already
  visible on Health and in the breakers. This is specifically for the case
  where nothing is happening and nothing is complaining.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import config

# How long a feature may go quiet before it's worth mentioning. These are
# generous on purpose: a false alarm teaches you to ignore the panel, which
# costs more than the fault it was meant to catch.
QUIET_DAYS = {
    "trend": 14,        # weekly scan, so a fortnight is genuinely quiet
    "challenge": 14,
    "crew": 21,
    "pipeline": 30,
    "backup": 10,
    "watch": 3,         # watchers run hourly or daily
    "selfimprove": 60,
    "skill": 45,
}

# Which audit kinds count as this feature having actually DONE something.
SUCCESS_KINDS = {
    "trend": ("trend",),
    "challenge": ("challenge",),
    "crew": ("crew",),
    "pipeline": ("pipeline",),
    "backup": ("backup",),
    "selfimprove": ("selfimprove",),
    "skill": ("skill",),
}

FRIENDLY = {
    "trend": "Trend Scout",
    "challenge": "Challenges",
    "crew": "Crew",
    "pipeline": "Pipelines",
    "backup": "Backup",
    "watch": "Watchers",
    "selfimprove": "Self-improvement",
    "skill": "Skills",
}


def _now() -> float:
    return time.time()


def _days(ts) -> float:
    try:
        return (_now() - float(ts)) / 86400.0
    except Exception:
        return 1e9


def _last_success(entries: list, kinds: tuple) -> dict | None:
    """The most recent entry that represents work actually completed.

    Failures are skipped: a feature erroring every hour is not silent, and
    counting its errors as activity would hide exactly the case this exists
    to find."""
    for e in entries:
        if e.get("kind") not in kinds:
            continue
        blob = f"{e.get('name', '')} {e.get('summary', '')}".lower()
        if any(w in blob for w in ("failed", "error", "could not", "denied")):
            continue
        return e
    return None


def _enabled_features(memory) -> dict:
    """What is switched on, and therefore expected to produce something."""
    on = {}
    try:
        rows = memory.conn.execute(
            "SELECT name, action FROM schedules WHERE enabled = 1").fetchall()
        for r in rows:
            act = (r["action"] or "").strip()
            key = {"trendscout": "trend",
                   "watch": "watch", "crew": "crew",
                   "backup": "backup"}.get(act)
            if key:
                on.setdefault(key, f"“{r['name']}” is scheduled")
    except Exception:
        pass
    return on


def why_quiet(feature: str) -> str:
    """Why a feature has produced nothing — not merely that it hasn't.

    "Job scout hasn't produced anything for 5 days, nothing is erroring" is
    a true statement that leaves you to go and find out. For most of these
    the reason is already computable, and a silence report that can't say
    why is half a report.
    """
    try:
        if feature.lower().startswith(("trend", "challenge")):
            from . import challenges
            if not [s for s in challenges.sources() if s.get("on")]:
                return "every source is switched off."
    except Exception:
        pass
    return ""


def report(memory=None, entries: list = None) -> dict:
    """Features that are on, were working, and have gone quiet."""
    if entries is None:
        try:
            from . import audit
            entries = audit.recent(1500) or []
        except Exception:
            entries = []
    on = _enabled_features(memory)
    quiet, healthy = [], []
    for key, why_on in on.items():
        kinds = SUCCESS_KINDS.get(key)
        if not kinds:
            continue                      # nothing in the trail to judge by
        last = _last_success(entries, kinds)
        limit = QUIET_DAYS.get(key, 14)
        name = FRIENDLY.get(key, key)
        if last is None:
            # never produced anything: that's a setup question, not silence,
            # and Capabilities already covers it
            continue
        age = _days(last.get("ts"))
        if age > limit:
            quiet.append({
                "feature": name, "key": key,
                # not merely that it's quiet — why, where that's computable
                "why_quiet": why_quiet(name),
                "days_quiet": round(age, 1),
                "expected_within_days": limit,
                "why_on": why_on,
                "last_success": last.get("iso", ""),
                "last_did": (last.get("summary")
                             or last.get("name") or "")[:140],
                "detail": (f"{name} is on ({why_on}) but hasn't produced "
                           f"anything for {age:.0f} days. It last worked on "
                           f"{last.get('iso', 'an unknown date')}."),
            })
        else:
            healthy.append({"feature": name, "days_quiet": round(age, 1)})
    quiet.sort(key=lambda q: -q["days_quiet"])
    return {"at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "quiet": quiet, "healthy": healthy,
            "watching": len(on),
            "note": ("Only features that are switched on and were working "
                     "before are counted. Something that has never run is a "
                     "setup question, and something failing loudly is already "
                     "on the health board.")}
