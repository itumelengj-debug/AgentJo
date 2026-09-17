"""Job scout — find remote contract work, and apply as yourself.

The loop: keep a PROFILE of what you actually are and want; SCAN sources for
remote contracting roles; SCORE each honestly (including the reasons against);
DRAFT tailored application material; you REVIEW and send; TRACK what happened
and when to follow up.

Three design decisions worth stating, because they're what make this useful
rather than dangerous:

  GROUNDED, NOT INVENTED. The drafter may only use facts from your profile.
  Every draft then goes through a fabrication check that scans for specifics
  it can't source — technologies, employers, year-counts, certifications —
  and flags them. An application that quietly claims five years of something
  you've touched twice is worse than no application: it gets found out in
  the interview, and it is your name on it.

  YOU SEND, NOT THE AGENT. Everything up to the send is automated. The send
  is yours. Not a rule for its own sake — an autonomous sender that gets one
  fact wrong has misrepresented you to a real employer under your name, and
  there is no unsend. The cost of a wrong send is asymmetric, so the human
  goes exactly there. Same pattern as self-improve and trend adoption.

  HONEST SCORING. A scout that says every role is a great fit is a scout you
  stop reading. Fit scores carry their reasons AGAINST, and roles you don't
  qualify for are marked as such rather than quietly padded.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

MAX_ROLES = 25
STAGES = ["found", "drafted", "applied", "responded", "interview",
          "offer", "closed"]


def _dir() -> Path:
    d = config.AGENT_HOME / "jobscout"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


# --------------------------------------------------------------------------- #
#  profile — the only source of truth a draft may draw on
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = {
    # Portal forms ask for these every time; an email application never did,
    # which is why they weren't here before.
    "full_name": "",
    "phone": "",
    "location": "",
    "linkedin": "",
    "github": "",
    "website": "",
    "cv_path": "",
    "headline": "",
    "summary": "",
    "skills": [],
    "technologies": [],
    "employers": [],
    "achievements": [],
    "certifications": [],
    "years_experience": {},
    "target_roles": ["Data Engineer", "BI Consultant", "AI Engineer"],
    "arrangement": "remote contract",
    "rate": "",
    "availability": "",
    "locations_ok": ["Remote", "South Africa"],
    "must_haves": [],
    "deal_breakers": [],
    "voice_notes": ("Write plainly and specifically. Short sentences. No "
                    "buzzwords, no 'passionate about', no superlatives. Lead "
                    "with a concrete thing built and what it changed."),
}


def _profile_path() -> Path:
    return _dir() / "profile.json"


def profile() -> dict:
    try:
        p = json.loads(_profile_path().read_text("utf-8"))
        return {**DEFAULT_PROFILE, **p}
    except Exception:
        return dict(DEFAULT_PROFILE)


def save_profile(p: dict) -> dict:
    merged = {**profile(), **(p or {})}
    _profile_path().write_text(json.dumps(merged, indent=2), "utf-8")
    return merged


def profile_ready() -> tuple[bool, str]:
    p = profile()
    if not (p.get("summary") or p.get("achievements")):
        return False, ("Your profile is empty. Fill it in first (or ask the "
                       "agent to build it from your CV) — drafts are only "
                       "allowed to use what's in there.")
    return True, ""


# --------------------------------------------------------------------------- #
#  roles
# --------------------------------------------------------------------------- #
def _roles_path() -> Path:
    return _dir() / "roles.json"


def roles() -> list:
    try:
        return json.loads(_roles_path().read_text("utf-8"))
    except Exception:
        return []


def save_roles(rs: list) -> None:
    _roles_path().write_text(json.dumps(rs, indent=2), "utf-8")


def _looks_like_a_real_role(job: dict) -> bool:
    """A vacancy has a title AND something to identify it by. Without a
    company or a link there is nothing to apply to, and drafting against it
    produces a letter addressed to nobody."""
    title = " ".join(str(job.get("title") or "").split())
    if len(title) < 4 or _is_category_link(title, job.get("company", "")):
        return False
    return bool(str(job.get("company") or "").strip()
                or str(job.get("url") or "").strip())


def add_roles(found: list) -> dict:
    """Merge newly-found roles.

    Skips ones already tracked, and ones the user has removed — otherwise
    deleting a role you've decided against just means seeing it again after
    the next scan."""
    have = {r.get("key") for r in roles()}
    have |= {k.lower() for k in ignored_keys()}
    rs = roles()
    added = 0
    for f in found or []:
        title = _as_text(f.get("title")).strip()
        company = _as_text(f.get("company")).strip()
        if not title:
            continue
        # last line of defence: a category link or a title with nothing to
        # apply to must never become a tracked role, whatever route it took
        if not _looks_like_a_real_role({**f, "title": title,
                                        "company": company}):
            continue
        key = _slug(f"{company}-{title}")
        if key in have or key.lower() in have:
            continue
        rs.append({
            "key": key, "title": title[:160], "company": company[:120],
            "url": _as_text(f.get("url"))[:500],
            "location": _as_text(f.get("location"))[:120],
            "summary": _as_text(f.get("summary"))[:600],
            "source": _as_text(f.get("source"))[:60],
            "apply_email": _as_text(f.get("apply_email")
                                    or f.get("email"))[:200],
            "found_at": _iso(), "stage": "found",
            "fit": None, "draft": None, "events": [],
        })
        have.add(key)
        added += 1
    save_roles(rs[-200:])
    _audit("found", f"{added} new role(s)")
    return {"ok": True, "added": added, "total": len(roles())}


def get_role(key: str) -> dict | None:
    for r in roles():
        if r["key"] == key:
            return r
    return None


def update_role(key: str, **fields) -> dict:
    rs = roles()
    for i, r in enumerate(rs):
        if r["key"] == key:
            rs[i] = {**r, **fields}
            save_roles(rs)
            return {"ok": True, "role": rs[i]}
    return {"ok": False, "error": "no such role"}


def set_stage(key: str, stage: str, note: str = "") -> dict:
    if stage not in STAGES:
        return {"ok": False, "error": f"stage must be one of {STAGES}"}
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    events = list(r.get("events") or [])
    events.append({"at": _iso(), "stage": stage, "note": note[:300]})
    res = update_role(key, stage=stage, events=events,
                      stage_at=_iso())
    _audit("stage", f"{r['title']} @ {r.get('company', '')} → {stage}")
    return res


def follow_ups(days: int = 7) -> list:
    """Applications that have gone quiet and are worth a nudge."""
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for r in roles():
        if r.get("stage") != "applied":
            continue
        try:
            when = datetime.strptime(r.get("stage_at", ""), "%Y-%m-%d %H:%M UTC")
            when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if when < cutoff:
            out.append({**r, "silent_days": (
                datetime.now(timezone.utc) - when).days})
    return out


# --------------------------------------------------------------------------- #
#  fabrication guard — the safeguard that makes autonomy safe
# --------------------------------------------------------------------------- #
_CLAIM_YEARS = re.compile(r"(\d+)\+?\s*(?:years?|yrs?)", re.I)

# Proper-noun detection misses lower-case tool names — and "dbt", "terraform"
# and friends are exactly what a draft is tempted to claim. A modest lexicon
# of the tools that actually come up in data/BI/AI contracting closes the gap
# the capitalisation rule leaves open.
_TECH_LEXICON = {
    "dbt", "airflow", "dagster", "prefect", "spark", "kafka", "flink",
    "snowflake", "databricks", "redshift", "bigquery", "synapse", "fabric",
    "terraform", "ansible", "kubernetes", "k8s", "docker", "helm",
    "postgres", "postgresql", "mysql", "mongodb", "cassandra", "clickhouse",
    "duckdb", "sqlite", "oracle", "sap", "salesforce", "tableau", "looker",
    "qlik", "superset", "metabase", "pandas", "numpy", "pytorch",
    "tensorflow", "sklearn", "langchain", "llamaindex", "pinecone", "faiss",
    "kubeflow", "mlflow", "sagemaker", "vertex", "bedrock", "azure", "aws",
    "gcp", "django", "flask", "fastapi", "react", "angular", "kotlin",
    "scala", "rust", "golang", "graphql", "grafana", "prometheus", "splunk",
    "informatica", "talend", "matillion", "fivetran", "stitch",
}
_STOP = {
    "I", "My", "The", "A", "An", "We", "Our", "This", "That", "As", "At",
    "In", "On", "For", "With", "And", "But", "You", "Your", "It", "If",
    "When", "While", "From", "To", "By", "Of", "Is", "Are", "Was", "Were",
    "Remote", "Contract", "Dear", "Hi", "Hello", "Regards", "Kind", "Best",
    "Sincerely", "Thanks", "Thank", "Team", "Role", "Position", "Company",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "January",
    "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}


def _profile_corpus(p: dict, role: dict | None = None) -> str:
    bits = [_as_text(p.get("summary")), _as_text(p.get("headline")),
            " ".join(_as_list(p.get("skills"))),
            " ".join(_as_list(p.get("technologies"))),
            " ".join(_as_list(p.get("employers"))),
            " ".join(_as_list(p.get("achievements"))),
            " ".join(_as_list(p.get("certifications"))),
            json.dumps(p.get("years_experience") or {})]
    if role:
        # terms lifted from the advert itself are fair to echo back
        bits += [_as_text(role.get("title")), _as_text(role.get("company")),
                 _as_text(role.get("summary"))]
    return " ".join(bits).lower()


def check_draft(text: str, role: dict | None = None) -> dict:
    """Flag specifics the draft asserts that the profile can't support.

    Deliberately conservative: it reports SUSPECTED unsupported claims for a
    human to glance at, rather than silently rewriting. A false flag costs
    you two seconds; a missed fabrication costs you an interview."""
    p = profile()
    corpus = _profile_corpus(p, role)
    text = _as_text(text)
    problems = []

    for m in _CLAIM_YEARS.finditer(text):
        span = text[max(0, m.start() - 60):m.end() + 20]
        n = m.group(1)
        stated = json.dumps(p.get("years_experience") or {})
        if n not in stated:
            problems.append({
                "kind": "year-claim",
                "detail": f"claims “{m.group(0)}” — not in your profile's "
                          f"years_experience",
                "context": span.strip()[:140]})

    # proper nouns / product names that don't appear anywhere in the profile
    for token in set(re.findall(r"\b([A-Z][A-Za-z0-9+.#/-]{2,})\b", text)):
        if token in _STOP or token.lower() in corpus:
            continue
        if len(token) < 3:
            continue
        problems.append({
            "kind": "unsourced-term",
            "detail": f"mentions “{token}”, which isn't in your profile or "
                      f"the advert",
            "context": ""})

    low = text.lower()
    for term in _TECH_LEXICON:
        if re.search(rf"\b{re.escape(term)}\b", low) and term not in corpus:
            problems.append({
                "kind": "unsourced-tool",
                "detail": f"mentions “{term}”, which isn't in your profile or "
                          f"the advert",
                "context": ""})

    seen, unique = set(), []
    for pr in problems:
        k = (pr["kind"], pr["detail"])
        if k not in seen:
            seen.add(k)
            unique.append(pr)
    return {"ok": not unique, "problems": unique[:12],
            "checked_chars": len(text)}


# --------------------------------------------------------------------------- #
#  scoring + drafting (engine-backed, strictly grounded)
# --------------------------------------------------------------------------- #
_SCORE_SYSTEM = (
    "You assess how well ONE candidate fits ONE role. Be blunt and useful, "
    "not encouraging. Return ONLY raw JSON: {\"score\": 0-100, \"verdict\": "
    "\"strong\"|\"possible\"|\"weak\", \"for\": [reasons this fits], "
    "\"against\": [honest reasons it may not — gaps, seniority mismatch, "
    "domain mismatch, red flags in the advert], \"missing\": [requirements "
    "the candidate does NOT demonstrably meet]}. If the candidate plainly "
    "doesn't meet the core requirement, say so with a low score — a padded "
    "score wastes their time.")

_DRAFT_SYSTEM = (
    "You draft a short application message for a candidate, in THEIR voice.\n"
    "ABSOLUTE RULE: use only facts present in the PROFILE. Never invent an "
    "employer, technology, certification, metric or number of years. If the "
    "role wants something the profile doesn't show, either omit it or state "
    "plainly what the candidate has done that is adjacent — never imply "
    "experience they lack.\n"
    "Write plainly: short sentences, concrete specifics, no buzzwords, no "
    "'passionate about', no superlatives, no filler openings. Lead with one "
    "real thing they built and what changed because of it. 150-220 words.\n"
    "Return ONLY raw JSON: {\"subject\": str, \"body\": str, \"gaps\": "
    "[anything the role asked for that you could NOT support from the "
    "profile]}.")


def _json_from(resp) -> dict:
    text = "\n".join(b.text for b in resp.content
                     if getattr(b, "type", "") == "text").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise ValueError(f"engine returned no JSON: {text[:200]}")
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
        raise ValueError("engine returned truncated JSON")


def score_role(key: str, brain, model=None) -> dict:
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    ok, why = profile_ready()
    if not ok:
        return {"ok": False, "error": why}
    payload = json.dumps({"profile": profile(), "role": r}, default=str)[:12000]
    try:
        kw = {"model": model} if model else {}
        data = _json_from(brain.chat([{"role": "user", "content": payload}],
                                     [_SCORE_SYSTEM], None, **kw))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    fit = {"score": int(data.get("score") or 0),
           "verdict": _as_text(data.get("verdict")) or "possible",
           "for": _as_list(data.get("for"))[:6],
           "against": _as_list(data.get("against"))[:6],
           "missing": _as_list(data.get("missing"))[:6],
           "at": _iso()}
    update_role(key, fit=fit)
    return {"ok": True, "fit": fit}


def _banned_clause() -> str:
    """A dismissed claim must not reappear in the next draft, or the same
    question gets asked forever."""
    b = banned_claims()
    if not b:
        return ""
    return ("\nNEVER mention any of these, in any form — the candidate has "
            "explicitly said they do not claim them: " + ", ".join(b) + ".")


def draft_application(key: str, brain, model=None) -> dict:
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    ok, why = profile_ready()
    if not ok:
        return {"ok": False, "error": why}
    p = profile()
    payload = json.dumps({"PROFILE": p, "ROLE": r,
                          "VOICE": p.get("voice_notes", "")},
                         default=str)[:12000]
    try:
        kw = {"model": model} if model else {}
        data = _json_from(brain.chat([{"role": "user", "content": payload}],
                                     [_DRAFT_SYSTEM + _banned_clause()],
                                     None, **kw))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    body = _as_text(data.get("body"))
    check = check_draft(body, r)
    draft = {"subject": _as_text(data.get("subject"))[:200],
             "body": body,
             "gaps": _as_list(data.get("gaps"))[:8],
             "check": check, "at": _iso()}
    update_role(key, draft=draft, stage="drafted")
    _audit("draft", f"{r['title']} @ {r.get('company', '')}"
                    + ("" if check["ok"]
                       else f" — {len(check['problems'])} claim(s) to verify"))
    return {"ok": True, "draft": draft}


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return "\n".join(_as_text(x) for x in v if x is not None)
    if isinstance(v, dict):
        return "\n".join(f"{k}: {_as_text(x)}" for k, x in v.items())
    return str(v)


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [_as_text(x) for x in v if x is not None]
    return [_as_text(v)]


def summary() -> dict:
    rs = roles()
    by_stage = {s: 0 for s in STAGES}
    for r in rs:
        by_stage[r.get("stage", "found")] = by_stage.get(
            r.get("stage", "found"), 0) + 1
    return {"total": len(rs), "by_stage": by_stage,
            "follow_ups": len(follow_ups()),
            "profile_ready": profile_ready()[0]}


# --------------------------------------------------------------------------- #
#  auto-apply — fully automated, with the gates that make that survivable
# --------------------------------------------------------------------------- #
AUTO_DEFAULTS = {
    "enabled": False,
    "dry_run": True,        # starts in rehearsal: prepares and logs, sends
                            # nothing, so you can read a week of what it WOULD
                            # have sent before trusting it with your name
    "min_score": 75,        # below this it waits for you
    "require_clean_check": True,   # a draft with unsourced claims never auto-sends
    "daily_cap": 5,         # applications are not a numbers game; a cap also
                            # bounds the blast radius of any mistake
    "signature": "",
}


def auto_config() -> dict:
    return {**AUTO_DEFAULTS, **(load_config().get("auto") or {})}


def save_auto_config(cfg: dict) -> dict:
    base = load_config()
    base["auto"] = {**auto_config(), **(cfg or {})}
    save_config(base)
    return base["auto"]


def _explain_engine_error(err: str) -> str:
    """Say what actually went wrong.

    These were truncated to 80 characters, which cut every message off at
    exactly the point the reason began — a dozen identical lines reading
    "BadRequestError: Error code: 400 - {'type': 'error', 'error':
    {'type': 'invalid_" told nobody anything."""
    e = " ".join(str(err or "").split())
    low = e.lower()
    if "credit balance" in low or "billing" in low:
        return ("that engine has no credit. Pick a local engine in the Jobs "
                "panel — scoring and drafting then cost nothing.")
    if "rate" in low and "limit" in low:
        return "rate limited by the provider; it will work again shortly."
    if "401" in low or "unauthor" in low or "invalid x-api-key" in low:
        return "the API key was rejected — check it in Settings."
    if "connect" in low or "refused" in low or "timeout" in low:
        return ("could not reach the engine. If it's local, check Ollama is "
                "running.")
    if "text content blocks must be non-empty" in low:
        return "the advert had no text to score."
    if "max_tokens" in low or "too long" in low or "context" in low:
        return "the advert was too long for that engine's context."
    return e[:240]


def _sent_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for r in roles()
               if r.get("sent_at", "").startswith(today))


