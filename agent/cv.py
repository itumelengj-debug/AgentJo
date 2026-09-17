"""CV work — matching, tailoring, and preparing for the interview.

Three things the job pipeline was missing, and they're the ones the
commercial tools charge for: a per-advert view of how well you match, a CV
reordered for the role, and questions you'll actually be asked.

The rule that shapes all of it: **nothing may be invented**. A tailored CV
here reorders, selects and re-emphasises what your profile already contains.
It never adds a skill, a year, or an employer to improve the match — that is
the difference between tailoring and lying, and the second one gets found out
in the interview you worked to get.

The ATS scan is deliberately not an engine call. Applicant tracking systems
match on terms, so the honest way to tell you how you'll score is to do the
same comparison locally: pull the requirements out of the advert, check them
against your profile, and show you exactly which ones are missing. It costs
nothing, runs instantly, and can't hallucinate a match that isn't there.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Terms worth matching on. A general-purpose word list would score "the" and
# "experience"; these are the things a screener actually filters for.
_SKILL_HINT = re.compile(
    r"\b("
    r"python|sql|r|scala|java|c#|javascript|typescript|go|rust|bash|"
    r"power ?bi|tableau|looker|qlik|superset|metabase|excel|dax|mdx|"
    r"spark|databricks|snowflake|bigquery|redshift|synapse|fabric|"
    r"airflow|dbt|dagster|prefect|nifi|ssis|talend|informatica|"
    r"azure|aws|gcp|google cloud|kubernetes|docker|terraform|"
    r"postgres|postgresql|mysql|oracle|sql server|mongodb|cassandra|"
    r"kafka|pubsub|kinesis|event hub|"
    r"etl|elt|data ?warehouse|data ?lake|lakehouse|medallion|star schema|"
    r"dimensional model|data ?governance|data ?quality|lineage|mdm|"
    r"machine learning|ml|mlops|forecasting|regression|classification|"
    r"nlp|llm|genai|rag|pytorch|tensorflow|scikit|pandas|numpy|"
    r"git|ci/cd|devops|agile|scrum|kanban|jira|confluence|"
    r"stakeholder|requirements|business analysis|reporting|dashboard|"
    r"finance|fx|treasury|risk|regulatory|banking|insurance|retail|"
    r"api|rest|graphql|microservice|integration|"
    r"powershell|linux|windows server|sap|salesforce|dynamics|workday"
    r")\b", re.I)

_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:to\s*\d+\s*)?year", re.I)
_DEGREE = re.compile(r"\b(bsc|b\.sc|ba\b|beng|bcom|degree|diploma|honours|"
                     r"masters|msc|mba|phd|matric)\b", re.I)


def _dir() -> Path:
    d = config.AGENT_HOME / "jobscout" / "cv"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def _profile_text(p: dict) -> str:
    """Everything the profile claims, as one searchable blob."""
    parts = []
    for k, v in (p or {}).items():
        parts.append(str(k))
        parts.extend(_as_list(v))
    return " ".join(parts).lower()


def requirements(advert: str) -> dict:
    """Pull the checkable requirements out of an advert.

    Terms only — an ATS is a keyword filter, and pretending otherwise would
    give a score that feels informative and isn't."""
    text = advert or ""
    terms, seen = [], set()
    for m in _SKILL_HINT.finditer(text):
        t = " ".join(m.group(0).lower().split())
        if t not in seen:
            seen.add(t)
            terms.append(t)
    years = [int(m.group(1)) for m in _YEARS.finditer(text)]
    return {"terms": terms,
            "years_required": max(years) if years else None,
            "degree_mentioned": bool(_DEGREE.search(text))}


