"""Portal applications — filling the forms the email path can't reach.

Most adverts don't give you an address. They give you a Greenhouse, Lever,
Workable, Ashby or Workday form, and until now those were tracked and drafted
but left entirely to you.

This drives a real browser and fills them. Three decisions shape it:

  A PERSISTENT BROWSER PROFILE. Sessions are kept in one profile directory, so
  when you sign in to Greenhouse once, every later Greenhouse application
  reuses that session. That is what makes "only involve me for login" true
  rather than aspirational — otherwise you'd be asked on every single form.

  IT HANDS OVER RATHER THAN GUESSING. A login wall, a CAPTCHA, a question it
  has no answer for in your profile, or a field it can't identify — it stops,
  leaves the browser open on that page, and says exactly what it needs. It
  does not invent an answer to "why do you want to work here", and it does not
  attempt CAPTCHAs.

  THE BROWSER IS VISIBLE. Headless would be faster and would also mean you
  couldn't see what was typed into a form submitted under your name. You can
  watch it, and you can take over mid-way.

Submission is gated separately from filling. The default is to fill everything
and stop, so you read it once and press submit yourself; turning that off
makes it submit unattended. Same shape as the email path, and for the same
reason: a wrong application can't be recalled.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import config

# Handover reasons, in the order they're checked.
NEEDS_LOGIN = "needs_login"
NEEDS_CAPTCHA = "needs_captcha"
NEEDS_ANSWER = "needs_answer"
UNKNOWN_FORM = "unknown_form"
FILLED = "filled"
SUBMITTED = "submitted"
FAILED = "failed"


def _dir() -> Path:
    d = config.AGENT_HOME / "portal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_dir() -> Path:
    """The browser profile. Logins live here, which is the whole point."""
    d = _dir() / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
#  which applicant tracking system are we looking at
# --------------------------------------------------------------------------- #
ATS_HOSTS = {
    "greenhouse": ("greenhouse.io", "boards.greenhouse.io",
                   "job-boards.greenhouse.io"),
    "lever": ("lever.co", "jobs.lever.co"),
    "workable": ("workable.com", "apply.workable.com"),
    "ashby": ("ashbyhq.com", "jobs.ashbyhq.com"),
    "smartrecruiters": ("smartrecruiters.com",),
    "recruitee": ("recruitee.com",),
    "teamtailor": ("teamtailor.com",),
    "bamboohr": ("bamboohr.com",),
    "workday": ("myworkdayjobs.com", "wd1.myworkdayjobs.com",
                "wd3.myworkdayjobs.com", "wd5.myworkdayjobs.com"),
}

# Markers in the page itself, for adverts that embed the form on their own
# domain — the host tells you nothing in that case.
ATS_MARKERS = {
    "greenhouse": ("greenhouse.io/embed", "grnhse", "#grnhse_app"),
    "lever": ("lever.co/", "lever-application"),
    "workable": ("workable.com/api", "whr-"),
    "ashby": ("ashbyhq.com/api", "_ashby"),
    "workday": ("workday", "wd-"),
}


def detect_ats(url: str = "", html: str = "") -> str:
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        pass
    for name, hosts in ATS_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    low = (html or "").lower()
    for name, marks in ATS_MARKERS.items():
        if any(m in low for m in marks):
            return name
    return "unknown"


# --------------------------------------------------------------------------- #
#  what we can answer, and what we can't
# --------------------------------------------------------------------------- #
# Field patterns are matched against a form control's name, id, label and
# placeholder together — every ATS names things differently, and matching on
# meaning rather than one vendor's markup is what makes this work on a form
# nobody has seen before.
FIELD_PATTERNS = [
    ("first_name", r"first[\s_-]*name|given[\s_-]*name|\bfname\b"),
    ("last_name", r"last[\s_-]*name|surname|family[\s_-]*name|\blname\b"),
    ("full_name", r"^\s*(full[\s_-]*)?name\s*$|your[\s_-]*name"),
    ("email", r"e[\s_-]*mail"),
    ("phone", r"phone|mobile|cell|telephone"),
    ("location", r"location|city|town|where.*based|current.*location"),
    ("linkedin", r"linked[\s_-]*in"),
    ("github", r"git[\s_-]*hub"),
    ("website", r"website|portfolio|personal[\s_-]*site"),
    ("resume", r"resume|cv\b|curriculum"),
    ("cover_letter", r"cover[\s_-]*letter|why.*(you|interest)|message"),
    ("notice", r"notice[\s_-]*period|availability|start[\s_-]*date"),
    ("salary", r"salary|rate|compensation|expectation"),
    ("work_auth", r"authori[sz]ed|right[\s_-]*to[\s_-]*work|visa|sponsor"),
]

# Things we will not answer on someone's behalf without being told.
# Matched as substrings, so each stem has to cover the words that actually
# appear on forms: "disabled" and "disability", "ethnicity" and "ethnic
# background". "disabilit" missed "Do you identify as disabled?" entirely.
SENSITIVE = ("gender", "race", "ethnic", "disabl", "veteran", "sexual",
             "religio", "date of birth", "birth date", "marital",
             "pregnan", "identity number", "id number", "national id",
             "criminal", "salary history")


def classify_field(label: str) -> str:
    text = " ".join((label or "").lower().split())
    if not text:
        return ""
    if any(s in text for s in SENSITIVE):
        return "sensitive"
    for name, pattern in FIELD_PATTERNS:
        if re.search(pattern, text):
            return name
    return ""


def answer_for(kind: str, profile: dict, role: dict) -> str:
    """What to type, or '' when we genuinely don't know."""
    p = profile or {}
    name = str(p.get("full_name") or "").strip()
    first, last = "", ""
    if name:
        parts = name.split()
        first, last = parts[0], (" ".join(parts[1:]) if len(parts) > 1 else "")
    table = {
        "first_name": p.get("first_name") or first,
        "last_name": p.get("last_name") or last,
        "full_name": name,
        "email": p.get("email", ""),
        "phone": p.get("phone", ""),
        "location": p.get("location", ""),
        "linkedin": p.get("linkedin", ""),
        "github": p.get("github", ""),
        "website": p.get("website", ""),
        "notice": p.get("availability", ""),
        "salary": p.get("rate", ""),
        "cover_letter": ((role or {}).get("draft") or {}).get("body", ""),
        "resume": p.get("cv_path", ""),
    }
    return str(table.get(kind, "") or "").strip()