def _gate(r: dict, cfg: dict) -> str:
    """Return '' if this role may be auto-applied to, else why it can't."""
    if not _as_text(r.get("apply_email")).strip():
        return ("no application email on the advert — portal applications "
                "still need you")
    fit = r.get("fit") or {}
    if fit.get("score") is None:
        return "not scored yet"
    if int(fit["score"]) < int(cfg["min_score"]):
        return f"fit {fit['score']} is below your threshold {cfg['min_score']}"
    d = r.get("draft") or {}
    if not d.get("body"):
        return "no draft yet"
    chk = d.get("check") or {}
    if cfg.get("require_clean_check", True) and chk.get("ok") is False:
        n = len(chk.get("problems") or [])
        return (f"draft makes {n} claim(s) I can't source from your profile "
                f"— needs your eyes")
    return ""


def auto_apply(brain, model=None, limit: int | None = None) -> dict:
    """Score, draft and SEND applications that clear every gate. Anything
    that doesn't clear a gate is held for you with the reason — that's the
    'only involve me where I'm needed' part."""
    cfg = auto_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "auto-apply is off. Turn it on in the "
                                      "Jobs panel."}
    ok, why = profile_ready()
    if not ok:
        return {"ok": False, "error": why}
    from . import outreach

    sent, held, errors = [], [], []
    cap = int(cfg.get("daily_cap", 5))
    room = max(0, cap - _sent_today())
    todo = [r for r in roles()
            if r.get("stage") in ("found", "drafted")]
    if limit:
        todo = todo[:limit]

    for r in todo:
        key = r["key"]
        try:
            # bring it up to date: score, then draft
            if (r.get("fit") or {}).get("score") is None:
                s = score_role(key, brain, model=model)
                if not s.get("ok"):
                    errors.append(f"{r['title']}: "
                                  + _explain_engine_error(s["error"]))
                    continue
                r = get_role(key)
            fit = r.get("fit") or {}
            if int(fit.get("score") or 0) >= int(cfg["min_score"]) \
                    and not (r.get("draft") or {}).get("body"):
                d = draft_application(key, brain, model=model)
                if not d.get("ok"):
                    errors.append(f"{r['title']}: "
                                  + _explain_engine_error(d["error"]))
                    continue
                r = get_role(key)

            reason = _gate(r, cfg)
            if reason:
                held.append({"key": key, "title": r["title"],
                             "company": r.get("company", ""),
                             "reason": reason})
                continue
            if room <= 0:
                held.append({"key": key, "title": r["title"],
                             "company": r.get("company", ""),
                             "reason": f"daily cap of {cap} reached — queued "
                                       f"for tomorrow"})
                continue

            d = r["draft"]
            body = d["body"] + ("\n\n" + cfg["signature"]
                                if cfg.get("signature") else "")
            res = outreach.send(r["apply_email"], d["subject"], body,
                                dry_run=bool(cfg.get("dry_run", True)))
            if res.get("ok"):
                room -= 1
                stamp = _iso()
                update_role(key, sent_at=stamp,
                            sent_dry_run=bool(cfg.get("dry_run", True)))
                set_stage(key, "applied",
                          ("REHEARSAL (dry run) — not actually sent"
                           if cfg.get("dry_run") else "auto-applied"))
                sent.append({"key": key, "title": r["title"],
                             "company": r.get("company", ""),
                             "to": r["apply_email"],
                             "dry_run": bool(cfg.get("dry_run", True))})
            else:
                errors.append(f"{r['title']}: {res.get('error', 'send failed')}")
        except Exception as exc:
            errors.append(f"{r.get('title', '?')}: {type(exc).__name__}: {exc}")

    # twelve roles failing with the same message is one fault, not twelve
    common = ""
    if errors and len(errors) >= 3:
        tails = [e.split(": ", 1)[-1] for e in errors]
        if len(set(tails)) == 1:
            common = tails[0]
    _audit("auto-apply",
           f"{len(sent)} sent{' (dry run)' if cfg.get('dry_run') else ''}, "
           f"{len(held)} held, {len(errors)} error(s)")
    return {"ok": True, "sent": sent, "held": held, "errors": errors,
            "common_error": common,
            "dry_run": bool(cfg.get("dry_run", True)),
            "remaining_today": room}