def ats_scan(role: dict, profile: dict) -> dict:
    """How this advert's requirements line up with what you can evidence."""
    advert = " ".join([str((role or {}).get("title") or ""),
                       str((role or {}).get("summary") or ""),
                       str((role or {}).get("description") or "")])
    req = requirements(advert)
    have = _profile_text(profile)
    matched, missing = [], []
    for t in req["terms"]:
        # "power bi" and "powerbi" are the same requirement to a human and
        # different strings to a naive check
        loose = t.replace(" ", "").replace("-", "")
        if t in have or loose in have.replace(" ", "").replace("-", ""):
            matched.append(t)
        else:
            missing.append(t)
    total = len(req["terms"])
    score = round(100.0 * len(matched) / total) if total else 0

    years_ok = None
    if req["years_required"] is not None:
        mine = 0
        for v in _as_list((profile or {}).get("years_experience")):
            for n in re.findall(r"\d+", v):
                mine = max(mine, int(n))
        years_ok = mine >= req["years_required"]

    advice = []
    if not total:
        advice.append("This advert names no specific tools, so a keyword "
                      "check can't tell you much — read it yourself.")
    if missing:
        advice.append(f"Not evidenced in your profile: {', '.join(missing[:8])}"
                      + ("…" if len(missing) > 8 else "")
                      + ". If you have used them, add them to your profile — "
                        "drafts and this score both read from it.")
    if years_ok is False:
        advice.append(f"It asks for {req['years_required']} years; your "
                      f"profile evidences fewer. Worth addressing directly "
                      f"rather than hoping it isn't noticed.")
    return {"score": score, "matched": matched, "missing": missing,
            "total_terms": total,
            "years_required": req["years_required"], "years_ok": years_ok,
            "degree_mentioned": req["degree_mentioned"],
            "advice": advice,
            "note": ("Keyword coverage only. It tells you what a screener "
                     "will see, not whether you'd do the job well.")}


# --------------------------------------------------------------------------- #
#  tailoring
# --------------------------------------------------------------------------- #
_TAILOR_SYSTEM = (
    "You prepare a CV for one specific advert.\n"
    "ABSOLUTE RULE: use ONLY facts present in the PROFILE. You may reorder, "
    "select, re-emphasise and rephrase. You may NOT add a skill, tool, "
    "employer, qualification, metric or year that the profile does not "
    "contain. If the advert asks for something the candidate lacks, leave it "
    "out — do not imply it.\n"
    "Lead with what this advert asks for. Keep every bullet concrete and "
    "measurable where the profile gives a number.\n"
    "Return ONLY raw JSON: {\"headline\": str, \"summary\": str, "
    "\"key_skills\": [str], \"experience\": [{\"employer\": str, "
    "\"role\": str, \"bullets\": [str]}], \"left_out\": [str]}. "
    "`left_out` names anything the advert wanted that the profile could not "
    "support. No prose, no fences.")


def tailor_cv(role: dict, profile: dict, brain, model=None) -> dict:
    """A CV ordered for this advert, built only from the profile."""
    payload = json.dumps({"ADVERT": {k: (role or {}).get(k) for k in
                                     ("title", "company", "summary",
                                      "description", "location")},
                          "PROFILE": profile}, default=str)[:12000]
    kw = {"model": model} if model else {}
    try:
        resp = brain.chat([{"role": "user", "content": payload}],
                          [_TAILOR_SYSTEM], None, **kw)
        text = "\n".join(b.text for b in resp.content
                         if getattr(b, "type", "") == "text").strip()
        data = _json_from(text)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    cv = {
        "headline": str(data.get("headline", ""))[:160],
        "summary": str(data.get("summary", ""))[:900],
        "key_skills": _as_list(data.get("key_skills"))[:18],
        "experience": data.get("experience") if isinstance(
            data.get("experience"), list) else [],
        "left_out": _as_list(data.get("left_out"))[:10],
        "at": _iso(),
    }
    cv["check"] = verify_cv(cv, profile)
    return {"ok": True, "cv": cv}


