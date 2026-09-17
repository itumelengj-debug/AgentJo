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
import re
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


class PlaywrightDriver:
    """A visible Chromium with a persistent profile, so logins survive."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._ctx = None

    def open(self, url: str):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(profile_dir()), headless=self.headless,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True)
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return page

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


def apply_to_portal(role: dict, profile_data: dict, *, submit: bool = False,
                    headless: bool = False) -> dict:
    """Open the advert, fill what we can, and either submit or hand over."""
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

    driver = DRIVER or PlaywrightDriver(headless=headless)
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
            result.update({"state": blocked,
                           "message": handover_message(blocked),
                           "screenshot": driver.shot(page, "handover")})
            log(result)
            return result

        fields = driver.fields(page)
        p = plan(role, profile_data, fields)
        result["plan"] = {"fill": len(p["fill"]), "missing": p["missing"],
                          "sensitive": p["sensitive"],
                          "unknown_required": p["unknown_required"]}
        if not p["can_complete"]:
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
        if failed:
            result.update({"state": UNKNOWN_FORM,
                           "message": handover_message(
                               UNKNOWN_FORM,
                               "couldn't type into: " + ", ".join(failed)),
                           "screenshot": driver.shot(page, "handover")})
            log(result)
            return result

        if not submit:
            result.update({"ok": True, "state": FILLED,
                           "message": ("Filled in and left for you to read. "
                                       "Press submit in the browser when "
                                       "you're happy with it."),
                           "screenshot": driver.shot(page, "filled")})
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
        result.update({"state": FAILED,
                       "message": f"{type(exc).__name__}: {exc}"})
        log(result)
        return result
    finally:
        # keep the window open unless it actually submitted
        driver.close(keep_open=result.get("state") != SUBMITTED)
