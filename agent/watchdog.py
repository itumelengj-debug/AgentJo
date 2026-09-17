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
    "jobscout": 4,      # auto-apply on: something should be found or sent
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
    "jobscout": ("jobscout",),
    "trend": ("trend",),
    "challenge": ("challenge",),
    "crew": ("crew",),
    "pipeline": ("pipeline",),
    "backup": ("backup",),
    "selfimprove": ("selfimprove",),
    "skill": ("skill",),
}

FRIENDLY = {
    "jobscout": "Job scout",
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
        from . import jobscout
        if jobscout.auto_config().get("enabled"):
            on["jobscout"] = "auto-apply is on"
    except Exception:
        pass
    try:
        rows = memory.conn.execute(
            "SELECT name, action FROM schedules WHERE enabled = 1").fetchall()
        for r in rows:
            act = (r["action"] or "").strip()
            key = {"trendscout": "trend", "jobscout": "jobscout",
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
        if feature.lower().startswith("job"):
            from . import jobscout
            p = jobscout.auto_preview()
            if not p.get("enabled"):
                return "auto-apply is switched off, so it isn't meant to."
            # order matters: "no sources" is true of a fresh install, but
            # if there are roles in the list it is not the reason anything
            # stopped — the specific cause has to win over the general one
            if not jobscout.roles() and not jobscout.job_sources():
                return ("it has no job sites configured — Sites & filters "
                        "\u2192 Find sites for me.")
            if not p.get("would_send") and p.get("would_hold"):
                return (f"every candidate is held: {len(p['would_hold'])} "
                        f"draft(s) claim things your profile can't support. "
                        f"Held drafts shows which.")
            if not p.get("would_send") and p.get("needs_scoring"):
                return (f"{len(p['needs_scoring'])} role(s) are waiting to be "
                        f"scored — that needs an engine with credit, or a "
                        f"local one pinned in the Jobs panel.")
            if not p.get("would_send") and p.get("no_address"):
                return (f"{len(p['no_address'])} role(s) are portal-only, so "
                        f"there is nothing it can send by itself.")
            if not p.get("would_send"):
                return ("there are no roles left to act on — the list is "
                        "empty or everything has been applied to.")
            if p.get("dry_run"):
                return (f"it would send {len(p['would_send'])}, but rehearsal "
                        f"is on so nothing leaves. Turn rehearsal off in "
                        f"Auto-apply when you're ready.")
            return (f"it has {len(p['would_send'])} ready to send — if that "
                    f"hasn't happened, the daily schedule may not be running.")
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