def _json_from(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", (text or "").strip(),
                  flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("the engine returned no JSON")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("the engine returned truncated JSON")


def verify_cv(cv: dict, profile: dict) -> dict:
    """Check the tailored CV invents nothing.

    The same guard the cover letters get. A CV is the document most likely to
    be checked against you, so it gets the strictest reading."""
    have = _profile_text(profile)
    have_loose = have.replace(" ", "").replace("-", "")
    problems = []

    def _flag(kind, term, where):
        problems.append({"kind": kind, "term": term, "where": where,
                         "detail": f"“{term}” appears in {where} but isn't "
                                   f"in your profile"})

    blob_parts = [cv.get("headline", ""), cv.get("summary", "")]
    blob_parts += cv.get("key_skills") or []
    for job in cv.get("experience") or []:
        blob_parts += [str(job.get("employer", "")), str(job.get("role", ""))]
        blob_parts += [str(b) for b in (job.get("bullets") or [])]
    blob = " ".join(str(x) for x in blob_parts)

    for m in _SKILL_HINT.finditer(blob):
        t = " ".join(m.group(0).lower().split())
        if t not in have and t.replace(" ", "") not in have_loose:
            _flag("tool", m.group(0), "the CV")

    for m in _YEARS.finditer(blob):
        n = m.group(1)
        if n not in have:
            _flag("years", m.group(0).strip(), "the CV")

    for job in cv.get("experience") or []:
        emp = str(job.get("employer", "")).strip()
        if emp and emp.lower() not in have:
            _flag("employer", emp, "the experience section")

    seen, unique = set(), []
    for p in problems:
        k = p["term"].lower()
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return {"ok": not unique, "problems": unique[:20]}


def render_cv(cv: dict, profile: dict) -> str:
    """A plain-text CV — pasteable into any form, readable by any ATS."""
    p = profile or {}
    lines = []
    name = str(p.get("full_name") or "").strip()
    if name:
        lines.append(name)
    contact = " · ".join([str(p.get(k) or "").strip() for k in
                          ("email", "phone", "location", "linkedin")
                          if str(p.get(k) or "").strip()])
    if contact:
        lines.append(contact)
    if cv.get("headline"):
        lines += ["", cv["headline"]]
    if cv.get("summary"):
        lines += ["", "SUMMARY", cv["summary"]]
    if cv.get("key_skills"):
        lines += ["", "KEY SKILLS", ", ".join(cv["key_skills"])]
    if cv.get("experience"):
        lines += ["", "EXPERIENCE"]
        for job in cv["experience"]:
            head = " — ".join(x for x in (str(job.get("role", "")).strip(),
                                          str(job.get("employer", "")).strip())
                              if x)
            lines.append(head)
            for b in (job.get("bullets") or []):
                lines.append(f"  - {b}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_cv(role: dict, cv: dict, profile: dict) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-",
                  f"{role.get('company','')}-{role.get('title','')}".lower()
                  ).strip("-")[:60] or "role"
    path = _dir() / f"cv-{slug}.txt"
    path.write_text(render_cv(cv, profile), "utf-8")
    return {"ok": True, "path": str(path)}


# --------------------------------------------------------------------------- #
#  interview preparation
# --------------------------------------------------------------------------- #
_INTERVIEW_SYSTEM = (
    "Prepare someone for an interview for this specific advert.\n"
    "Return 6 to 8 questions they are genuinely likely to be asked, drawn "
    "from what the advert emphasises. For each, give the strongest honest "
    "answer AVAILABLE IN THEIR PROFILE — cite the specific experience they "
    "would draw on. Where the profile has nothing to draw on, say so plainly "
    "in `gap` rather than inventing an answer; that is the question they need "
    "to think about beforehand.\n"
    "Return ONLY raw JSON: {\"questions\": [{\"question\": str, "
    "\"answer_from_profile\": str, \"gap\": str}], \"ask_them\": [str]}. "
    "`ask_them` is 3 questions worth asking the interviewer. No prose.")


def interview_prep(role: dict, profile: dict, brain, model=None) -> dict:
    payload = json.dumps({"ADVERT": {k: (role or {}).get(k) for k in
                                     ("title", "company", "summary",
                                      "description")},
                          "PROFILE": profile}, default=str)[:12000]
    kw = {"model": model} if model else {}
    try:
        resp = brain.chat([{"role": "user", "content": payload}],
                          [_INTERVIEW_SYSTEM], None, **kw)
        text = "\n".join(b.text for b in resp.content
                         if getattr(b, "type", "") == "text").strip()
        data = _json_from(text)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    qs = []
    for q in (data.get("questions") or [])[:10]:
        if not isinstance(q, dict) or not q.get("question"):
            continue
        qs.append({"question": str(q["question"])[:300],
                   "answer_from_profile": str(
                       q.get("answer_from_profile", ""))[:900],
                   "gap": str(q.get("gap", ""))[:300]})
    return {"ok": True, "at": _iso(), "questions": qs,
            "ask_them": _as_list(data.get("ask_them"))[:5],
            "gaps": [q["gap"] for q in qs if q["gap"].strip()]}
