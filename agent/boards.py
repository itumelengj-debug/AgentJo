"""Boards — finding job sites worth watching, and proving they work.

Adding sources by hand means knowing which boards exist, which suit your
field, and which will actually give a machine anything to read. Two of those
are research and the third is only discoverable by trying.

So this matches a catalogue against your profile, then **fetches each
candidate and checks it really returns roles** before adding it. A suggestion
that 403s or renders its list in JavaScript is worse than no suggestion: it
sits in your sources failing quietly for weeks. Everything here is verified or
reported as failed, with the reason.

On "which I qualify for" — that phrase does a lot of work and deserves an
honest answer. This cannot check your right to work anywhere. What it can do
is match on the two things that are knowable: boards serving a region you have
said you can work in, and boards that are remote-worldwide, where the
employer's constraint is usually timezone rather than nationality. A board
advertising onsite roles in a country you have no visa for is filtered out not
because the app knows your immigration status, but because you told it where
you can work.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import config

WORLD = "worldwide"

# name, url, kind, regions it serves, what it covers.
# Kept deliberately small and checkable rather than exhaustive — a long list
# of boards that don't parse is worse than a short list that does.
CATALOGUE = [
    # --- remote, open to anyone ---------------------------------------
    {"name": "Remotive", "kind": "remotive",
     "url": "https://remotive.com/api/remote-jobs?limit=80",
     "regions": [WORLD], "focus": ["software", "data", "product"],
     "about": "Remote roles, worldwide, with a clean API"},
    {"name": "RemoteOK", "kind": "remoteok", "url": "https://remoteok.com/api",
     "regions": [WORLD], "focus": ["software", "data", "design"],
     "about": "Remote roles, worldwide"},
    {"name": "We Work Remotely — programming", "kind": "rss",
     "url": "https://weworkremotely.com/categories/"
            "remote-programming-jobs.rss",
     "regions": [WORLD], "focus": ["software", "data"],
     "about": "Remote engineering roles"},
    {"name": "We Work Remotely — devops/sysadmin", "kind": "rss",
     "url": "https://weworkremotely.com/categories/"
            "remote-devops-sysadmin-jobs.rss",
     "regions": [WORLD], "focus": ["devops", "cloud", "software"],
     "about": "Remote infrastructure roles"},
    {"name": "Himalayas", "kind": "html",
     "url": "https://himalayas.app/jobs/countries/south-africa",
     "regions": [WORLD, "south africa"], "focus": ["software", "data"],
     "about": "Remote roles open to South Africa"},
    {"name": "Working Nomads — development", "kind": "rss",
     "url": "https://www.workingnomads.com/jobsrss/development",
     "regions": [WORLD], "focus": ["software", "data"],
     "about": "Remote development roles"},
    {"name": "Jobicy — data science", "kind": "rss",
     "url": "https://jobicy.com/?feed=job_feed&job_categories=data-science",
     "regions": [WORLD], "focus": ["data", "analytics", "ml"],
     "about": "Remote data roles"},
    {"name": "Jobspresso", "kind": "rss",
     "url": "https://jobspresso.co/?feed=job_feed",
     "regions": [WORLD], "focus": ["software", "data", "marketing"],
     "about": "Curated remote roles"},

    # --- contract and freelance ----------------------------------------
    {"name": "Wellfound (AngelList)", "kind": "html",
     "url": "https://wellfound.com/role/r/data-engineer",
     "regions": [WORLD], "focus": ["software", "data", "startup"],
     "about": "Startup roles, many remote"},

    # --- South Africa ---------------------------------------------------
    {"name": "Careers24 — IT", "kind": "html",
     "url": "https://www.careers24.com/jobs/kw-information-technology/",
     "regions": ["south africa"], "focus": ["software", "data", "it"],
     "about": "South African IT roles"},
    {"name": "CareerJunction — IT", "kind": "html",
     "url": "https://www.careerjunction.co.za/jobs/it",
     "regions": ["south africa"], "focus": ["software", "data", "it"],
     "about": "South African IT roles"},
    {"name": "PNet — IT", "kind": "html",
     "url": "https://www.pnet.co.za/jobs/information-technology",
     "regions": ["south africa"], "focus": ["software", "data", "it"],
     "about": "South African IT roles"},
    {"name": "OfferZen", "kind": "html",
     "url": "https://www.offerzen.com/jobs",
     "regions": ["south africa", "europe"], "focus": ["software", "data"],
     "about": "Developer roles, South Africa and Europe"},

    # --- Europe / UK, useful when remote-friendly -----------------------
    {"name": "EU Remote Jobs", "kind": "rss",
     "url": "https://euremotejobs.com/job-region/remote-jobs-europe/feed/",
     "regions": ["europe", WORLD], "focus": ["software", "data"],
     "about": "Remote roles across Europe"},
]

# What a profile's tools imply about the kind of board that suits it.
FOCUS_HINTS = {
    "data": ("sql", "power bi", "tableau", "dbt", "airflow", "spark",
             "databricks", "snowflake", "bigquery", "etl", "warehouse",
             "analytics", "bi", "pipeline", "medallion"),
    "ml": ("machine learning", "pytorch", "tensorflow", "scikit", "llm",
           "nlp", "forecasting", "mlops"),
    "software": ("python", "java", "typescript", "javascript", "go", "rust",
                 "c#", "api", "backend", "frontend", "django", "fastapi"),
    "devops": ("kubernetes", "docker", "terraform", "ci/cd", "devops",
               "ansible", "jenkins"),
    "cloud": ("azure", "aws", "gcp", "cloud"),
    "analytics": ("dashboard", "reporting", "power bi", "looker", "qlik"),
    "it": ("support", "helpdesk", "administrator", "windows server"),
}


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None]
    if isinstance(v, dict):
        return [f"{k}: {x}" for k, x in v.items()]
    return [str(v)]


def profile_focus(p: dict) -> list:
    """What this person actually does, from what they list."""
    blob = " ".join(_as_list(p.get("skills"))
                    + _as_list(p.get("technologies"))
                    + _as_list(p.get("target_roles"))
                    + [str(p.get("headline") or ""),
                       str(p.get("summary") or "")]).lower()
    hits = []
    for focus, words in FOCUS_HINTS.items():
        n = sum(1 for w in words if w in blob)
        if n:
            hits.append((n, focus))
    hits.sort(reverse=True)
    return [f for _, f in hits[:4]]


def profile_regions(p: dict) -> list:
    """Where this person has said they can work.

    Not a judgement about visas — it is what they wrote in `locations_ok`,
    plus remote-worldwide, which is the case where an employer's constraint
    is usually a timezone rather than a passport."""
    out = [WORLD]
    for loc in _as_list(p.get("locations_ok")) + [str(p.get("location") or "")]:
        loc = loc.strip().lower()
        if not loc or loc == "remote":
            continue
        out.append(loc)
        # a city implies its country when the profile names one
        if "johannesburg" in loc or "cape town" in loc or "pretoria" in loc:
            out.append("south africa")
    return list(dict.fromkeys(out))


def suggest(p: dict = None, include_added: bool = False) -> list:
    """Boards worth watching, ranked, with the reason for each."""
    from . import jobscout
    p = p if p is not None else jobscout.profile()
    focus = profile_focus(p)
    regions = profile_regions(p)
    have = {str(s.get("url", "")).rstrip("/")
            for s in jobscout.job_sources()}
    out = []
    for b in CATALOGUE:
        added = b["url"].rstrip("/") in have
        if added and not include_added:
            continue
        region_hit = [r for r in b["regions"] if r in regions]
        if not region_hit:
            continue                      # somewhere you can't work
        focus_hit = [f for f in b["focus"] if f in focus]
        score = len(focus_hit) * 10 + (5 if WORLD in region_hit else 8)
        why = []
        if focus_hit:
            why.append("matches your " + ", ".join(focus_hit) + " work")
        if WORLD in region_hit:
            why.append("remote and open worldwide")
        else:
            why.append("serves " + ", ".join(r for r in region_hit
                                             if r != WORLD))
        out.append({**b, "score": score, "added": added,
                    "why": "; ".join(why)})
    out.sort(key=lambda b: -b["score"])
    return out


# --------------------------------------------------------------------------- #
#  proving a board works before adding it
# --------------------------------------------------------------------------- #
def validate(url: str, kind: str) -> dict:
    """Fetch it and check it really yields roles.

    This is the part that can't be researched — a board either gives a machine
    something to read or it doesn't, and the only way to know is to try."""
    from . import jobscout
    try:
        text = jobscout._fetch_source(url)
    except Exception as exc:
        # a refusal is often just headers; the browser path is the fallback
        try:
            text = jobscout.fetch_with_browser(url)
        except Exception:
            return {"ok": False,
                    "why": jobscout._explain_fetch_error(
                        f"{type(exc).__name__}: {exc}", url)}
    parser = jobscout._PARSERS.get(kind, jobscout._parse_jobs_rss)
    try:
        jobscout._CURRENT_SOURCE_URL["url"] = url
        items = [j for j in parser(text) if j.get("title")]
    except Exception as exc:
        return {"ok": False,
                "why": f"read the page but couldn't parse it "
                       f"({type(exc).__name__})"}
    real = [j for j in items
            if not jobscout._is_category_link(j.get("title", ""),
                                              j.get("company", ""))]
    if not real:
        return {"ok": False, "found": len(items),
                "why": ("nothing job-shaped came back — the list is probably "
                        "built in the browser after loading")}
    return {"ok": True, "found": len(real),
            "sample": [j["title"] for j in real[:3]]}