# --------------------------------------------------------------------------- #
#  weekly autopilot (opt-in)
# --------------------------------------------------------------------------- #
def _cfg_path() -> Path:
    return _dir() / "config.json"


def load_config() -> dict:
    try:
        return json.loads(_cfg_path().read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    _cfg_path().write_text(json.dumps(cfg), "utf-8")


def schedule_enabled(memory) -> bool:
    sid = load_config().get("schedule_id")
    if not sid:
        return False
    try:
        return memory.get_schedule(int(sid)) is not None
    except Exception:
        return False


def set_schedule(memory, scheduler, enabled: bool) -> bool:
    cfg = load_config()
    sid = cfg.get("schedule_id")
    if enabled and not schedule_enabled(memory):
        spec = scheduler.make_spec("daily", time_str="07:00", n=30)
        nxt = scheduler.next_run(scheduler.parse_spec(spec))
        new_sid = memory.create_schedule(
            "Job scout — daily",
            "Search for new remote contracting roles matching my profile, "
            "score each honestly, and draft applications for strong fits. "
            "Do not send anything.",
            spec, "Auto", False, nxt, action="jobscout", payload="{}")
        save_config({**cfg, "schedule_id": new_sid})
        return True
    if not enabled and sid:
        try:
            memory.delete_schedule(int(sid))
        except Exception:
            pass
        cfg.pop("schedule_id", None)
        save_config(cfg)
    return schedule_enabled(memory)


def _audit(name: str, summary_text: str) -> None:
    try:
        from . import audit
        audit.record("jobscout", name=name, summary=summary_text[:250])
    except Exception:
        pass


# =========================================================================== #
#  Sourcing — where the roles come from
#
#  Everything else here was already automatic: scoring, drafting, the
#  fabrication check, the gates, the send. What wasn't is SUPPLY. Roles had to
#  be handed to it, so "apply to many without intervention" was impossible for
#  a reason that had nothing to do with the applying.
#
#  These are boards that publish their listings openly for exactly this
#  purpose. Nothing is scraped from behind a login, and nothing here touches
#  LinkedIn: automating Easy Apply breaks their terms and gets accounts
#  restricted, which is a bad trade for a contractor whose profile is a
#  business asset.
# =========================================================================== #
# No default boards. Which sites are worth watching is a personal decision —
# the ones that suit a Johannesburg contractor are not the ones that suit a
# graduate in Berlin, and shipping three defaults quietly framed the feature
# as "these three, plus whatever you add". You add what you use.
JOB_SOURCES = []

# Offered as a starting point in the UI, added only if the user picks them.
SUGGESTED_SOURCES = [
    {"name": "Remotive", "kind": "remotive",
     "url": "https://remotive.com/api/remote-jobs?limit=80",
     "about": "Remote roles, worldwide"},
    {"name": "RemoteOK", "kind": "remoteok", "url": "https://remoteok.com/api",
     "about": "Remote roles, worldwide"},
    {"name": "We Work Remotely", "kind": "rss",
     "url": "https://weworkremotely.com/categories/"
            "remote-programming-jobs.rss",
     "about": "Remote programming roles"},
    {"name": "Careers24 (IT)", "kind": "html",
     "url": "https://www.careers24.com/jobs/kw-information-technology/",
     "about": "South African IT roles"},
    {"name": "CareerJunction (IT)", "kind": "html",
     "url": "https://www.careerjunction.co.za/jobs/it",
     "about": "South African IT roles"},
    {"name": "PNet (IT)", "kind": "html",
     "url": "https://www.pnet.co.za/jobs/information-technology",
     "about": "South African IT roles"},
]

JOB_FETCHER = None          # tests replace this


def job_sources() -> list:
    """Whatever the user has added. Empty until they add something."""
    try:
        data = json.loads((_dir() / "sources.json").read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_job_sources(items: list) -> list:
    (_dir() / "sources.json").write_text(json.dumps(items, indent=2), "utf-8")
    return items


BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/json;q=0.8,*/*;q=0.7"),
    "Accept-Language": "en-ZA,en-GB;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


_LAST_HIT = {}
POLITE_GAP = 2.5          # seconds between requests to the same host


def _pace(url: str) -> None:
    """One request at a time per host, with a gap.

    This is not evasion — it is the difference between a reader and a load
    generator. Most "blocks" are rate limits, so pacing genuinely reduces
    them, and a site that has asked you to slow down has said something worth
    honouring."""
    try:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url or "")
        now = time.time()
        last = _LAST_HIT.get(host, 0)
        wait = POLITE_GAP - (now - last)
        if wait > 0:
            time.sleep(min(wait, POLITE_GAP))
        _LAST_HIT[host] = time.time()
    except Exception:
        pass


def _fetch_source(url: str) -> str:
    """Fetch a page the way a browser would.

    The plain client announced itself as a script, and Careers24, Toptal and
    Catalant all answered 403. A normal browser header set is not a trick —
    it is what any person visiting the page sends — and it fixes most of
    them. Sites that still refuse, or that build their list in JavaScript,
    need the real browser below."""
    if JOB_FETCHER is not None:
        return JOB_FETCHER(url)
    _pace(url)
    import httpx
    url = normalise_url(url)
    last = None
    for kwargs in ({"http2": False}, {"http2": False, "verify": False}):
        try:
            with httpx.Client(follow_redirects=True, timeout=25.0,
                              headers=BROWSER_HEADERS, **kwargs) as c:
                r = c.get(url)
                r.raise_for_status()
                # Trusting the declared charset produced "CanÃ³vanas" and
                # "St Johnâ€™s": these boards serve UTF-8 while some declare
                # latin-1. Decode as UTF-8 first and only fall back if that
                # genuinely fails.
                try:
                    return r.content.decode("utf-8")
                except UnicodeDecodeError:
                    return r.text
        except Exception as exc:
            last = exc
    raise last


def fetch_with_browser(url: str) -> str:
    """Load the page in the real browser and take what it renders.

    Two things this solves that headers can't: a site that fingerprints
    non-browser clients, and a listing that only exists after JavaScript has
    run — which is most modern job boards, PNet and Careers24 included."""
    from . import portal
    url = normalise_url(url)
    driver = portal.DRIVER or portal.PlaywrightDriver(headless=True)
    try:
        page = driver.open(url)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass                       # a slow tracker shouldn't lose the page
        return driver.html(page)
    finally:
        try:
            driver.close(keep_open=False)
        except Exception:
            pass


def _explain_fetch_error(err: str, url: str = "") -> str:
    e = " ".join(str(err or "").split())
    low = e.lower()
    if "403" in low or "forbidden" in low:
        return ("blocks automated readers (403). Try 'Use browser' — it loads "
                "the page properly, which most of these accept.")
    if "429" in low:
        return "asked us to slow down (429). Try again in a few minutes."
    if "404" in low:
        return "that page isn't there any more (404) — check the address."
    if "disconnected" in low or "remoteprotocol" in low:
        return ("dropped the connection, usually a bot check. Try 'Use "
                "browser'.")
    if "missing an 'http" in low or "unsupportedprotocol" in low:
        return "the address had no https:// — it has been repaired, try again."
    if "timeout" in low or "timed out" in low:
        return "took too long to answer."
    if "name or service not known" in low or "getaddrinfo" in low:
        return "that domain doesn't resolve — check the spelling."
    return e[:160]


_EMAIL_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# addresses that exist on a page but are not where an application goes
_NOT_APPLY = ("noreply", "no-reply", "donotreply", "support@", "privacy@",
              "legal@", "abuse@", "sentry", "example.com", "@remoteok",
              "@remotive", "@weworkremotely")


def _apply_email(text: str) -> str:
    """An address to apply to, or '' when it's a portal application.

    Being wrong here is expensive in both directions: a missed address means a
    role that could have been applied to automatically isn't, and a wrong one
    sends your application to a mailing list."""
    for m in _EMAIL_IN_TEXT.finditer(text or ""):
        addr = m.group(0).lower()
        if any(bad in addr for bad in _NOT_APPLY):
            continue
        return addr
    return ""


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _clean_location(loc: str) -> str:
    """'Hobart, ' is what an empty country field looks like once joined."""
    return re.sub(r"[,\s]+$", "", str(loc or "").strip())


def _parse_remotive(text: str) -> list:
    out = []
    for j in (json.loads(text).get("jobs") or []):
        body = _strip_html(j.get("description", ""))
        out.append({"title": j.get("title", ""),
                    "company": j.get("company_name", ""),
                    "url": j.get("url", ""),
                    "location": _clean_location(j.get("candidate_required_location", "Remote")),
                    "summary": body[:600],
                    "apply_email": _apply_email(body),
                    "source": "Remotive"})
    return out


def _parse_remoteok(text: str) -> list:
    out = []
    data = json.loads(text)
    for j in (data if isinstance(data, list) else []):
        if not isinstance(j, dict) or not j.get("position"):
            continue                      # the first entry is a legal notice
        body = _strip_html(j.get("description", ""))
        out.append({"title": j.get("position", ""),
                    "company": j.get("company", ""),
                    "url": j.get("url", ""),
                    "location": _clean_location(j.get("location") or "Remote"),
                    "summary": body[:600],
                    "apply_email": _apply_email(body),
                    "source": "RemoteOK"})
    return out


def _parse_jobs_rss(text: str) -> list:
    from xml.etree import ElementTree as ET
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    for item in root.iter():
        if item.tag.split("}")[-1].lower() not in ("item", "entry"):
            continue
        title = link = body = ""
        for c in item:
            tag = c.tag.split("}")[-1].lower()
            if tag == "title":
                title = (c.text or "").strip()
            elif tag == "link":
                link = (c.get("href") or c.text or "").strip()
            elif tag in ("description", "summary", "content"):
                body = _strip_html(c.text or "")
        if not title:
            continue
        company = ""
        if ":" in title:                  # WWR uses "Company: Role"
            company, title = [p.strip() for p in title.split(":", 1)]
        out.append({"title": title, "company": company, "url": link,
                    "location": "Remote", "summary": body[:600],
                    "apply_email": _apply_email(body),
                    "source": "RSS"})
    return out


def _parse_html_kind(text: str) -> list:
    """A source of kind 'html': any ordinary careers page."""
    return _parse_html_listings(text, _CURRENT_SOURCE_URL.get("url", ""))


_CURRENT_SOURCE_URL = {"url": ""}

_PARSERS = {"remotive": _parse_remotive, "remoteok": _parse_remoteok,
            "rss": _parse_jobs_rss, "html": _parse_html_kind}


def _matches_profile(job: dict, p: dict) -> bool:
    """Keep what this person could plausibly do. A wider net means more
    applications and a worse hit rate; the scoring step is expensive, so
    filtering here is what keeps a daily run affordable."""
    blob = (job.get("title", "") + " " + job.get("summary", "")).lower()
    targets = [t.lower() for t in _as_list(p.get("target_roles"))]
    skills = [s.lower() for s in
              _as_list(p.get("skills")) + _as_list(p.get("technologies"))]
    if targets and any(t and t in blob for t in targets):
        return True
    hits = sum(1 for s in skills if s and s in blob)
    return hits >= 2


def discover(limit: int = 40) -> dict:
    """Pull fresh roles from the configured boards and record the new ones."""
    p = profile()
    found, errors, per_source = [], [], {}
    for src in job_sources():
        if not src.get("on", True):
            continue
        name = src.get("name", "?")
        try:
            parser = _PARSERS.get(src.get("kind", "rss"), _parse_jobs_rss)
            _CURRENT_SOURCE_URL["url"] = src["url"]
            jobs = parser(_fetch_source(src["url"]))
            cfg = {**search_config(), "_use_profile_targets": True}
            # search terms, when set, are a deliberate instruction and beat
            # the looser profile guess
            kept = []
            for j in jobs:
                if not j.get("title"):
                    continue
                ok, _ = matches_search(j, cfg)
                if not ok:
                    continue
                if not (cfg.get("queries") or []) \
                        and not _matches_profile(j, p):
                    continue
                kept.append(j)
            per_source[name] = len(kept)
            found.extend(kept)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            per_source[name] = 0
    found = found[:limit]
    res = add_roles(found)
    emails = sum(1 for j in found if j.get("apply_email"))
    _audit("discover", f"{res['added']} new of {len(found)} matched; "
                       f"{emails} have an application address")
    return {"ok": True, "added": res["added"], "matched": len(found),
            "with_email": emails, "per_source": per_source,
            "errors": errors,
            "note": ("Roles without an application address are portal "
                     "applications — they're tracked, but only you can "
                     "submit them.")}


# --------------------------------------------------------------------------- #
#  Follow-ups — the cheapest thing in the whole pipeline
# --------------------------------------------------------------------------- #
_FOLLOWUP_SYSTEM = (
    "Write a short follow-up to an application sent {days} days ago with no "
    "reply. Four sentences at most. Reference the specific role, add ONE "
    "concrete thing of value not in the original (a relevant piece of work, a "
    "question about their stack), and make it easy to say no. Do not guilt "
    "them, do not repeat the original letter, do not invent anything about "
    "the candidate that isn't in the PROFILE. Return ONLY raw JSON: "
    "{{\"subject\": str, \"body\": str}}.")


def draft_follow_up(key: str, brain, model=None) -> dict:
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    if r.get("stage") != "applied":
        return {"ok": False,
                "error": "only an application that was actually sent needs "
                         "chasing"}
    days = 7
    for f in follow_ups(0):
        if f["key"] == key:
            days = f.get("silent_days", 7)
    payload = json.dumps({"PROFILE": profile(), "ROLE": r,
                          "ORIGINAL": (r.get("draft") or {}).get("body", "")},
                         default=str)[:9000]
    try:
        kw = {"model": model} if model else {}
        data = _json_from(brain.chat(
            [{"role": "user", "content": payload}],
            [_FOLLOWUP_SYSTEM.format(days=days)], None, **kw))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    body = _as_text(data.get("body"))
    check = check_draft(body, r)
    fu = {"subject": _as_text(data.get("subject"))[:200], "body": body,
          "check": check, "at": _iso(), "days": days}
    update_role(key, follow_up=fu)
    return {"ok": True, "follow_up": fu}


# --------------------------------------------------------------------------- #
#  A tailored letter per role, saved where you can attach it
# --------------------------------------------------------------------------- #
def application_dir() -> Path:
    d = _dir() / "applications"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_application_file(key: str) -> dict:
    """Write the drafted letter to disk so it can be attached, pasted into a
    portal form, or kept as a record of what was actually sent."""
    r = get_role(key)
    if r is None or not (r.get("draft") or {}).get("body"):
        return {"ok": False, "error": "nothing drafted for that role yet"}
    d = r["draft"]
    name = _slug(f"{r.get('company','')}-{r.get('title','')}") or key
    path = application_dir() / f"{name}.txt"
    lines = [f"Role: {r.get('title','')}", f"Company: {r.get('company','')}",
             f"Advert: {r.get('url','')}", f"Prepared: {_iso()}", "",
             f"Subject: {d.get('subject','')}", "", d.get("body", "")]
    if (d.get("check") or {}).get("ok") is False:
        lines += ["", "VERIFY BEFORE SENDING — claims not sourced from your "
                      "profile:"]
        lines += ["  - " + p["detail"] for p in d["check"].get("problems", [])]
    path.write_text("\n".join(lines), "utf-8")
    update_role(key, application_file=str(path))
    return {"ok": True, "path": str(path)}


# --------------------------------------------------------------------------- #
#  One unattended pass: find, score, draft, send what qualifies
# --------------------------------------------------------------------------- #
ATS_FLOOR = 25          # below this, a screener would drop it anyway


def prescreen(role: dict, p: dict = None) -> dict:
    """A free keyword check before any engine call.

    Scoring a role costs a request; the keyword comparison costs nothing and
    catches the obviously-wrong ones. On a scan that finds forty roles this is
    the difference between forty paid calls and a handful."""
    try:
        from . import cv as cvmod
        s = cvmod.ats_scan(role, p if p is not None else profile())
    except Exception:
        return {"pass": True, "score": None, "why": ""}
    if s.get("total_terms", 0) < 3:
        return {"pass": True, "score": s.get("score"),
                "why": "advert names too few specifics to screen on"}
    if s["score"] < ATS_FLOOR:
        return {"pass": False, "score": s["score"],
                "why": (f"only {s['score']}% keyword overlap — missing "
                        f"{', '.join(s['missing'][:5])}")}
    return {"pass": True, "score": s["score"], "why": ""}


def auto_cycle(brain, model=None) -> dict:
    """What the daily schedule runs. Sourcing is what turned this from
    'applies to the roles you gave it' into something that can genuinely run
    without you."""
    out = {"discovered": 0, "errors": []}
    try:
        d = discover()
        out["discovered"] = d.get("added", 0)
        out["errors"] += d.get("errors", [])
        out["with_email"] = d.get("with_email", 0)
    except Exception as exc:
        out["errors"].append(f"discovery: {type(exc).__name__}: {exc}")
    # collapse the same vacancy found on several boards before spending
    # anything on it
    try:
        d2 = dedupe_roles()
        out["deduped"] = d2.get("removed", 0)
    except Exception:
        out["deduped"] = 0
    # then drop the obviously-wrong ones for free
    screened = 0
    p = profile()
    for r in roles():
        if r.get("stage") != "found" or (r.get("fit") or {}).get("score"):
            continue
        ps = prescreen(r, p)
        if not ps["pass"]:
            update_role(r["key"], stage="closed",
                        closed_reason=f"screened out: {ps['why']}",
                        ats=ps["score"])
            screened += 1
    out["screened_out"] = screened
    try:
        sw = sweep_expired(limit=8)
        out["expired"] = len(sw.get("closed") or [])
    except Exception:
        out["expired"] = 0
    res = auto_apply(brain, model=model)
    out.update({k: res.get(k) for k in
                ("ok", "sent", "held", "dry_run", "remaining_today")})
    out["errors"] += res.get("errors", [])
    if not res.get("ok"):
        out["error"] = res.get("error", "")
    return out


# =========================================================================== #
#  Search — what to look for, and where
#
#  discover() took whatever the boards happened to list and kept anything that
#  loosely matched the profile. That is fine for a daily trickle and useless
#  when you want contract BI work in a particular stack, or want to exclude
#  the agencies that repost the same role nine times.
#
#  Two separate ideas, deliberately not merged:
#    WHAT  — terms to match, terms to exclude, remote/location rules
#    WHERE — which boards are on, plus any you add yourself
# =========================================================================== #
SEARCH_DEFAULTS = {
    "queries": [],          # any-of; empty falls back to the profile
    "exclude": [],          # none-of, checked first
    "locations": [],        # any-of against the advert's location
    "remote_only": True,
    "require_email": False,  # only roles the email path can actually use
}


def search_config() -> dict:
    return {**SEARCH_DEFAULTS, **(load_config().get("search") or {})}


def save_search_config(cfg: dict) -> dict:
    base = load_config()
    merged = {**search_config(), **(cfg or {})}
    for k in ("queries", "exclude", "locations"):
        merged[k] = [s.strip() for s in _as_list(merged.get(k)) if s.strip()]
    base["search"] = merged
    save_config(base)
    return merged


def _blob(job: dict) -> str:
    return " ".join([str(job.get("title", "")), str(job.get("summary", "")),
                     str(job.get("company", ""))]).lower()


_REGION_WORDS = (
    "worldwide", "anywhere", "global", "remote", "emea", "apac", "latam",
    "americas", "europe", "asia", "africa", "oceania", "north america",
    "south america", "middle east", "timezone", "timezones", "gmt", "utc",
    "cet", "est", "pst", "usa", "united states", "canada", "uk",
    "united kingdom", "australia", "new zealand", "india", "germany",
    "france", "spain", "portugal", "poland", "brazil", "argentina",
    "mexico", "singapore", "japan", "israel", "south africa", "nigeria",
    "kenya", "egypt", "eu", "european union",
)


def _is_region_constraint(loc: str) -> bool:
    """Is this a list of regions a remote candidate may live in, rather than
    an office address? A comma-separated run of region names is the giveaway;
    a city with a suburb is not."""
    l = (loc or "").strip().lower().strip(",")
    if not l:
        return False
    parts = [p.strip() for p in l.split(",") if p.strip()]
    if not parts:
        return False
    return all(any(w == p or w in p for w in _REGION_WORDS) for p in parts)


def _region_includes_me(loc: str, cfg: dict) -> bool:
    """Does that region list cover where this person can work?"""
    l = (loc or "").lower()
    if any(w in l for w in ("worldwide", "anywhere", "global")):
        return True
    wanted = [w.lower() for w in (cfg.get("locations") or [])]
    if not wanted:
        wanted = [w.lower() for w in
                  _as_list(profile().get("locations_ok"))
                  if w.strip().lower() != "remote"]
    if not wanted:
        return True                    # nothing declared: don't guess
    # "Africa" covers "South Africa"; so does the country name itself
    return any(w in l or any(part in w for part in l.split(", "))
               for w in wanted)


def matches_search(job: dict, cfg: dict = None) -> tuple[bool, str]:
    """(keep, why_not). Exclusions are checked first — a term you've banned
    should win over a term you asked for, or 'senior' would drag back in the
    agency reposts you just excluded."""
    cfg = cfg if cfg is not None else search_config()
    blob = _blob(job)
    for bad in cfg.get("exclude") or []:
        if bad.lower() in blob:
            return False, f"excluded by \u201c{bad}\u201d"
    loc = str(job.get("location", "")).lower()
    # Remote boards use this field for WHERE THE CANDIDATE MAY LIVE, not for
    # an office. "LATAM, Europe, USA, Canada, APAC" is a remote role with a
    # region constraint, and rejecting it as "not remote" threw away exactly
    # the roles these boards exist to list.
    # NOTE: this only settles the LOCATION question. Returning True here
    # short-circuited the search-term and exclusion checks below, so a
    # worldwide-remote role matched every query.
    region = _is_region_constraint(loc)
    if region and not _region_includes_me(loc, cfg):
        return False, "region excludes you"
    # "Remote only" means "not onsite somewhere I can't be" — not "reject my
    # own country". A South Africa role was being thrown out for someone in
    # Johannesburg. The profile already says where they can work, so use it
    # when no explicit location list has been set.
    wanted = cfg.get("locations") or []
    if not wanted:
        wanted = [l for l in _as_list(profile().get("locations_ok"))
                  if l.strip().lower() not in ("remote",)]
    if cfg.get("remote_only") and loc and not region and not any(
            w in loc for w in ("remote", "anywhere", "worldwide", "global")):
        if not any(l.lower() in loc for l in wanted):
            return False, "not remote"
    # a region list has already been judged above; re-testing it here as if
    # it were a city rejected roles that _region_includes_me had approved
    if wanted and loc and not region \
            and not any(l.lower() in loc for l in wanted) \
            and not any(w in loc for w in ("remote", "anywhere", "worldwide")):
        return False, "location not in your list"
    queries = cfg.get("queries") or []
    if not queries and cfg.get("_use_profile_targets"):
        # An unattended run should look for what your profile says you want,
        # rather than everything the boards have. An explicitly empty search
        # box means the opposite — "show me everything" — so this only
        # applies where nobody is typing.
        queries = derive_queries()
    if queries and not any(q.lower() in blob for q in queries):
        return False, "no search term matched"
    if cfg.get("require_email") and not job.get("apply_email"):
        return False, "no application address"
    return True, ""


def _fetch_all(query: str = "") -> tuple[list, list, dict]:
    if not [s for s in job_sources() if s.get("on", True)]:
        return [], ["No sites added yet — add one under Where & filters, or "
                    "paste a page into 'Search this page'."], {}
    """Pull every enabled source once. `query` is pushed into the URL where a
    board supports it, so the filtering happens at their end rather than ours
    where it can."""
    found, errors, per_source = [], [], {}
    for src in job_sources():
        if not src.get("on", True):
            continue
        name = src.get("name", "?")
        url = src.get("url", "")
        if query and src.get("kind") == "remotive":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}search={query.replace(' ', '+')}"
        try:
            parser = _PARSERS.get(src.get("kind", "rss"), _parse_jobs_rss)
            _CURRENT_SOURCE_URL["url"] = url
            jobs = [j for j in parser(_fetch_source(url)) if j.get("title")]
            per_source[name] = len(jobs)
            found.extend(jobs)
        except Exception as exc:
            errors.append(f"{name}: "
                          + _explain_fetch_error(f"{type(exc).__name__}: "
                                                 f"{exc}", url))
            per_source[name] = 0
    return found, errors, per_source


def search(query: str = "", limit: int = 40) -> dict:
    """Look now, without recording anything. A preview matters: adding forty
    roles you didn't want is tedious to undo, and the scoring step that
    follows costs money."""
    cfg = search_config()
    if query:
        cfg = {**cfg, "queries": [q.strip() for q in
                                  re.split(r"[,;]| OR ", query) if q.strip()]}
    found, errors, per_source = _fetch_all(query)
    have = {r.get("key") for r in roles()}
    results, rejected = [], {}
    for j in found:
        keep, why = matches_search(j, cfg)
        if not keep:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        j = dict(j)
        j["already_tracked"] = _slug(
            f"{j.get('company','')}-{j.get('title','')}") in have
        results.append(j)
    return {"ok": True, "query": query, "results": results[:limit],
            "total": len(results), "per_source": per_source,
            "rejected": rejected, "errors": errors}


def add_from_search(items: list) -> dict:
    """Record roles chosen from a search preview."""
    return add_roles(items or [])


# --------------------------------------------------------------------------- #
#  where to look
# --------------------------------------------------------------------------- #
def normalise_url(url: str) -> str:
    """Make a pasted address usable.

    Two sources were saved as bare hostnames and every fetch failed with
    'Request URL is missing an http:// or https:// protocol'. People paste
    what they see in the address bar, which often has no scheme."""
    u = (url or "").strip().strip("<>\"' ")
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if not re.match(r"^https?://", u, re.I):
        if not re.match(r"^[\w.-]+\.[a-z]{2,}", u, re.I):
            return ""              # not an address at all
        u = "https://" + u
    return u


def repair_sources() -> int:
    """Fix sources already saved without a scheme, so an old mistake doesn't
    keep failing forever."""
    items = job_sources()
    fixed = 0
    for s in items:
        good = normalise_url(s.get("url", ""))
        if good and good != s.get("url"):
            s["url"] = good
            fixed += 1
        if (s.get("name") or "").startswith(("http://", "https://")):
            s["name"] = _site_of(s["url"])
            fixed += 1
    if fixed:
        save_job_sources(items)
    return fixed


def add_job_source(name: str, url: str, kind: str = "rss") -> dict:
    name = (name or "").strip()
    url = (url or "").strip()
    if not url and name.startswith(("http://", "https://")):
        url = name                       # only a url was given
        name = ""
    url = normalise_url(url)
    if not url:
        return {"ok": False, "error": "a source needs a URL"}
    if not name or name.startswith(("http://", "https://")):
        # a url makes a poor label and, until now, an unusable identifier
        name = _site_of(url)
    if kind not in _PARSERS:
        return {"ok": False,
                "error": f"kind must be one of: {', '.join(_PARSERS)}"}
    items = job_sources()
    if any(s.get("url") == url for s in items):
        return {"ok": False, "error": "that URL is already a source"}
    items.append({"name": name, "url": url, "kind": kind, "on": True})
    save_job_sources(items)
    return {"ok": True, "sources": items}


def set_job_source(name: str, on: bool) -> dict:
    items = job_sources()
    hit = False
    name = (name or "").strip()
    for s in items:
        if (s.get("name") or "").strip() == name \
                or (s.get("url") or "").strip() == name:
            s["on"] = bool(on)
            hit = True
    if not hit:
        return {"ok": False, "error": "no such source"}
    save_job_sources(items)
    return {"ok": True, "sources": items}


def remove_job_source(ident: str) -> dict:
    """Remove by name OR url.

    Matching on name alone broke for sources whose name IS a url — which is
    what you get when a url is pasted into the name box, or when the agent
    adds one. The url is the stable identifier, so both are accepted."""
    ident = (ident or "").strip()
    items = job_sources()
    keep = [s for s in items
            if (s.get("name") or "").strip() != ident
            and (s.get("url") or "").strip() != ident]
    if len(keep) == len(items):
        return {"ok": False,
                "error": f"no source matching '{ident[:80]}'"}
    save_job_sources(keep)
    return {"ok": True, "sources": keep, "removed": ident}


# --------------------------------------------------------------------------- #
#  Any site at all
#
#  Three boards is not "search anywhere". Most careers pages aren't RSS and
#  don't have an API — they're a list of links on an HTML page. This reads
#  those, so any URL can be a source: a company's careers page, a niche SA
#  board, a Greenhouse listing page, a filtered search result you've already
#  set up on someone else's site.
#
#  It works on structure rather than any one site's markup: a link is a job if
#  its address or its text looks like one, and isn't navigation.
# --------------------------------------------------------------------------- #
_JOBBY_HREF = re.compile(
    r"/(jobs?|careers?|vacanc\w*|position|opening|opportunit\w*|role|"
    r"listing|apply|posting)[/\-_?=]|"
    r"(greenhouse|lever\.co|workable|ashbyhq|smartrecruiters|recruitee|"
    r"teamtailor|bamboohr|myworkdayjobs|pnet|careers24|jobmail|careerjunction)",
    re.I)

# Words that mean "this is the site's furniture", not a role.
# A listing page links to CATEGORIES as well as to roles, and they read like
# job titles: "Data analyst jobs", "Power BI specialists", "Data analysts".
# Those were tracked as vacancies and drafted against — nine junk applications
# with no company and no advert behind them.
_CATEGORY_TITLE = re.compile(
    r"("
    r"\bjobs?\b\s*$|"                     # "Data analyst jobs"
    r"\bvacanc(y|ies)\b|\bcareers?\b|\bopportunit(y|ies)\b|"
    r"^\s*(all|more|browse|view|see|search|find|latest|top|new)\b|"
    r"\bin\s+[A-Z][a-z]+\s*$|"            # "Data Analyst in Gauteng"
    r"\b(near me|remote jobs|full[- ]time|part[- ]time)\b"
    r")", re.I)

# a bare plural with no company is a category ("Data analysts"), whereas a
# real advert is singular and usually carries seniority or a company
_PLURAL_ONLY = re.compile(
    r"^[A-Za-z][A-Za-z /&+.-]{2,40}(s|ists|ers|eers|ians)\s*$")


def _is_category_link(title: str, company: str = "") -> bool:
    t = " ".join((title or "").split())
    if not t:
        return True
    if _CATEGORY_TITLE.search(t):
        return True
    # "Data analysts" with nothing else attached
    if not company and _PLURAL_ONLY.match(t) and len(t.split()) <= 4:
        return True
    return False


_NAV_WORDS = {
    "home", "about", "about us", "contact", "contact us", "login", "log in",
    "sign in", "sign up", "register", "privacy", "terms", "cookies", "blog",
    "news", "press", "search", "menu", "back", "next", "previous", "more",
    "all jobs", "view all", "browse jobs", "careers", "jobs", "apply now",
    "learn more", "read more", "see all", "faq", "help", "support",
    "our team", "culture", "benefits", "life at", "students", "graduates",
}
_TITLE_HINT = re.compile(
    r"\b(engineer|developer|analyst|scientist|architect|consultant|manager|"
    r"lead|specialist|administrator|designer|officer|director|head of|"
    r"技術|data|bi\b|etl|sql|python|cloud|devops|product|project|qa|test)\b",
    re.I)


def _abs_url(href: str, base: str) -> str:
    from urllib.parse import urljoin
    try:
        return urljoin(base, href)
    except Exception:
        return href


def _parse_html_listings(text: str, base_url: str = "") -> list:
    """Pull job links out of an ordinary web page."""
    out, seen = [], set()
    for m in re.finditer(
            r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
            text or "", re.I | re.S):
        href, inner = m.group(1).strip(), m.group(2)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if not title or len(title) < 6 or len(title) > 140:
            continue
        low = title.lower().strip(" ›»→|-")
        if low in _NAV_WORDS or any(low == w for w in _NAV_WORDS):
            continue
        looks_jobby = bool(_JOBBY_HREF.search(href))
        reads_jobby = bool(_TITLE_HINT.search(title))
        if not (looks_jobby or reads_jobby):
            continue
        # a link that only *reads* like a role still needs a plausible target
        if reads_jobby and not looks_jobby and href.count("/") < 2:
            continue
        if _is_category_link(title):
            continue                     # a link to more listings, not a role
        url = _abs_url(href, base_url)
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "company": "", "url": url,
                    "location": "", "summary": "",
                    "apply_email": "", "source": _site_of(base_url)})
    return out


def _site_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").replace("www.", "") or "web"
    except Exception:
        return "web"


def search_url(url: str, limit: int = 40, use_browser: bool = False) -> dict:
    """Search one page you name, without adding it as a permanent source.

    This is the 'anywhere' part: paste a careers page, a board's filtered
    results, anything — and see what's on it."""
    # normalise BEFORE validating: people paste what the address bar shows,
    # which usually has no scheme, and rejecting that was pedantry
    url = normalise_url(url)
    if not url:
        return {"ok": False,
                "error": "That doesn't look like a web address — paste "
                         "something like careers24.com/jobs or the full "
                         "https:// link."}
    text, how, err = "", "direct", ""
    if not use_browser:
        try:
            text = _fetch_source(url)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
    found = _parse_html_listings(text, url) if text else []
    # Escalate on our own: a refusal, or a page that rendered nothing, is
    # exactly what the browser is for. Doing it automatically means the user
    # isn't asked to understand why a site failed.
    if use_browser or (not found and (err or not text)):
        try:
            text = fetch_with_browser(url)
            how = "browser"
            found = _parse_html_listings(text, url)
            err = ""
        except Exception as exc:
            if err:
                return {"ok": False,
                        "error": f"Couldn't read that page. Directly it "
                                 f"{_explain_fetch_error(err, url)} "
                                 f"Through the browser: "
                                 f"{type(exc).__name__}. "
                                 f"If Playwright isn't installed, run: "
                                 f"pip install playwright && playwright "
                                 f"install chromium"}
            return {"ok": False,
                    "error": f"Couldn't read that page — "
                             f"{_explain_fetch_error(str(exc), url)}"}
    if not found:
        # it might still be a feed someone pasted
        found = _parse_jobs_rss(text)
        for f in found:
            f["source"] = _site_of(url)
    cfg = search_config()
    have = {r.get("key") for r in roles()}
    kept, rejected = [], {}
    for j in found:
        ok, why = matches_search(j, cfg)
        if not ok:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        j = dict(j)
        j["already_tracked"] = _slug(
            f"{j.get('company','')}-{j.get('title','')}") in have
        kept.append(j)
    return {"ok": True, "url": url, "site": _site_of(url), "how": how,
            "results": kept[:limit], "total": len(kept),
            "rejected": rejected,
            "note": ("Titles come from the page's links, so company and "
                     "location are often blank until you open the advert."
                     if kept else
                     "Nothing job-shaped found on that page. If the list "
                     "only appears after the page loads in a browser, this "
                     "can't see it.")}


# --------------------------------------------------------------------------- #
#  Reviewing what the guard held back
#
#  The guard flags claims the profile can't support and refuses to send them.
#  That is right, but on its own it is a locked door with no handle: "9 drafts
#  make a claim I couldn't source" tells you nothing about WHICH claims, and
#  the fix is nearly always that the claim is true and simply isn't written
#  down anywhere yet.
#
#  So: gather every flagged claim across every held draft, and let each one be
#  either confirmed into the profile (it's true, record it) or rejected (it
#  isn't, redraft without it). Confirming is not loosening the guard — it is
#  giving the guard the evidence it was asking for.
# --------------------------------------------------------------------------- #
def held_claims() -> dict:
    """Every unsourced claim still awaiting your decision.

    Dismissed claims are left out: you've already answered. They keep their
    draft held — the wording still makes the claim — but the fix for those is
    a redraft, not another question."""
    banned = {b.lower() for b in banned_claims()}
    claims, held = {}, []
    for r in roles():
        # the same classifier the pipeline counts with — this pairing is what
        # produced "15 held" beside an empty tab
        if role_state(r)["bucket"] not in ("held", "needs_redraft"):
            continue
        d = r.get("draft") or {}
        c = d.get("check") or {}
        # carry the draft and its problems: the tab listed CLAIMS and never
        # the drafts themselves, so a draft held for a reason that names no
        # claim showed as nothing at all
        _d = r.get("draft") or {}
        _c = _d.get("check") or {}
        held.append({"key": r["key"], "title": r.get("title", ""),
                     "company": r.get("company", ""),
                     "subject": _d.get("subject", ""),
                     "body": (_d.get("body") or "")[:4000],
                     "problems": [p.get("detail", "")
                                  for p in (_c.get("problems") or [])],
                     "needs_redraft": bool(_d.get("needs_redraft")),
                     "redraft_reason": _d.get("redraft_reason", "")})
        for p in c.get("problems") or []:
            term = _claim_term(p)
            if not term:
                continue
            # the proper-noun check and the tool-lexicon check flag the same
            # word with different casing; showing "Databricks" and
            # "databricks" as two things to decide is just noise
            key = term.lower()
            if key in banned:
                continue                  # already decided against
            entry = claims.setdefault(key, {
                "term": term, "kind": p.get("kind", ""), "roles": [],
                "detail": p.get("detail", ""),
                "context": p.get("context", "")})
            if r.get("title") and r["title"] not in entry["roles"]:
                entry["roles"].append(r["title"])
            if p.get("context") and not entry["context"]:
                entry["context"] = p["context"]
            # prefer the capitalised spelling for display
            if term and term[:1].isupper():
                entry["term"] = term
    ordered = sorted(claims.values(), key=lambda c: (-len(c["roles"]),
                                                     c["term"].lower()))
    # With an empty profile NOTHING can be sourced, so every draft is held and
    # the list becomes dozens of terms to confirm one at a time. That is the
    # wrong instruction: the fix is to fill the profile once, not to approve
    # each word individually.
    p = profile()
    have = sum(len(_as_list(p.get(f))) for f in
               ("skills", "technologies", "employers", "achievements"))
    profile_empty = have < 3
    needs_redraft = [h for h in held if h["key"] in _redraft_keys(banned)]
    for h in held:
        h["needs_redraft"] = h["key"] in {n["key"] for n in needs_redraft}
    drafted_ok = sum(1 for r in roles()
                     if r.get("stage") == "drafted"
                     and ((r.get("draft") or {}).get("check") or {}
                          ).get("ok") is not False)
    return {"held": held, "drafted_ready": drafted_ok,
            "claims": [] if profile_empty else ordered,
            "count": len(held),
            "distinct": 0 if profile_empty else len(ordered),
            "profile_empty": profile_empty,
            "would_be_claims": len(ordered) if profile_empty else 0,
            "needs_redraft": needs_redraft,
            "banned": sorted(banned_claims())}


def _redraft_keys(banned: set) -> set:
    """Drafts that still mention something you said you don't claim. No
    amount of confirming will clear these; the wording has to change."""
    out = set()
    for r in roles():
        d = r.get("draft") or {}
        c = d.get("check") or {}
        if r.get("stage") != "drafted" or c.get("ok") is not False:
            continue
        terms = {_claim_term(p).lower() for p in (c.get("problems") or [])}
        terms.discard("")
        if terms and terms & banned:
            out.add(r["key"])
    return out


def _claim_term(p: dict) -> str:
    """The thing being claimed, pulled out of the message shown to the user."""
    # The quotes below are the curly ones the messages actually use.
    # Written as \u escapes inside a RAW string they matched a literal
    # backslash instead, so every term came back empty -- which is why
    # the tab said "9 held" and "0 claims to decide" at the same time.
    m = re.search("[“\"']([^”\"']+)[”\"']",
                  p.get("detail", "") or "")
    return m.group(1).strip() if m else ""


CLAIM_TARGETS = ("technologies", "skills", "achievements", "employers")


def confirm_claim(term: str, where: str = "technologies") -> dict:
    """Record a claim as true, then re-check every held draft.

    This is the honest half of the guard: most flags are things you have
    genuinely done that were never written into the profile."""
    term = (term or "").strip()
    if not term:
        return {"ok": False, "error": "nothing to confirm"}
    if where not in CLAIM_TARGETS:
        return {"ok": False,
                "error": f"where must be one of: {', '.join(CLAIM_TARGETS)}"}
    p = profile()
    items = _as_list(p.get(where))
    if not any(term.lower() == str(x).lower() for x in items):
        items.append(term)
        p[where] = items
        save_profile(p)
    _audit("claim-confirmed", f"{term} -> {where}")
    return {"ok": True, "profile_field": where, "term": term,
            **recheck_drafts()}


def recheck_drafts() -> dict:
    """Re-run the fabrication check on every drafted role.

    Without this, adding something to the profile would change nothing until
    each draft was regenerated — which costs an engine call per role for a
    check that is entirely local."""
    cleared, still_held = 0, 0
    for r in roles():
        d = r.get("draft") or {}
        if r.get("stage") != "drafted" or not d.get("body"):
            continue
        fresh = check_draft(d["body"], r)
        was = ((d.get("check") or {}).get("ok"))
        d["check"] = fresh
        update_role(r["key"], draft=d)
        if fresh["ok"] and was is False:
            cleared += 1
        elif not fresh["ok"]:
            still_held += 1
    return {"cleared": cleared, "still_held": still_held}


def dismiss_claim(term: str) -> dict:
    """Mark a claim as one you do NOT want to make, so drafts stop using it."""
    term = (term or "").strip()
    if not term:
        return {"ok": False, "error": "nothing to dismiss"}
    cfg = load_config()
    banned = [b for b in _as_list(cfg.get("banned_claims"))]
    if term.lower() not in [b.lower() for b in banned]:
        banned.append(term)
    cfg["banned_claims"] = banned
    save_config(cfg)
    affected = sorted(_redraft_keys({term.lower()}))
    for key in affected:
        r = get_role(key)
        d = (r or {}).get("draft") or {}
        if d:
            d["needs_redraft"] = True
            d["redraft_reason"] = (f"mentions “{term}”, which you said you "
                                   f"don't claim")
            update_role(key, draft=d)
    _audit("claim-dismissed", f"{term}; {len(affected)} draft(s) to rewrite")
    return {"ok": True, "banned": banned, "affected": affected,
            "note": (f"{len(affected)} draft(s) still say it — redraft them "
                     f"and it won't be used again."
                     if affected else
                     "Noted. Future drafts won't use it.")}


def banned_claims() -> list:
    return _as_list(load_config().get("banned_claims"))


# --------------------------------------------------------------------------- #
#  Removing roles
#
#  The subtlety: deleting a role you've decided against is pointless if the
#  next scan finds it again tomorrow. So removal remembers the key, and
#  discovery skips it. That memory is a short list of keys, not the role —
#  nothing of the advert is kept.
#
#  Removal that ISN'T a rejection (tidying up duplicates, clearing out old
#  closed roles) can opt out of being remembered.
# --------------------------------------------------------------------------- #
def ignored_keys() -> list:
    return _as_list(load_config().get("ignored_roles"))


def _remember_ignored(keys) -> None:
    cfg = load_config()
    have = {k.lower() for k in _as_list(cfg.get("ignored_roles"))}
    out = _as_list(cfg.get("ignored_roles"))
    for k in keys:
        if k and k.lower() not in have:
            out.append(k)
            have.add(k.lower())
    cfg["ignored_roles"] = out[-500:]        # bounded
    save_config(cfg)


def unignore(key: str) -> dict:
    cfg = load_config()
    keep = [k for k in _as_list(cfg.get("ignored_roles"))
            if k.lower() != (key or "").lower()]
    cfg["ignored_roles"] = keep
    save_config(cfg)
    return {"ok": True, "ignored": keep}


def remove_role(key: str, forget: bool = True) -> dict:
    """Remove one tracked role. `forget` keeps it from coming back."""
    key = (key or "").strip()
    rs = roles()
    keep = [r for r in rs if r.get("key") != key]
    if len(keep) == len(rs):
        return {"ok": False, "error": "no such role"}
    gone = next(r for r in rs if r.get("key") == key)
    save_roles(keep)
    if forget:
        _remember_ignored([key])
    _audit("role-removed", f"{gone.get('title', key)}"
                           f"{' (won\'t return)' if forget else ''}")
    return {"ok": True, "removed": key, "title": gone.get("title", ""),
            "forgotten": bool(forget), "remaining": len(keep)}


def remove_roles(keys: list, forget: bool = True) -> dict:
    wanted = {str(k).strip() for k in (keys or []) if str(k).strip()}
    rs = roles()
    keep = [r for r in rs if r.get("key") not in wanted]
    removed = len(rs) - len(keep)
    if not removed:
        return {"ok": False, "error": "none of those are tracked"}
    save_roles(keep)
    if forget:
        _remember_ignored(wanted)
    _audit("roles-removed", f"{removed} role(s)")
    return {"ok": True, "removed": removed, "remaining": len(keep)}


def prune_junk(forget: bool = False) -> dict:
    """Remove roles that were never vacancies.

    Category links were tracked as jobs before the extractor could tell them
    apart, and each one generated a draft addressed to nobody. This clears
    them out; `forget` is off by default because these aren't decisions about
    a role, they're a mistake being corrected."""
    rs = roles()
    junk = [r for r in rs if not _looks_like_a_real_role(r)]
    if not junk:
        return {"ok": False, "error": "nothing junk-looking is tracked"}
    keep = [r for r in rs if r not in junk]
    save_roles(keep)
    if forget:
        _remember_ignored([r["key"] for r in junk])
    _audit("prune-junk", f"{len(junk)} non-vacancy entr(ies) removed")
    return {"ok": True, "removed": len(junk), "remaining": len(keep),
            "titles": [r.get("title", "") for r in junk][:20]}


def clear_roles(stage: str = "", never_scored: bool = False,
                forget: bool = False) -> dict:
    """Bulk tidy-up. Defaults to NOT remembering, because clearing out old
    closed roles is housekeeping, not a decision about the role."""
    rs = roles()
    def drop(r):
        if stage and r.get("stage") != stage:
            return False
        if never_scored and (r.get("fit") or {}).get("score") is not None:
            return False
        if not stage and not never_scored:
            return False                  # refuse to wipe everything blindly
        return True
    doomed = [r for r in rs if drop(r)]
    if not doomed:
        return {"ok": False, "error": "nothing matched that"}
    keep = [r for r in rs if r not in doomed]
    save_roles(keep)
    if forget:
        _remember_ignored([r["key"] for r in doomed])
    _audit("roles-cleared", f"{len(doomed)} role(s), stage={stage or 'any'}")
    return {"ok": True, "removed": len(doomed), "remaining": len(keep)}


# =========================================================================== #
#  Running without you
#
#  The pieces were all here and none of them joined up: you had to type search
#  terms, press scan, press score, press draft, and set up a schedule by hand.
#  And every role got an engine call to score it — including ones a free local
#  keyword check would have rejected in a millisecond.
# =========================================================================== #
def derive_queries(p: dict = None) -> list:
    """Search terms from the profile, when you haven't set any.

    Asking someone to type search terms they already wrote into their profile
    is make-work, and an empty query list means the scan falls back to a loose
    profile match that pulls in noise."""
    p = p if p is not None else profile()
    out = []
    for t in _as_list(p.get("target_roles")):
        t = t.strip()
        if t and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    if not out:
        # no stated targets: use the strongest tools as a proxy
        for s in (_as_list(p.get("technologies"))
                  + _as_list(p.get("skills")))[:4]:
            s = s.strip()
            if s and s.lower() not in [o.lower() for o in out]:
                out.append(s)
    return out[:6]


def _norm_title(s: str) -> str:
    """Strip the decoration boards add, so the same role from three sites
    collapses to one entry rather than three applications."""
    t = (s or "").lower()
    t = re.sub(r"\((remote|contract|hybrid|onsite|f/m/d|m/f/d)[^)]*\)", " ", t)
    t = re.sub(r"\b(senior|snr|jnr|junior|lead|principal|staff|iii|ii|i|"
               r"remote|contract|permanent|full[- ]time|part[- ]time)\b",
               " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def dedupe_roles(forget: bool = False) -> dict:
    """Collapse the same vacancy listed on several boards.

    Keeps the one with an application address if there is one, since that is
    the copy the email path can actually use."""
    rs = roles()
    groups = {}
    for r in rs:
        key = (_norm_title(r.get("title", "")),
               (r.get("company") or "").strip().lower())
        if not key[0]:
            continue
        groups.setdefault(key, []).append(r)
    dropped = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda r: (bool(r.get("apply_email")),
                                  r.get("stage") != "found",
                                  len(str(r.get("summary") or ""))),
                   reverse=True)
        for extra in items[1:]:
            dropped.append(extra)
    if not dropped:
        return {"ok": False, "error": "no duplicates found"}
    keep = [r for r in rs if r not in dropped]
    save_roles(keep)
    if forget:
        _remember_ignored([r["key"] for r in dropped])
    _audit("dedupe", f"{len(dropped)} duplicate listing(s) merged")
    return {"ok": True, "removed": len(dropped), "remaining": len(keep),
            "titles": [r.get("title", "") for r in dropped][:10]}


def pipeline() -> dict:
    """Where everything is, and where it has stopped.

    "70 held, 0 sent" is only useful if you can see WHICH step is blocking.
    """
    # counts come from role_state, so this cannot disagree with the tabs
    c = counts()
    stages = {
        "found": c["found"], "scored": c["scored"],
        "drafted": c["drafted"] + c["no_address"],
        "held": c["held"] + c["needs_redraft"],
        "applied": c["applied"], "responded": c["waiting"],
        "interview": 0, "offer": 0,
        "closed": c["closed"] + c["expired"] + c["screened"],
    }
    total = sum(c.values())
    # the blockage is the first stage holding more than half of everything
    block = ""
    if total >= 4:
        for name in ("found", "scored", "held", "drafted"):
            if stages[name] >= max(3, total * 0.5):
                block = name
                break
    reasons = {
        "found": "nothing has been scored yet — run auto-apply, or score one "
                 "to see how it reads",
        "scored": "scored but not drafted — drafting needs a profile it can "
                  "quote from",
        "held": "drafts are held because they claim things your profile "
                "can't support — Held drafts shows which",
        "drafted": "drafted and waiting: either no application address, or "
                   "auto-apply is off",
    }
    return {"stages": stages, "total": total, "blocked_at": block,
            "why": reasons.get(block, ""), "counts": c,
            "sent": c["applied"] + c["waiting"]}


# =========================================================================== #
#  Adverts that have closed
#
#  A vacancy is a perishable thing. Nothing here noticed, so a role found in
#  July sat in the list looking exactly like one found this morning, and an
#  application could be drafted for something that closed weeks ago.
#
#  The distinction that matters: a page SAYING it is closed is evidence, and a
#  page that will not load is not. Boards block automated readers all the time
#  — treating a 403 as "expired" would quietly delete live roles, which is a
#  worse fault than leaving a dead one on the list.
# =========================================================================== #
STALE_DAYS = 45          # after this, an unapplied advert is probably gone

_CLOSED_PHRASES = (
    "no longer accepting applications",
    "no longer available", "this job has closed", "position has been filled",
    "applications are closed", "advert has expired", "job has expired",
    "vacancy has closed", "closed for applications", "posting has expired",
    "we are no longer hiring", "this role is no longer",
    "the position is filled", "job not found", "vacancy no longer",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def mark_closed(key: str, reason: str, expired: bool = False) -> dict:
    """Close a role and record WHEN and WHY.

    A stage of "closed" with no date told you nothing three weeks later."""
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    update_role(key, stage="closed", closed_at=_now_iso(),
                closed_reason=reason[:200], expired=bool(expired))
    _audit("closed", f"{r.get('title', key)}: {reason[:120]}")
    return {"ok": True, "key": key, "closed_at": _now_iso(),
            "reason": reason, "expired": bool(expired)}


def check_expiry(role: dict) -> dict:
    """Is this advert still open?

    Three answers, not two. "Closed" needs the page to say so. "Unknown" is
    what a blocked or unreachable page gets — and unknown must never close a
    role, or a board having a bad afternoon deletes your shortlist.
    """
    url = (role or {}).get("url") or ""
    if not url:
        return {"state": "unknown", "why": "no advert link to check"}
    try:
        text = _fetch_source(url)
    except Exception as exc:
        e = f"{type(exc).__name__}: {exc}".lower()
        if "404" in e or "not found" in e:
            return {"state": "closed",
                    "why": "the advert page is gone (404)"}
        return {"state": "unknown",
                "why": f"couldn't read the page — {_explain_fetch_error(e)}"}
    low = " ".join(_strip_html(text).lower().split())
    for phrase in _CLOSED_PHRASES:
        if phrase in low:
            return {"state": "closed", "why": f"the page says “{phrase}”"}
    if len(low) < 200:
        # an empty shell is usually a redirect to a "job not found" page, but
        # it is also what a JavaScript board looks like — so, unknown
        return {"state": "unknown",
                "why": "the page came back nearly empty"}
    return {"state": "open", "why": ""}


def days_listed(role: dict) -> float:
    added = (role or {}).get("added_at") or ""
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d"):
        try:
            when = datetime.strptime(added, fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - when).total_seconds() / 86400
        except Exception:
            continue
    return 0.0


def looks_stale(role: dict) -> bool:
    """Old, and never acted on. Not proof, which is why it's only a label."""
    if role.get("stage") in ("applied", "responded", "interview", "offer",
                             "closed"):
        return False
    return days_listed(role) >= STALE_DAYS


def sweep_expired(limit: int = 12, include_applied: bool = False) -> dict:
    """Check the oldest open adverts and close the ones that say they're shut."""
    cands = [r for r in roles()
             if r.get("stage") not in ("closed", "offer")
             and (include_applied or r.get("stage") != "applied")
             and r.get("url")]
    cands.sort(key=lambda r: -days_listed(r))
    closed, unknown, still_open = [], [], 0
    for r in cands[:max(1, limit)]:
        res = check_expiry(r)
        if res["state"] == "closed":
            mark_closed(r["key"], res["why"], expired=True)
            closed.append({"title": r.get("title", ""), "why": res["why"]})
        elif res["state"] == "unknown":
            unknown.append({"title": r.get("title", ""), "why": res["why"]})
        else:
            still_open += 1
    return {"ok": True, "checked": len(cands[:max(1, limit)]),
            "closed": closed, "unknown": unknown, "still_open": still_open,
            "note": ("A page that wouldn't load is left alone — being blocked "
                     "is not the same as being closed.")}


# =========================================================================== #
#  What the next run would do
#
#  The auto-apply panel showed the gates and a Run button, which tells you the
#  rules but not the consequence. Deciding whether to turn rehearsal off is a
#  decision about THIS list, not about the settings in the abstract — so work
#  it out and show it, before anything is spent.
#
#  Entirely local: the screening is keyword work, the gates are arithmetic,
#  and where an engine call would be needed the answer is "it would score
#  this one", not a guess at the result.
# =========================================================================== #
def auto_preview() -> dict:
    """A dry walk through the cycle. No engine calls, no sending."""
    cfg = auto_config()
    p = profile()
    floor = int(cfg.get("min_score", 75) or 75)
    cap = int(cfg.get("daily_cap", 5) or 5)
    sent_today = _sent_today()
    room = max(0, cap - sent_today)

    would_send, would_hold, needs_score, screened, no_address, expired = \
        [], [], [], [], [], []
    # grouped by the same buckets the pipeline counts, so the preview and
    # the numbers above it can't tell different stories
    for r in roles():
        s = role_state(r, cfg)
        b, title = s["bucket"], s["title"]
        if b == "expired":
            expired.append(title)
        elif b in ("closed", "applied", "waiting"):
            continue
        elif b == "found":
            ps = prescreen(r, p)
            if ps["pass"]:
                needs_score.append(title)
            else:
                screened.append({"title": title, "why": ps["why"]})
        elif b in ("screened",):
            screened.append({"title": title, "why": s["why"]})
        elif b in ("held", "needs_redraft"):
            would_hold.append({"title": title, "why": s["why"]})
        elif b == "no_address":
            no_address.append(title)
        elif b == "scored":
            needs_score.append(title)
        elif b == "drafted":
            would_send.append({"title": title, "company": s["company"],
                               "fit": s["fit"], "to": s["apply_email"]})

    capped = would_send[room:] if room < len(would_send) else []
    return {
        "enabled": bool(cfg.get("enabled")),
        "dry_run": cfg.get("dry_run", True) is not False,
        "min_score": floor, "daily_cap": cap,
        "sent_today": sent_today, "room_today": room,
        "would_send": would_send[:room], "capped": [c["title"] for c in capped],
        "needs_scoring": needs_score, "would_hold": would_hold,
        "screened_out": screened, "no_address": no_address,
        "expired": expired,
        "follow_ups": [f.get("title", "") for f in (follow_ups() or [])],
        "summary": _preview_sentence(len(would_send[:room]), len(needs_score),
                                     len(would_hold), len(no_address),
                                     len(screened), cfg),
    }


def _preview_sentence(send, score, hold, noaddr, screened, cfg) -> str:
    """One line that answers 'so what happens if I press it'."""
    if not cfg.get("enabled"):
        return ("Auto-apply is off. Nothing runs on its own — this is what it "
                "would do if you turned it on.")
    bits = []
    if send:
        bits.append(f"send {send}"
                    + (" (rehearsal, so nothing leaves)"
                       if cfg.get("dry_run", True) is not False else ""))
    if score:
        bits.append(f"score {score}")
    if hold:
        bits.append(f"hold {hold} back")
    if noaddr:
        bits.append(f"skip {noaddr} with no address")
    if screened:
        bits.append(f"ignore {screened} that don't match")
    return ("The next run would " + ", ".join(bits) + "."
            if bits else "There's nothing for the next run to do.")


def _sent_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for r in roles():
        when = str(r.get("applied_at") or "")
        if when.startswith(today):
            n += 1
    return n


def recent_runs(limit: int = 8) -> list:
    """What the unattended runs have actually done."""
    out = []
    try:
        from . import audit
        for e in audit.recent(400):
            if e.get("kind") != "jobscout":
                continue
            if e.get("name") not in ("auto-apply", "discover", "alert-ingest"):
                continue
            out.append({"at": e.get("iso", ""), "what": e.get("name", ""),
                        "detail": (e.get("summary") or "")[:160]})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


# =========================================================================== #
#  One answer to "what state is this role in"
#
#  This has been the same bug three times: the Held tab said 9 while the list
#  showed nothing; the dashboard reported "needs checking" for drafts whose
#  claims were already decided; the tab showed 15 held beside an empty panel.
#  Each time a COUNT was computed one way and a LIST another, and each fix
#  corrected one instance of a fault that could recur anywhere.
#
#  Four callers classify roles — the pipeline, held drafts, the auto-apply
#  preview, and the tracked list. Rather than keep them in step by discipline,
#  they read this. A disagreement stops being a bug that can be written.
# =========================================================================== #
BUCKETS = ("found", "screened", "scored", "drafted", "held", "needs_redraft",
           "no_address", "applied", "waiting", "closed", "expired")


def _st(bucket: str, why: str, r: dict) -> dict:
    return {"bucket": bucket, "why": why, "key": r.get("key", ""),
            "title": r.get("title", ""), "company": r.get("company", ""),
            "fit": (r.get("fit") or {}).get("score"),
            "apply_email": r.get("apply_email", ""),
            "can_send": bucket == "drafted"}


def role_state(r: dict, cfg: dict = None) -> dict:
    """The single answer. Everything that classifies a role calls this."""
    cfg = cfg if cfg is not None else auto_config()
    stage = r.get("stage", "found")
    fit = (r.get("fit") or {}).get("score")
    draft = r.get("draft") or {}
    check = draft.get("check") or {}
    floor = int(cfg.get("min_score", 75) or 75)

    if r.get("expired"):
        return _st("expired", "the advert has closed", r)
    if stage == "closed":
        return _st("closed", r.get("closed_reason", "closed"), r)
    if stage in ("responded", "interview", "offer"):
        return _st("waiting", f"they replied — {stage}", r)
    if stage == "applied":
        return _st("applied", "sent, waiting on them", r)
    if stage == "drafted":
        if draft.get("needs_redraft"):
            return _st("needs_redraft",
                       draft.get("redraft_reason", "needs rewriting"), r)
        if check.get("ok") is False:
            return _st("held", "the draft claims something your profile "
                               "can't support", r)
        if not r.get("apply_email"):
            return _st("no_address", "portal only — apply from Tracked", r)
        return _st("drafted", "ready to send", r)
    if fit is not None:
        if fit < floor:
            return _st("screened", f"fit {fit} is below your {floor}", r)
        return _st("scored", "scored, not drafted yet", r)
    return _st("found", "not scored yet", r)


def states(cfg: dict = None) -> list:
    cfg = cfg if cfg is not None else auto_config()
    return [role_state(r, cfg) for r in roles()]


def counts() -> dict:
    """Bucket totals. The one place a number comes from."""
    out = {b: 0 for b in BUCKETS}
    for s in states():
        out[s["bucket"]] = out.get(s["bucket"], 0) + 1
    return out


# =========================================================================== #
#  The archive, and applying by hand
#
#  Closed and expired roles cluttered the list they were no longer part of.
#  Deleting them would lose the record — you want to know you saw a role, and
#  what became of it, when the same company advertises again. So they move
#  rather than vanish, and can come back.
#
#  And the pipeline assumed it did the applying. Most applications are still
#  made by a person: a portal form, an email you wrote yourself, a referral.
#  Without a way to say so, a role you'd already applied to sat looking
#  untouched, and follow-ups never started.
# =========================================================================== #
def _archive_path() -> Path:
    return _dir() / "archive.json"


def archived() -> list:
    try:
        data = json.loads(_archive_path().read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_archive(items: list) -> None:
    _archive_path().write_text(json.dumps(items, indent=2), "utf-8")


def archive_closed(also_applied_before_days: int = 0) -> dict:
    """Move closed and expired roles out of the working list.

    They keep everything — the draft, the score, why they closed — so the
    record survives. `also_applied_before_days` sweeps old applications you
    never heard back about, which is the other thing that silts up a list."""
    keep, moved = [], []
    cutoff_days = int(also_applied_before_days or 0)
    for r in roles():
        st = role_state(r)["bucket"]
        old_application = (cutoff_days and st == "applied"
                           and days_listed(r) >= cutoff_days)
        if st in ("closed", "expired") or old_application:
            moved.append({**r, "archived_at": _now_iso(),
                          "archived_because": ("no reply after "
                                               f"{cutoff_days} days"
                                               if old_application
                                               else role_state(r)["why"])})
        else:
            keep.append(r)
    if not moved:
        return {"ok": False, "error": "nothing closed or expired to archive"}
    save_roles(keep)
    _save_archive(archived() + moved)
    _audit("archive", f"{len(moved)} role(s) archived")
    return {"ok": True, "moved": len(moved), "remaining": len(keep),
            "titles": [m.get("title", "") for m in moved][:20],
            "note": "They're kept in the archive — nothing was deleted."}


def unarchive(key: str) -> dict:
    """Bring one back into the working list."""
    items = archived()
    hit = next((a for a in items if a.get("key") == key), None)
    if hit is None:
        return {"ok": False, "error": "not in the archive"}
    _save_archive([a for a in items if a.get("key") != key])
    r = {k: v for k, v in hit.items()
         if k not in ("archived_at", "archived_because")}
    r["stage"] = "found"
    r.pop("expired", None)
    r.pop("closed_at", None)
    r.pop("closed_reason", None)
    save_roles(roles() + [r])
    _audit("unarchive", r.get("title", key))
    return {"ok": True, "title": r.get("title", ""),
            "note": "Back in the list, as if newly found."}


def archive_summary() -> dict:
    items = archived()
    by = {}
    for a in items:
        why = (a.get("archived_because") or "closed")[:60]
        by[why] = by.get(why, 0) + 1
    return {"count": len(items), "by_reason": by,
            "roles": [{"key": a.get("key", ""), "title": a.get("title", ""),
                       "company": a.get("company", ""),
                       "when": a.get("archived_at", ""),
                       "why": a.get("archived_because", ""),
                       "was_applied": a.get("stage") == "applied"}
                      for a in items[-80:]][::-1]}


def mark_applied(key: str, how: str = "by hand", note: str = "") -> dict:
    """Record that an application went in — however it went in.

    The pipeline assumed it did the sending. Most applications are made by a
    person, and without this the role sat looking untouched: no follow-up
    clock, no count against the day, and a real chance of applying twice."""
    r = get_role(key)
    if r is None:
        return {"ok": False, "error": "no such role"}
    if role_state(r)["bucket"] in ("applied", "waiting"):
        return {"ok": False,
                "error": f"“{r.get('title', '')}” is already marked applied "
                         f"— nothing sent twice."}
    when = _now_iso()
    update_role(key, stage="applied", applied_at=when, applied_how=how,
                applied_note=note[:300])
    _audit("applied", f"{r.get('title', key)} — {how}")
    return {"ok": True, "title": r.get("title", ""), "at": when, "how": how,
            "note": ("Recorded. Follow-up is due in a week, and it won't be "
                     "applied to again.")}
