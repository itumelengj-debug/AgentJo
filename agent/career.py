"""Your career, as Agent Jo Jobs knows it — readable from the chat.

Agent Jo Jobs keeps your full CV (split into sections), your job profile, and
every answer you've given an application form. Asked "what did I do at
Standard Bank?" or "write a bio from my CV", the chat had none of it. This
reads that knowledge so the chat can answer from it.

The two apps share no code (CHANGELOG 155), and this keeps it that way: it
reads the Jobs app's data files — ~/.agent_jo_jobs/jobscout, or
AGENT_JOBS_HOME — and imports nothing from it. It only reads: it never writes
to the job search's data, never applies, never sends. And it honours the
job search's own rule for sensitive questions: an answer you haven't switched
on for reuse there (gender, race, ID number…) isn't shown here either.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
_STOP = set("""a an and are as at be by for from has have i in is it its of on
or our that the their this to was were will with you your my me we us he she
they them his her not no but if so than then there these those which who what
when where how all any can into out up did do does about tell""".split())


def home() -> Path:
    return Path(os.environ.get("AGENT_JOBS_HOME") or (Path.home() / ".agent_jo_jobs"))


def _read(rel: str):
    try:
        return json.loads((home() / "jobscout" / rel).read_text("utf-8"))
    except Exception:
        return None


def _words(text: str) -> list:
    return [w.strip(".-") for w in _WORD.findall((text or "").lower())
            if w.strip(".-") and w not in _STOP and len(w) > 1]


def _bm25(query: str, docs: list, k: int) -> list:
    """docs: [(text, item)] -> the k best items for the query."""
    q = set(_words(query))
    toks = [_words(t) for t, _ in docs]
    if not q or not docs:
        return []
    n, avg = len(docs), sum(len(t) for t in toks) / max(1, len(docs))
    df = {w: sum(1 for t in toks if w in t) for w in q}
    out = []
    for (_, item), t in zip(docs, toks):
        s = 0.0
        for w in q:
            tf = t.count(w)
            if tf:
                s += math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) * tf * 2.2 / (
                    tf + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if s > 0:
            out.append((s, item))
    out.sort(key=lambda x: -x[0])
    return [item for _, item in out[:k]]


def status() -> dict:
    cv = _read("cv/chunks.json") or {}
    answers = [a for a in (_read("answers.json") or []) if a.get("reuse", True)]
    prof = _read("profile.json") or {}
    return {"found": home().exists(), "cv_sections": len(cv.get("chunks") or []),
            "cv_saved": cv.get("at", ""), "answers": len(answers),
            "profile": bool(prof.get("summary") or prof.get("skills"))}


def applications(query: str = "", now=None) -> str:
    """Interviews coming up, and where the roles the question names stand —
    with the interview prep Agent Jo Jobs wrote for them. "" when none."""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    roles = _read("roles.json") or []
    if not isinstance(roles, list):
        return ""
    lines = []
    coming = []
    for r in roles:
        try:
            at = datetime.strptime(r.get("interview_at") or "", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if at >= now - timedelta(hours=2) and r.get("stage") in ("interview", "responded", "offer"):
            coming.append((at, r))
    if coming:
        lines.append("INTERVIEWS COMING UP")
        for at, r in sorted(coming, key=lambda x: x[0])[:6]:
            lines.append(f"- {at:%a %d %b %Y, %H:%M} — {r.get('title', '')} @ {r.get('company', '')}"
                         + (f" (link: {r['interview_link']})" if r.get("interview_link") else ""))
    q = set(_words(query))
    named = [r for r in roles if r.get("stage") not in ("found", None)
             and q & set(_words(r.get("company", "")))]
    for r in named[:3]:
        lines.append(f"\nAPPLICATION — {r.get('title', '')} @ {r.get('company', '')}: "
                     f"stage {r.get('stage', '')}"
                     + (f", sent {str(r.get('sent_at', ''))[:10]}" if r.get("sent_at") else ""))
        prep = r.get("interview") or {}
        for x in (prep.get("questions") or [])[:8]:
            lines.append(f"  Likely question: {x.get('question', '')}")
            if (x.get("gap") or "").strip():
                lines.append(f"    Gap: {x['gap']}")
    people = _read("contacts.json") or {}
    if isinstance(people, dict) and q:
        known = [c for c in people.values()
                 if q & set(_words(" ".join(c.get("companies") or []) + " " + (c.get("name") or "")))]
        if known:
            lines.append("\nPEOPLE THE USER HAS DEALT WITH")
            for c in known[:5]:
                what = ", ".join(f"{n} {k}" for k, n in (c.get("kinds") or {}).items())
                lines.append(f"- {c.get('name') or c.get('email')} <{c.get('email')}> — "
                             f"{', '.join(c.get('companies') or [])}"
                             + (f"; {what}" if what else "")
                             + (f"; last heard {str(c.get('last', ''))[:10]}" if c.get("last") else "")
                             + (f"; notes: {c['notes'][:200]}" if c.get("notes") else ""))
    return "\n".join(lines)


def lookup(query: str, k: int = 5) -> str:
    """What the job search knows that answers `query`, as text for the chat."""
    st = status()
    apps = applications(query)
    if not st["found"] or not (st["cv_sections"] or st["answers"] or st["profile"] or apps):
        return ("Agent Jo Jobs has nothing on the user's career yet. Tell them they "
                "can paste or upload their CV under Your profile in Agent Jo Jobs.")
    parts = []
    prof = _read("profile.json") or {}
    keep = ("full_name", "headline", "summary", "location", "target_roles", "skills",
            "technologies", "employers", "certifications", "years_experience",
            "achievements", "availability")
    prof = {k2: v for k2, v in prof.items() if k2 in keep and v not in (None, "", [], {})}
    if prof:
        parts.append("PROFILE\n" + json.dumps(prof, ensure_ascii=False)[:2500])
    chunks = (_read("cv/chunks.json") or {}).get("chunks") or []
    hits = _bm25(query, [(c.get("heading", "") + " " + c.get("text", ""), c) for c in chunks], k)
    if not hits and chunks:
        hits = chunks[:2]                       # a general question: the top of the CV
    for c in hits:
        parts.append("CV" + (f" — {c['heading']}" if c.get("heading") else "") + "\n" + c["text"])
    answers = [a for a in (_read("answers.json") or []) if a.get("reuse", True)]
    got = _bm25(query, [(a["question"] + " " + a["answer"], a) for a in answers], 3)
    if got:
        parts.append("ANSWERS THE USER HAS GIVEN APPLICATION FORMS\n" + "\n".join(
            f"Q: {a['question']}\nA: {a['answer']}" for a in got))
    if apps:
        parts.append(apps)
    out = "\n\n".join(parts)
    return (out[:9000] + "\n\n(From the user's own CV and job profile in Agent Jo Jobs. "
            "Use only what is here; don't add employers, dates, tools or numbers it "
            "doesn't state.)")