_ANSWER_SYSTEM = (
    "You are filling in a job application form on behalf of a candidate.\n"
    "You are given the candidate's PROFILE, the ROLE, and the form's "
    "QUESTIONS.\n\n"
    "Answer each question using ONLY what is in the PROFILE. If the profile "
    "does not support an answer, return an empty string for that question — "
    "an unanswered question is fine; an invented one is not. Never state a "
    "tool, an employer, a qualification or a number of years that is not in "
    "the profile.\n\n"
    "Write plainly, in the first person, no buzzwords. A few sentences at "
    "most unless the question asks for more. For yes/no questions answer "
    "'Yes' or 'No' only when the profile settles it.\n\n"
    "Return JSON only: {\"answers\": [{\"question\": \"...\", "
    "\"answer\": \"...\"}]}"
)


def answer_questions(role: dict, profile: dict, questions: list,
                     brain, model=None) -> list:
    """Answer a form's free-text questions from the profile, and nothing else.

    Portal forms ask things no field table can cover — "why this role", "how
    many years with Power BI", "notice period". Those were left blank and the
    application stalled there. The engine answers them, and every answer goes
    through the same claims check a drafted email does, so a form can't claim
    what a covering letter wouldn't be allowed to.
    """
    questions = [q for q in (questions or []) if str(q).strip()]
    if not questions or brain is None:
        return [{"question": q, "answer": "", "source": "blank",
                 "why": "no engine available to answer it"} for q in questions]
    from . import jobscout
    payload = json.dumps({"PROFILE": profile or {}, "ROLE": role or {},
                          "QUESTIONS": questions}, default=str)[:12000]
    try:
        kw = {"model": model} if model else {}
        raw = brain.chat([{"role": "user", "content": payload}],
                         [_ANSWER_SYSTEM + jobscout._banned_clause()],
                         None, **kw)
        text = "".join(b.text for b in getattr(raw, "content", [])
                       if getattr(b, "type", "") == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception as exc:
        return [{"question": q, "answer": "", "source": "blank",
                 "why": f"couldn't answer: {type(exc).__name__}"}
                for q in questions]

    by_q = {str(a.get("question", "")).strip().lower():
            str(a.get("answer", "")).strip()
            for a in (data.get("answers") or []) if isinstance(a, dict)}
    out = []
    for q in questions:
        ans = by_q.get(str(q).strip().lower(), "")
        if not ans:
            out.append({"question": q, "answer": "", "source": "blank",
                        "why": "your profile doesn't answer this — type it "
                               "yourself"})
            continue
        check = jobscout.check_draft(ans, role or {})
        if check.get("ok") is False:
            out.append({"question": q, "answer": ans, "source": "held",
                        "why": "; ".join(p.get("detail", "")
                                         for p in check.get("problems", []))
                               or "claims something your profile can't support"})
        else:
            out.append({"question": q, "answer": ans, "source": "engine",
                        "why": ""})
    return out


def _offer_companion(driver, page, hints: list) -> bool:
    """Put the companion in the page if this driver can.

    Help inside the form is a nicety; failing to offer it must never fail an
    application. A driver without the method — or a page that refuses the
    script — simply doesn't get one.
    """
    fn = getattr(driver, "companion", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(page, hints))
    except Exception:
        return False


def companion_hints(fill: list, answers: list, fields: list) -> list:
    """What to offer for each field on the page, in the page's own words."""
    hints = []
    for f in fill or []:
        if f.get("value"):
            hints.append({"label": f.get("label") or f.get("kind"),
                          "value": f["value"], "source": "profile"})
    for a in answers or []:
        hints.append({"label": a.get("question", ""),
                      "value": a.get("answer", ""),
                      "source": a.get("source", ""),
                      "why": a.get("why", "")})
    # fields nothing was worked out for still get an honest answer
    known = {str(h["label"]).strip().lower() for h in hints}
    for f in fields or []:
        label = (f.get("label") or f.get("name") or "").strip()
        if label and label.lower() not in known:
            hints.append({"label": label, "value": "",
                          "why": "Not in your profile — type it yourself."})
    return hints


def paste_pack(answers: list, filled: list) -> str:
    """Everything the form needs, as text you can paste by hand.

    Automation gets some way into most forms and stops — a file upload it
    can't reach, a question in a widget, a captcha. Having to retype what the
    app already worked out is the moment people give up, so it hands the
    answers over in a form you can paste.
    """
    lines = []
    for f in filled or []:
        if f.get("value"):
            lines.append(f"{f.get('label') or f.get('kind')}:\n{f['value']}\n")
    for a in answers or []:
        if a.get("answer"):
            mark = "  [held — check this]" if a.get("source") == "held" else ""
            lines.append(f"{a['question']}{mark}:\n{a['answer']}\n")
    return "\n".join(lines).strip()


def plan(role: dict, profile: dict, fields: list) -> dict:
    """Given the form's fields, work out what can be filled and what can't —
    before touching anything, so a form that can't be completed doesn't get
    half-finished."""
    fill, missing, sensitive, unknown = [], [], [], []
    for f in fields or []:
        label = f.get("label") or f.get("name") or f.get("id") or ""
        kind = classify_field(label)
        if kind == "sensitive":
            sensitive.append(label)
            continue
        if not kind:
            if f.get("required"):
                unknown.append(label)
            continue
        value = answer_for(kind, profile, role)
        if value:
            fill.append({"kind": kind, "label": label,
                         "selector": f.get("selector", ""),
                         "type": f.get("type", "text"), "value": value})
        elif f.get("required"):
            missing.append({"kind": kind, "label": label})
    return {"fill": fill, "missing": missing, "sensitive": sensitive,
            "unknown_required": unknown,
            "can_complete": not missing and not unknown}


# --------------------------------------------------------------------------- #
#  page signals
# --------------------------------------------------------------------------- #
_LOGIN_MARKS = ("sign in", "log in", "login", "create an account",
                "create account", "sign up", "continue with google",
                "password")
_CAPTCHA_MARKS = ("recaptcha", "hcaptcha", "cf-turnstile", "captcha",
                  "i'm not a robot")


def page_state(html: str, url: str = "") -> str:
    """What kind of page are we on? Checked before filling, because typing
    into a login form is worse than stopping."""
    low = (html or "").lower()
    if any(m in low for m in _CAPTCHA_MARKS):
        return NEEDS_CAPTCHA
    # a password field is the reliable signal; the words appear in footers
    if re.search(r"<input[^>]+type=[\"']password[\"']", low):
        return NEEDS_LOGIN
    if sum(1 for m in _LOGIN_MARKS if m in low) >= 3 and "apply" not in low:
        return NEEDS_LOGIN
    return ""


def handover_message(state: str, detail: str = "") -> str:
    if state == NEEDS_LOGIN:
        return ("This one needs you signed in. The browser is open on the "
                "page — sign in or create the account, then run it again: "
                "the session is saved, so you won't be asked for this site "
                "again.")
    if state == NEEDS_CAPTCHA:
        return ("There's a CAPTCHA on this form. Solve it in the open "
                "browser and run it again — automating that would be both "
                "unreliable and a good way to get blocked.")
    if state == NEEDS_ANSWER:
        return ("The form asks something your profile doesn't answer: "
                + detail + ". Add it to your profile, or type it in the open "
                "browser and press submit yourself.")
    if state == UNKNOWN_FORM:
        return ("This form doesn't look like an ATS I can fill reliably"
                + (f" ({detail})" if detail else "")
                + ". It's open in the browser for you.")
    return detail


def log(entry: dict) -> None:
    try:
        with open(_dir() / "runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**entry, "at": _iso()}) + "\n")
    except Exception:
        pass


def history(n: int = 30) -> list:
    try:
        lines = (_dir() / "runs.jsonl").read_text("utf-8").splitlines()
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


def _site_of(url: str) -> str:
    """The host, which is what a form belongs to — not the advert's path."""
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _recipes_path() -> Path:
    return _dir() / "site_recipes.json"


def recipes() -> dict:
    try:
        return json.loads(_recipes_path().read_text("utf-8")) or {}
    except Exception:
        return {}


def recipe_for(url: str) -> dict:
    """What this site asked for last time.

    Every application teaches the app something about the site: which system
    runs the form, whether it wanted a sign-in, whether a captcha appeared,
    which fields it asks for and which questions it repeats. The next
    application to the same host starts knowing all of that instead of
    discovering it again.
    """
    return recipes().get(_site_of(url)) or {}


def remember_site(url: str, *, ats: str = "", state: str = "",
                  fields: list | None = None, questions: list | None = None,
                  filled: list | None = None, blocked: str = "") -> dict:
    """Record what this application taught us about the site."""
    site = _site_of(url)
    if not site:
        return {}
    all_r = recipes()
    r = all_r.get(site) or {"site": site, "seen": 0, "questions": {},
                            "fields": {}, "steps": []}
    r["seen"] = int(r.get("seen", 0)) + 1
    r["ats"] = ats or r.get("ats", "")
    r["last_state"] = state
    r["last_at"] = _iso()
    if blocked:
        r["blocks"] = sorted(set((r.get("blocks") or []) + [blocked]))
    for f in fields or []:
        label = (f.get("label") or f.get("name") or "").strip()
        if label:
            known = r["fields"].setdefault(label, {"seen": 0, "kind": ""})
            known["seen"] += 1
            known["kind"] = classify_field(label) or known.get("kind", "")
            if f.get("required"):
                known["required"] = True
    for q in questions or []:
        if str(q).strip():
            r["questions"][str(q).strip()] = \
                int(r["questions"].get(str(q).strip(), 0)) + 1
    # the shortest honest description of what it takes to apply here
    steps = []
    if "needs_login" in (r.get("blocks") or []):
        steps.append("sign in")
    if "needs_captcha" in (r.get("blocks") or []):
        steps.append("solve a captcha")
    if any(k.get("kind") == "resume" for k in r["fields"].values()):
        steps.append("attach your CV")
    if r["questions"]:
        steps.append(f"answer {len(r['questions'])} question(s)")
    steps.append("submit")
    r["steps"] = steps
    all_r[site] = r
    try:
        _recipes_path().parent.mkdir(parents=True, exist_ok=True)
        _recipes_path().write_text(json.dumps(all_r, indent=2), "utf-8")
    except Exception:
        pass
    return r


def site_brief(url: str) -> str:
    """One line about what applying here takes, for before you start."""
    r = recipe_for(url)
    if not r or not r.get("seen"):
        return ""
    bits = [f"You've applied through {r['site']} {r['seen']} time(s)"]
    if r.get("ats"):
        bits.append(f"it runs {r['ats']}")
    if r.get("steps"):
        bits.append("it takes: " + ", ".join(r["steps"]))
    return " — ".join(bits) + "."


def readiness(profile: dict) -> dict:
    """Portal forms ask for things an email application never did. Better to
    find that out once here than to abandon twenty half-filled forms."""
    p = profile or {}
    need = {"full_name": "your name", "email": "an email address",
            "phone": "a phone number", "location": "your location",
            "cv_path": "a path to your CV file"}
    missing = [label for key, label in need.items()
               if not str(p.get(key) or "").strip()]
    cv = str(p.get("cv_path") or "").strip()
    if cv and not Path(cv).exists():
        missing.append(f"a CV at {cv} (that file isn't there)")
    return {"ready": not missing, "missing": missing}


# --------------------------------------------------------------------------- #
#  the browser
#
#  Split deliberately: everything above decides WHAT to do and is pure enough
#  to test without a browser; this decides HOW. A driver can be substituted,
#  which is how the sequence gets exercised in a container that has no
#  Chromium and no display.
# --------------------------------------------------------------------------- #
DRIVER = None          # tests substitute this


# A small companion inside the form itself. Hover a field and it shows what
# it would type there and why — click to fill it, or copy it. It exists
# because automation never finishes every form: the moment it stops, you are
# on your own in a page full of boxes, holding answers the app already
# worked out.
_COMPANION_JS = r"""
(hints) => {
  if (window.__agentJoCompanion) { window.__agentJoCompanion.update(hints); return true; }
  const norm = (s) => (s || "").toLowerCase().replace(/[\s:*]+/g, " ").trim();
  let table = {};
  const load = (h) => {
    table = {};
    (h || []).forEach((x) => { if (x && x.label) table[norm(x.label)] = x; });
  };
  load(hints);

  const box = document.createElement("div");
  box.style.cssText = [
    "position:fixed", "z-index:2147483647", "max-width:320px",
    "font:13px/1.45 -apple-system,Segoe UI,system-ui,sans-serif",
    "background:rgba(18,26,29,.97)", "color:#eef4f2",
    "border:1px solid rgba(70,211,154,.45)", "border-radius:12px",
    "box-shadow:0 12px 34px rgba(0,0,0,.45)", "padding:10px 12px",
    "pointer-events:auto", "display:none", "transition:opacity .12s",
  ].join(";");
  document.documentElement.appendChild(box);

  const dot = document.createElement("div");
  dot.style.cssText = [
    "position:fixed", "z-index:2147483646", "width:14px", "height:14px",
    "border-radius:50%", "background:#46d39a",
    "box-shadow:0 0 0 4px rgba(70,211,154,.25)", "pointer-events:none",
    "transform:translate(-50%,-50%)", "transition:opacity .15s", "opacity:0",
  ].join(";");
  document.documentElement.appendChild(dot);

  let current = null;
  const labelFor = (el) => {
    let t = "";
    if (el.labels && el.labels[0]) t = el.labels[0].innerText;
    if (!t && el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) t = l.innerText;
    }
    if (!t) t = el.getAttribute("aria-label") || el.placeholder || el.name || "";
    return t;
  };
  const show = (el, x, y) => {
    const hit = table[norm(labelFor(el))];
    box.innerHTML = "";
    const head = document.createElement("div");
    head.style.cssText = "font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7f9a92;margin-bottom:5px";
    head.textContent = "Agent Jo";
    box.appendChild(head);
    if (!hit || !hit.value) {
      const p = document.createElement("div");
      p.style.color = "#c6cfcb";
      p.textContent = hit && hit.why
        ? hit.why
        : "Your profile doesn't answer this one — type it yourself.";
      box.appendChild(p);
    } else {
      const v = document.createElement("div");
      v.style.cssText = "white-space:pre-wrap;max-height:160px;overflow:auto";
      v.textContent = hit.value;
      box.appendChild(v);
      if (hit.source === "held") {
        const w = document.createElement("div");
        w.style.cssText = "margin-top:6px;color:#f3c46d;font-size:12px";
        w.textContent = "Held: " + (hit.why || "claims more than your profile");
        box.appendChild(w);
      }
      const bar = document.createElement("div");
      bar.style.cssText = "display:flex;gap:6px;margin-top:8px";
      const mk = (text, fn) => {
        const b = document.createElement("button");
        b.textContent = text;
        b.style.cssText = "cursor:pointer;border:0;border-radius:7px;padding:5px 10px;font:inherit;font-size:12px;background:#46d39a;color:#04200f";
        b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); fn(b); };
        return b;
      };
      bar.appendChild(mk("Fill this", () => {
        const setter = Object.getOwnPropertyDescriptor(
          el.constructor.prototype, "value");
        if (setter && setter.set) setter.set.call(el, hit.value);
        else el.value = hit.value;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }));
      const copy = mk("Copy", (b) => {
        navigator.clipboard.writeText(hit.value).then(() => {
          b.textContent = "Copied";
          setTimeout(() => { b.textContent = "Copy"; }, 1200);
        }).catch(() => {});
      });
      copy.style.background = "rgba(255,255,255,.1)";
      copy.style.color = "#eef4f2";
      bar.appendChild(copy);
      box.appendChild(bar);
    }
    box.style.display = "block";
    const w = box.getBoundingClientRect();
    box.style.left = Math.min(x + 16, innerWidth - w.width - 12) + "px";
    box.style.top = Math.min(y + 16, innerHeight - w.height - 12) + "px";
  };

  const isField = (el) => el && /^(input|textarea|select)$/i.test(el.tagName)
    && !/^(hidden|submit|button)$/i.test(el.type || "");

  document.addEventListener("mousemove", (e) => {
    dot.style.left = e.clientX + "px";
    dot.style.top = e.clientY + "px";
    const el = e.target;
    if (isField(el)) {
      dot.style.opacity = "1";
      if (el !== current) { current = el; show(el, e.clientX, e.clientY); }
      else {
        const w = box.getBoundingClientRect();
        box.style.left = Math.min(e.clientX + 16, innerWidth - w.width - 12) + "px";
        box.style.top = Math.min(e.clientY + 16, innerHeight - w.height - 12) + "px";
      }
    } else if (!box.contains(el)) {
      dot.style.opacity = "0";
      current = null;
      box.style.display = "none";
    }
  }, true);

  window.__agentJoCompanion = { update: load };
  return true;
}
"""


class PlaywrightDriver:
    """A visible Chromium with a persistent profile, so logins survive."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._ctx = None

    def open(self, url: str):
        """Open a page, launching the browser only the first time.

        This launched a whole browser on every call. A cycle fetches every
        source in turn, so ten sources meant ten browsers against one profile
        — which Chromium turns into a heap of blank tabs, one real page among
        them. The browser is launched once per driver and the pages are
        reused.
        """
        from playwright.sync_api import sync_playwright
        if self._ctx is None:
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(profile_dir()), headless=self.headless,
                viewport={"width": 1280, "height": 900},
                accept_downloads=True)
        # the context always starts with one blank page — use it rather than
        # leaving it behind and adding another
        blank = [p for p in self._ctx.pages if p.url in ("", "about:blank")]
        page = blank[0] if blank else self._ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return page

    def close_page(self, page) -> None:
        """Done with one page; the browser stays for the next URL."""
        try:
            if page and len(self._ctx.pages) > 1:
                page.close()
            elif page:
                page.goto("about:blank")       # keep one, so nothing reopens
        except Exception:
            pass

    def fields(self, page) -> list:
        """Every visible control, with its label — matching on meaning needs
        the label, not just the name attribute."""
        return page.evaluate("""() => {
          const out = [];
          const seen = new Set();
          document.querySelectorAll(
            "input, textarea, select").forEach((el, i) => {
            if (el.type === "hidden" || el.disabled) return;
            const r = el.getBoundingClientRect();
            if (!r.width && !r.height && el.type !== "file") return;
            let label = "";
            if (el.id) {
              const l = document.querySelector(
                `label[for="${CSS.escape(el.id)}"]`);
              if (l) label = l.innerText;
            }
            if (!label && el.closest("label")) {
              label = el.closest("label").innerText;
            }
            label = [label, el.name, el.id, el.placeholder,
                     el.getAttribute("aria-label")]
                    .filter(Boolean).join(" ");
            const sel = el.id ? `#${CSS.escape(el.id)}`
                      : el.name ? `${el.tagName.toLowerCase()}[name="${el.name}"]`
                      : null;
            if (!sel || seen.has(sel)) return;
            seen.add(sel);
            out.push({selector: sel, label, type: el.type || el.tagName.toLowerCase(),
                      required: el.required || el.getAttribute("aria-required") === "true"});
          });
          return out;
        }""")

    def html(self, page) -> str:
        return page.content()

    def fill(self, page, item) -> bool:
        try:
            if item["type"] == "file":
                page.set_input_files(item["selector"], item["value"])
            elif item["type"] in ("select-one", "select"):
                page.select_option(item["selector"], label=item["value"])
            else:
                page.fill(item["selector"], item["value"])
            return True
        except Exception:
            return False

    def submit(self, page) -> bool:
        for sel in ("button[type=submit]", "input[type=submit]",
                    "text=/^\\s*(submit|submit application|apply)\\s*$/i"):
            try:
                page.click(sel, timeout=4000)
                page.wait_for_load_state("networkidle", timeout=20000)
                return True
            except Exception:
                continue
        return False

    def companion(self, page, hints: list) -> bool:
        """Put the companion in the page. Never fatal: a form you can't be
        helped on is still a form you can fill."""
        try:
            return bool(page.evaluate(_COMPANION_JS, hints))
        except Exception:
            return False

    def shot(self, page, name: str) -> str:
        path = _dir() / f"{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return ""

    def close(self, keep_open: bool = True):
        # left open on purpose when a human is needed — closing it would
        # discard the very page they have to act on
        if keep_open:
            return
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


WAITING = "waiting_for_you"
RUNNING = "running"
CANCELLED = "cancelled"

# Live sessions, keyed by role. A portal application can sit waiting for a
# person for minutes, so it runs on its own thread and reports progress here;
# the window polls it. An unattended run never waits — it records what needs
# you and moves on.
_sessions: dict = {}
_sessions_lock = threading.Lock()


def session(key: str) -> dict:
    with _sessions_lock:
        return dict(_sessions.get(key) or {})


def _set_session(key: str, **fields) -> None:
    with _sessions_lock:
        s = _sessions.setdefault(key, {})
        s.update(fields)
        s["at"] = _iso()


def session_continue(key: str) -> dict:
    """The person says they've handled it. Checked by the waiting loop."""
    _set_session(key, resume=True)
    return session(key)


def session_cancel(key: str) -> dict:
    _set_session(key, cancel=True)
    return session(key)


def _wait_for_person(key: str, driver, page, state: str,
                     timeout_s: float = 900.0) -> bool:
    """Stop, ask for help, and carry on once it's handled.

    A sign-in page or a captcha used to end the attempt: it reported what it
    saw and closed. But the browser is already open on the person's own
    machine, so the useful thing is to wait — they sign in or solve it, and
    the application continues from where it stopped. It notices by itself when
    the page clears, and there's a Continue button for when it can't tell.
    """
    _set_session(key, state=WAITING, blocked_by=state,
                 message=handover_message(state),
                 screenshot=driver.shot(page, "waiting"),
                 waiting_since=_iso(), resume=False, cancel=False)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = session(key)
        if s.get("cancel"):
            return False
        try:
            if page_state(driver.html(page), "") == "":
                return True                    # they handled it
        except Exception:
            pass                               # page mid-navigation; look again
        if s.get("resume"):
            return True                        # they say it's handled
        time.sleep(2)
    _set_session(key, message="Nobody came back within 15 minutes, so it "
                              "stopped. Nothing was submitted.")
    return False


def start_sign_in(url: str, label: str = "") -> dict:
    """Open a site so you can sign in once, and keep the session.

    Expert marketplaces publish nothing until you're signed in. The browser
    runs on a profile that persists, so signing in here is remembered for
    every later fetch and application — this is the difference between a
    source that can never return anything and one that works.
    """
    key = "signin:" + _site_of(url)
    with _sessions_lock:
        if (_sessions.get(key) or {}).get("state") in (RUNNING, WAITING):
            return dict(_sessions[key])
        _sessions[key] = {"state": RUNNING, "at": _iso(), "key": key,
                          "title": label or _site_of(url)}

    def _run():
        driver = acquire_driver(headless=False)
        try:
            page = driver.open(normalise_url_safe(url))
            _set_session(key, state=WAITING, blocked_by=NEEDS_LOGIN,
                         message=("Sign in on the window that just opened. "
                                  "Press Done when you're in — the session is "
                                  "kept for later fetches."),
                         resume=False, cancel=False)
            deadline = time.time() + 900
            while time.time() < deadline:
                s = session(key)
                if s.get("cancel"):
                    _set_session(key, state=CANCELLED, done=True,
                                 message="Left as it was.")
                    return
                if s.get("resume"):
                    break
                time.sleep(2)
            _set_session(key, state=FILLED, done=True,
                         message=(f"Signed in to {_site_of(url)}. The browser "
                                  f"keeps it, so fetches and applications "
                                  f"will use it."))
        except Exception as exc:
            _set_session(key, state=FAILED, done=True,
                         message=_explain_portal_error(exc))
        finally:
            # give the browser back rather than closing it: a fetch or an
            # application may be using the same one, and closing the profile
            # from under them is what produced "already in use"
            release_driver(close=False)

    threading.Thread(target=_run, daemon=True, name=f"signin-{key}").start()
    return session(key)


def normalise_url_safe(url: str) -> str:
    u = (url or "").strip()
    return u if u.startswith(("http://", "https://")) else "https://" + u


def _open_with_help(role: dict, profile_data: dict, key: str,
                    brain=None, model=None) -> dict:
    driver = acquire_driver(headless=False)
    url = normalise_url_safe(str((role or {}).get("url") or ""))
    try:
        page = driver.open(url)
        fields = driver.fields(page)
        p = plan(role, profile_data, fields)
        answers = []
        if p["unknown_required"]:
            answers = answer_questions(role, profile_data,
                                       p["unknown_required"], brain, model)
        hints = companion_hints(p["fill"], answers, fields)
        ok = _offer_companion(driver, page, hints)
        _set_session(key, state=FILLED, done=True, url=url,
                     answers=answers, companion=ok,
                     paste_pack=paste_pack(answers, p["fill"]),
                     site=site_brief(url),
                     message=("The form is open with the helper in it — hover "
                              "any field to see what to put there. Nothing "
                              "was filled in or sent."
                              if ok else
                              "The form is open. The helper couldn't load in "
                              "this page, so use the answers below."))
        remember_site(url, ats=detect_ats(url, driver.html(page)),
                      state="helper", fields=fields,
                      questions=p["unknown_required"])
    except Exception as exc:
        _set_session(key, state=FAILED, done=True,
                     message=_explain_portal_error(exc))
    # the window stays open — you are about to work in it — but the browser
    # is handed back so nothing else has to launch a second one
    release_driver(close=False)
    return session(key)


def start_helper(role: dict, profile_data: dict, *, brain=None,
                 model=None) -> dict:
    """Open a role's form with the companion in it, and fill in nothing.

    The companion only exists in a window this app opened — it is injected
    into that page. Browsing a form in your own browser will never show it,
    which is the commonest reason for "I can't see it". This opens the form
    the way the app can help with.
    """
    key = "help:" + str((role or {}).get("key") or "role")
    with _sessions_lock:
        if (_sessions.get(key) or {}).get("state") == RUNNING:
            return dict(_sessions[key])
        _sessions[key] = {"state": RUNNING, "at": _iso(), "key": key,
                          "title": (role or {}).get("title", "")}
    threading.Thread(
        target=_open_with_help, daemon=True, name=f"help-{key}",
        args=(role, profile_data, key), kwargs={"brain": brain,
                                                "model": model}).start()
    return session(key)


def start_apply(role: dict, profile_data: dict, *, submit: bool = False,
                brain=None, model=None) -> dict:
    """Begin a portal application in the background and return at once.

    Waiting for a person can take minutes; a browser request can't be held
    open that long, so the work runs on its own thread and the window follows
    it through session().
    """
    key = str((role or {}).get("key") or (role or {}).get("url") or "role")
    with _sessions_lock:
        live = _sessions.get(key) or {}
        if live.get("state") in (RUNNING, WAITING):
            return dict(live)                  # already going; don't start twice
        _sessions[key] = {"state": RUNNING, "at": _iso(), "key": key,
                          "title": (role or {}).get("title", "")}

    def _run():
        try:
            res = apply_to_portal(role, profile_data, submit=submit,
                                  brain=brain, model=model, wait=True,
                                  session_key=key)
        except Exception as exc:
            res = {"ok": False, "state": FAILED,
                   "message": _explain_portal_error(exc)}
        _set_session(key, **{**res, "state": res.get("state", FAILED),
                             "done": True})

    threading.Thread(target=_run, daemon=True,
                     name=f"portal-{key}").start()
    return session(key)


class ThreadBoundDriver:
    """The browser, owned by one thread and driven from any.

    Playwright's synchronous objects belong to the thread that created them —
    touching them from another gives "cannot switch to a different thread
    (which happens to have exited)". Sharing one browser across the app was
    right; sharing it across threads is not. So the browser lives on a thread
    of its own and every call is posted to it, which keeps one browser and
    one profile while fetching, applying and the helper each run where they
    like.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._real = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="browser")
        self._thread.start()

    def _serve(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, box = job
            try:
                box["value"] = fn()
            except BaseException as exc:        # noqa: BLE001 — relayed below
                box["error"] = exc
            finally:
                box["done"].set()

    def _call(self, fn, timeout: float = 180.0):
        if threading.current_thread() is self._thread:
            return fn()                        # already on the owner thread
        box = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            raise TimeoutError("the browser didn't answer in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _ensure(self):
        if self._real is None:
            self._real = PlaywrightDriver(headless=self.headless)
        return self._real

    # every method the app uses, posted to the owner thread
    def open(self, url):
        return self._call(lambda: self._ensure().open(url))

    def fields(self, page):
        return self._call(lambda: self._ensure().fields(page))

    def html(self, page):
        return self._call(lambda: self._ensure().html(page))

    def fill(self, page, item):
        return self._call(lambda: self._ensure().fill(page, item))

    def submit(self, page):
        return self._call(lambda: self._ensure().submit(page))

    def shot(self, page, name):
        return self._call(lambda: self._ensure().shot(page, name))

    def companion(self, page, hints):
        return self._call(lambda: self._ensure().companion(page, hints))

    def close_page(self, page):
        return self._call(lambda: self._ensure().close_page(page))

    def close(self, keep_open: bool = True):
        def _shut():
            if self._real is not None:
                self._real.close(keep_open=keep_open)
                if not keep_open:
                    self._real = None
        try:
            self._call(_shut, timeout=60)
        finally:
            if not keep_open:
                self._jobs.put(None)           # let the owner thread finish

    @property
    def _ctx(self):
        return getattr(self._real, "_ctx", None)


# One browser for the whole application, borrowed and returned.
#
# Chromium allows a profile to be open once. Fetching a source, applying to a
# role and opening a form with help each launched their own against the same
# profile, so the second was refused — "Opening in existing browser session…
# the profile is already in use" — and the page it wanted ended up as a blank
# tab in the first browser. Logins live in that profile, so separate profiles
# are not an answer: one instance is.
_SHARED: dict = {"driver": None, "uses": 0, "headless": True}
_SHARED_LOCK = threading.RLock()


def acquire_driver(headless: bool | None = None):
    """Borrow the browser. Launches it if nobody has it yet."""
    with _SHARED_LOCK:
        if DRIVER is not None:
            return DRIVER                      # a test supplied its own
        if _SHARED["driver"] is None:
            _SHARED["driver"] = ThreadBoundDriver(
                headless=bool(_SHARED["headless"] if headless is None
                              else headless))
            _SHARED["uses"] = 0
        elif headless is False and _SHARED["headless"]:
            # somebody now needs to see it; the window is already there, so
            # it stays as it is rather than fighting over the profile
            pass
        _SHARED["uses"] += 1
        return _SHARED["driver"]


def release_driver(close: bool = False) -> None:
    """Give it back. Closes only when nobody else is using it."""
    with _SHARED_LOCK:
        if DRIVER is not None:
            # a supplied driver still hears that the session ended — it owns
            # its own lifecycle, and silently skipping this left it open
            try:
                DRIVER.close(keep_open=not close)
            except Exception:
                pass
            return
        _SHARED["uses"] = max(0, _SHARED["uses"] - 1)
        if close and _SHARED["uses"] == 0 and _SHARED["driver"] is not None:
            try:
                _SHARED["driver"].close(keep_open=False)
            except Exception:
                pass
            _SHARED["driver"] = None


def set_browser_visible(visible: bool) -> None:
    """Applications need a window you can see; fetching doesn't."""
    with _SHARED_LOCK:
        _SHARED["headless"] = not visible


def _explain_portal_error(exc: Exception) -> str:
    """Say what to do, not what the library printed.

    A portal failure is nearly always one of two set-up steps missing, and
    both were reported as a wall of Playwright output with the answer buried
    in it.
    """
    s = f"{type(exc).__name__}: {exc}"
    low = s.lower()
    if isinstance(exc, ImportError) or "no module named 'playwright'" in low:
        return ("The browser driver isn't installed. In the app folder run:  "
                ".venv\\Scripts\\python -m pip install playwright  "
                "(macOS/Linux: .venv/bin/python -m pip install playwright)")
    if "executable doesn't exist" in low or "please run the following command" in low:
        return ("Playwright is installed but its browser isn't. In the app "
                "folder run:  .venv\\Scripts\\python -m playwright install "
                "chromium  (macOS/Linux: .venv/bin/python -m playwright "
                "install chromium)")
    if "already in use" in low or "existing browser session" in low:
        return ("Another browser is already using Agent Jo's profile. Close "
                "any Chromium window the app opened and try again — if it "
                "keeps happening, restart the app.")
    if "async api" in low:
        return ("The browser was started from the wrong thread — this is a "
                "fault in the app, not your setup. Please report it.")
    if "timeout" in low:
        return ("The page didn't finish loading in time. The advert may be "
                "slow, behind a login, or blocking automated browsers.")
    if "net::" in low or "name_not_resolved" in low:
        return "Couldn't reach the advert — check the link and your connection."
    return s[:300]


def apply_to_portal(role: dict, profile_data: dict, *, submit: bool = False,
                    headless: bool = False, brain=None, model=None,
                    wait: bool = False, session_key: str = "") -> dict:
    """Open the advert, fill what we can, and either submit or hand over.

    Runs the browser off the event loop. Playwright's synchronous API refuses
    to start on a thread that has a running loop, and reports it as "use the
    Async API instead" — which reads as a coding mistake in the app rather
    than a place it was called from. A plain HTTP route is fine (those run on
    a worker thread), but a tool call inside a chat turn runs on the loop, and
    every portal application from a conversation failed with that message.
    """
    import asyncio as _asyncio
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        pass                                   # no loop here: drive it directly
    else:
        # a loop is running on this thread — do the work on one without
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=1, thread_name_prefix="portal") as _ex:
            return _ex.submit(_apply_to_portal, role, profile_data,
                              submit=submit, headless=headless,
                              brain=brain, model=model, wait=wait,
                              session_key=session_key).result()
    return _apply_to_portal(role, profile_data, submit=submit,
                            headless=headless, brain=brain, model=model,
                            wait=wait, session_key=session_key)


def _apply_to_portal(role: dict, profile_data: dict, *, submit: bool = False,
                     headless: bool = False, brain=None, model=None,
                     wait: bool = False, session_key: str = "") -> dict:
    url = str((role or {}).get("url") or "").strip()
    if not url:
        return {"ok": False, "state": FAILED,
                "message": "that role has no advert link to open"}
    ready = readiness(profile_data)
    if not ready["ready"]:
        return {"ok": False, "state": NEEDS_ANSWER,
                "message": ("Your profile is missing " +
                            ", ".join(ready["missing"]) +
                            " — portal forms ask for these every time."),
                "missing": ready["missing"]}

    driver = acquire_driver(headless=headless)
    result = {"ok": False, "state": FAILED, "url": url,
              "role": role.get("title", "")}
    page = None
    try:
        page = driver.open(url)
        html = driver.html(page)
        ats = detect_ats(url, html)
        result["ats"] = ats

        blocked = page_state(html, url)
        if blocked:
            # A sign-in or a captcha used to end it. The browser is open on
            # this machine, so it asks for you and carries on afterwards.
            if wait and session_key:
                # you are about to work in this page yourself
                _offer_companion(driver, page, companion_hints(
                    [], [], driver.fields(page)))
                if not _wait_for_person(session_key, driver, page, blocked):
                    result.update({"state": blocked,
                                   "message": handover_message(blocked),
                                   "screenshot": driver.shot(page, "handover")})
                    log(result)
                    return result
                _set_session(session_key, state=RUNNING,
                             message="Thanks — carrying on.")
                html = driver.html(page)
                result["waited_for_you"] = blocked
            else:
                result.update({"state": blocked,
                               "message": handover_message(blocked),
                               "screenshot": driver.shot(page, "handover")})
                log(result)
                return result

        fields = driver.fields(page)
        result["site"] = site_brief(url)       # what this site took last time
        # in the page from the start: it was only injected at hand-over, so
        # it appeared at the end of a session or not at all. If you are ever
        # looking at this window, it should be there.
        _offer_companion(driver, page, companion_hints([], [], fields))
        p = plan(role, profile_data, fields)
        result["plan"] = {"fill": len(p["fill"]), "missing": p["missing"],
                          "sensitive": p["sensitive"],
                          "unknown_required": p["unknown_required"]}
        # the questions no field table can cover — "why this role", "years
        # with X". These used to stop the application dead; the engine
        # answers them from the profile, and each answer is checked exactly
        # as a drafted email is.
        answers = []
        if p["unknown_required"]:
            answers = answer_questions(role, profile_data,
                                       p["unknown_required"], brain, model)
            result["answers"] = answers
            by_label = {a["question"]: a for a in answers}
            still = []
            for f in fields or []:
                label = f.get("label") or f.get("name") or f.get("id") or ""
                a = by_label.get(label)
                if a and a["answer"] and a["source"] == "engine":
                    p["fill"].append({"kind": "answer", "label": label,
                                      "selector": f.get("selector", ""),
                                      "type": f.get("type", "text"),
                                      "value": a["answer"]})
            for a in answers:
                if not a["answer"] or a["source"] != "engine":
                    still.append(a["question"])
            p["unknown_required"] = still
            p["can_complete"] = not still and not p["missing"]
            result["plan"]["unknown_required"] = still

        if not p["can_complete"]:
            # even when it can't finish, hand over what it worked out
            result["paste_pack"] = paste_pack(answers, p["fill"])
            # it can't finish, so it helps you finish: hover any field
            result["companion"] = _offer_companion(
                driver, page, companion_hints(p["fill"], answers, fields))
            detail = ", ".join(
                [m["label"] for m in p["missing"]] + p["unknown_required"])
            state = NEEDS_ANSWER if p["missing"] else UNKNOWN_FORM
            result.update({"state": state,
                           "message": handover_message(state, detail),
                           "screenshot": driver.shot(page, "handover")})
            log(result)
            return result

        filled, failed = 0, []
        for item in p["fill"]:
            if driver.fill(page, item):
                filled += 1
            else:
                failed.append(item["label"])
        result["filled"] = filled
        result["could_not_fill"] = failed
        # what was typed, in a form you can paste if anything is left by hand
        result["paste_pack"] = paste_pack(result.get("answers") or [], p["fill"])
        if failed:
            result.update({"state": UNKNOWN_FORM,
                           "message": handover_message(
                               UNKNOWN_FORM,
                               "couldn't type into: " + ", ".join(failed)),
                           "screenshot": driver.shot(page, "handover")})
            log(result)
            return result

        if not submit:
            result["companion"] = _offer_companion(driver, page, companion_hints(
                p["fill"], result.get("answers") or [], fields))
            result.update({"ok": True, "state": FILLED,
                           "message": ("Filled in and left for you to read. "
                                       "Press submit in the browser when "
                                       "you're happy with it."),
                           "screenshot": driver.shot(page, "filled")})
            log(result)
            return result

        # a sign-in or a captcha often appears only at the submit step
        late = page_state(driver.html(page), url)
        if late and wait and session_key:
            if _wait_for_person(session_key, driver, page, late):
                _set_session(session_key, state=RUNNING,
                             message="Thanks — submitting.")
                result["waited_for_you"] = late
            else:
                result.update({"state": late,
                               "message": handover_message(late),
                               "screenshot": driver.shot(page, "handover")})
                log(result)
                return result
        elif late:
            result.update({"state": late, "message": handover_message(late),
                           "screenshot": driver.shot(page, "handover")})
            log(result)
            return result

        if driver.submit(page):
            result.update({"ok": True, "state": SUBMITTED,
                           "message": "Submitted.",
                           "screenshot": driver.shot(page, "submitted")})
        else:
            result.update({"state": UNKNOWN_FORM,
                           "message": handover_message(
                               UNKNOWN_FORM,
                               "everything is filled but I couldn't find the "
                               "submit button — press it yourself"),
                           "screenshot": driver.shot(page, "filled")})
        log(result)
        return result
    except Exception as exc:
        result.update({"state": FAILED, "message": _explain_portal_error(exc)})
        log(result)
        return result
    finally:
        # every attempt teaches the app something about this site, however it
        # ended — which system runs the form, whether it wanted a sign-in,
        # what it asks. The next application starts knowing it.
        try:
            remember_site(url, ats=result.get("ats", ""),
                          state=result.get("state", ""),
                          fields=locals().get("fields") or [],
                          questions=[a["question"] for a
                                     in (result.get("answers") or [])],
                          filled=(locals().get("p") or {}).get("fill"),
                          blocked=result.get("waited_for_you")
                                  or (result.get("state") if
                                      result.get("state") in
                                      (NEEDS_LOGIN, NEEDS_CAPTCHA) else ""))
        except Exception:
            pass
        # hand it back; it closes when nobody else is using it and the
        # application actually went through
        release_driver(close=(result.get("state") == SUBMITTED))