def auto_add(p: dict = None, limit: int = 5) -> dict:
    """Suggest, verify, and add only what actually works."""
    from . import jobscout
    p = p if p is not None else jobscout.profile()
    if not (_as_list(p.get("skills")) or _as_list(p.get("technologies"))
            or _as_list(p.get("target_roles"))):
        return {"ok": False,
                "error": ("Your profile doesn't say what you do yet, so "
                          "there's nothing to match boards against. Ask in "
                          "chat: “build my job profile from my CV”.")}
    added, rejected = [], []
    # Keep going until enough boards work, rather than stopping after a fixed
    # number of candidates: giving up at six when the seventh would have
    # worked is exactly the unhelpful behaviour this is meant to replace.
    # Bounded at 14 so a run can't take all afternoon.
    MAX_TRIED = 14
    tried = 0
    for b in suggest(p):
        if len(added) >= limit or tried >= MAX_TRIED:
            break
        tried += 1
        v = validate(b["url"], b["kind"])
        if not v.get("ok"):
            rejected.append({"name": b["name"], "why": v.get("why", "")})
            continue
        r = jobscout.add_job_source(b["name"], b["url"], b["kind"])
        if r.get("ok"):
            added.append({"name": b["name"], "why": b["why"],
                          "found": v.get("found"),
                          "sample": v.get("sample", [])})
        else:
            rejected.append({"name": b["name"], "why": r.get("error", "")})
    _audit("boards", f"{len(added)} added, {len(rejected)} rejected")
    return {"ok": True, "added": added, "rejected": rejected,
            "tried": tried, "at": _iso(),
            "note": ("Only boards that actually returned roles were added. "
                     "Work authorisation isn't something this can check — "
                     "matching is on where you said you can work, plus "
                     "remote-worldwide.")}


def _audit(name: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("jobscout", name=name, summary=summary[:200])
    except Exception:
        pass
