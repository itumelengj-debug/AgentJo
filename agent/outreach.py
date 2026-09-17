"""Outreach / email engine for Agent Jo.

Sends email **as you, from your own SMTP account**, with guardrails so it stays a
personal/business outreach assistant rather than a mass-mailer:

  • Off until you configure SMTP and explicitly enable sending.
  • Rate-limited per hour and per day (AGENT_EMAIL_RATE_HOUR / _DAY).
  • Every send is written to an audit log (email_log.jsonl).
  • A dry-run mode renders and logs without sending.
  • The From address is always your configured account — no spoofing.
  • Bulk campaigns are drafted for review; firing them is an explicit step.

Credentials are encrypted at rest with the same key as your engine secrets.
Stdlib only (smtplib / email); the network send is behind a swappable transport
so it can be tested without touching a real server.
"""
from __future__ import annotations

import json
import re
import smtplib
import ssl
import threading
import time
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from . import config

try:
    from . import crypto as _crypto
    _HAVE_CRYPTO = True
except Exception:                       # pragma: no cover
    _crypto = None
    _HAVE_CRYPTO = False

import os as _os
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOCK = threading.Lock()
_DRAFT_ONLY = threading.local()


def set_draft_only(flag: bool) -> None:
    """Scope a 'rendered but never delivered' window to the current thread. Used by
    observe-mode watchers so an agent run can draft but cannot actually send."""
    _DRAFT_ONLY.on = bool(flag)


def _is_draft_only() -> bool:
    return bool(getattr(_DRAFT_ONLY, "on", False))
_SENT_TIMES: deque = deque()            # monotonic timestamps of real sends


def _env_int(name: str, default: int) -> int:
    try:
        return int(_os.environ.get(name, default))
    except Exception:
        return default


RATE_HOUR = _env_int("AGENT_EMAIL_RATE_HOUR", 30)
RATE_DAY = _env_int("AGENT_EMAIL_RATE_DAY", 200)


def _clean_addr_list(v) -> list:
    if isinstance(v, str):
        v = re.split(r"[,\n;]+", v)
    out = []
    for a in (v or []):
        a = (a or "").strip().lower()
        if _EMAIL_RE.match(a) and a not in out:
            out.append(a)
    return out


def _clean_domain_list(v) -> list:
    if isinstance(v, str):
        v = re.split(r"[,\n;]+", v)
    out = []
    for d in (v or []):
        d = (d or "").strip().lower().lstrip("@")
        if d and "." in d and d not in out:
            out.append(d)
    return out


def _recipient_allowed(to: str, cfg: dict) -> bool:
    """For autonomous sends: only addresses the user pre-approved are permitted."""
    if not cfg.get("require_allowlist", True):
        return True
    to = (to or "").strip().lower()
    if to in _clean_addr_list(cfg.get("allowed_recipients")):
        return True
    dom = to.split("@")[-1] if "@" in to else ""
    return dom in _clean_domain_list(cfg.get("allowed_domains"))


def _cfg_path():
    return config.AGENT_HOME / "email.json"


def _log_path():
    return config.AGENT_HOME / "email_log.jsonl"


# --------------------------------------------------------------------------- #
#  configuration (password encrypted at rest)
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    try:
        cfg = json.loads(_cfg_path().read_text("utf-8"))
        if not isinstance(cfg, dict):
            return {}
    except Exception:
        return {}
    pw = cfg.get("password", "")
    if pw and _HAVE_CRYPTO and _crypto.is_encrypted(pw):
        try:
            cfg["password"] = _crypto.decrypt_str(pw)
        except Exception:
            cfg["password"] = ""
    return cfg


def save_config(data: dict) -> dict:
    cfg = load_config()
    for k in ("host", "port", "username", "from_addr", "from_name",
              "use_tls", "enabled", "footer",
              "autonomous_enabled", "require_allowlist", "max_per_run",
              "allowed_recipients", "allowed_domains"):
        if k in data:
            cfg[k] = data[k]
    if data.get("password"):            # only overwrite when a new one is given
        cfg["password"] = data["password"]
    try:
        cfg["port"] = int(cfg.get("port") or 587)
    except Exception:
        cfg["port"] = 587
    cfg["use_tls"] = bool(cfg.get("use_tls", True))
    cfg["enabled"] = bool(cfg.get("enabled", False))
    # autonomy policy (safe defaults: disarmed, allowlist required, small cap)
    cfg["autonomous_enabled"] = bool(cfg.get("autonomous_enabled", False))
    cfg["require_allowlist"] = bool(cfg.get("require_allowlist", True))
    try:
        cfg["max_per_run"] = max(1, min(500, int(cfg.get("max_per_run") or 25)))
    except Exception:
        cfg["max_per_run"] = 25
    cfg["allowed_recipients"] = _clean_addr_list(cfg.get("allowed_recipients"))
    cfg["allowed_domains"] = _clean_domain_list(cfg.get("allowed_domains"))
    to_disk = dict(cfg)
    if to_disk.get("password") and _HAVE_CRYPTO:
        to_disk["password"] = _crypto.encrypt_str(to_disk["password"])
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(to_disk, indent=2), "utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass
    return status()


def is_configured() -> bool:
    c = load_config()
    return bool(c.get("host") and c.get("from_addr"))


def status() -> dict:
    """Safe view of the config — never includes the password."""
    c = load_config()
    return {
        "configured": is_configured(),
        "enabled": bool(c.get("enabled", False)),
        "host": c.get("host", ""),
        "port": c.get("port", 587),
        "username": c.get("username", ""),
        "from_addr": c.get("from_addr", ""),
        "from_name": c.get("from_name", ""),
        "use_tls": bool(c.get("use_tls", True)),
        "has_password": bool(c.get("password")),
        "footer": c.get("footer", ""),
        "autonomous_enabled": bool(c.get("autonomous_enabled", False)),
        "require_allowlist": bool(c.get("require_allowlist", True)),
        "max_per_run": int(c.get("max_per_run") or 25),
        "allowed_recipients": _clean_addr_list(c.get("allowed_recipients")),
        "allowed_domains": _clean_domain_list(c.get("allowed_domains")),
        "rate_hour": RATE_HOUR, "rate_day": RATE_DAY,
    }


# --------------------------------------------------------------------------- #
#  rate limiting + audit log
# --------------------------------------------------------------------------- #
def _rate_check() -> tuple[bool, str]:
    now = time.monotonic()
    with _LOCK:
        while _SENT_TIMES and now - _SENT_TIMES[0] > 86400:
            _SENT_TIMES.popleft()
        last_hour = sum(1 for t in _SENT_TIMES if now - t <= 3600)
        last_day = len(_SENT_TIMES)
        if last_hour >= RATE_HOUR:
            return False, f"hourly send limit reached ({RATE_HOUR}/h)"
        if last_day >= RATE_DAY:
            return False, f"daily send limit reached ({RATE_DAY}/day)"
    return True, ""


def _rate_record() -> None:
    with _LOCK:
        _SENT_TIMES.append(time.monotonic())


def _audit(entry: dict) -> None:
    try:
        entry = dict(entry)
        entry["ts"] = time.time()
        with _LOCK, open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def recent_log(n: int = 50) -> list:
    try:
        lines = _log_path().read_text("utf-8").splitlines()[-max(1, n):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return list(reversed(out))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
#  transport (swappable for tests / alternative providers)
# --------------------------------------------------------------------------- #
def _smtp_transport(cfg: dict, to_addrs: list, msg: EmailMessage) -> None:
    host, port = cfg["host"], int(cfg.get("port") or 587)
    user, pw = cfg.get("username") or cfg["from_addr"], cfg.get("password") or ""
    if cfg.get("use_tls", True) and port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            if pw:
                s.login(user, pw)
            s.send_message(msg, to_addrs=to_addrs)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if cfg.get("use_tls", True):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if pw:
                s.login(user, pw)
            s.send_message(msg, to_addrs=to_addrs)


TRANSPORT = _smtp_transport             # tests override this


# --------------------------------------------------------------------------- #
#  templating
# --------------------------------------------------------------------------- #
class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def personalize(template: str, contact: dict) -> str:
    """Fill {name}/{company}/... placeholders from a contact dict. Missing fields
    become empty strings; never executes code."""
    try:
        return (template or "").format_map(_SafeDict(contact or {}))
    except Exception:
        return template or ""


def build_campaign(subject_tmpl: str, body_tmpl: str, contacts: list) -> list:
    """Render personalised drafts for a list of contacts (no sending)."""
    drafts = []
    for c in contacts or []:
        to = (c.get("email") or c.get("to") or "").strip()
        drafts.append({
            "to": to,
            "valid": bool(_EMAIL_RE.match(to)),
            "subject": personalize(subject_tmpl, c),
            "body": personalize(body_tmpl, c),
            "contact": c,
        })
    return drafts


# --------------------------------------------------------------------------- #
#  send
# --------------------------------------------------------------------------- #
def send(to: str, subject: str, body: str, *, html: str | None = None,
         dry_run: bool = False, autonomous: bool = False) -> dict:
    """Send a single email through the configured account. Returns a result dict;
    never raises for the common refusals (returns ok=False with a reason).

    autonomous=True is the unattended path: it requires the auto-pilot arm switch
    to be ON and the recipient to be on the approved allowlist, so the agent can
    never autonomously email people you didn't pre-approve."""
    to = (to or "").strip()
    if not _EMAIL_RE.match(to):
        return {"ok": False, "to": to, "error": "invalid recipient address"}
    # observe mode: render + log but never deliver, whatever the caller asked
    draft_only = _is_draft_only()
    if draft_only:
        dry_run = True
    cfg = load_config()
    if not is_configured():
        return {"ok": False, "to": to,
                "error": "email is not configured (set SMTP details first)"}
    if autonomous:
        if not cfg.get("autonomous_enabled"):
            return {"ok": False, "to": to,
                    "error": "auto-pilot is disarmed — arm it in the Outreach panel"}
        if not _recipient_allowed(to, cfg):
            _audit({"to": to, "subject": subject, "status": "blocked_not_allowed", "auto": True})
            return {"ok": False, "to": to,
                    "error": "recipient is not on the approved allowlist"}
    elif not dry_run and not cfg.get("enabled"):
        return {"ok": False, "to": to,
                "error": "email sending is disabled — enable it in the Outreach panel"}

    footer = cfg.get("footer") or ""
    full_body = body if not footer else f"{body}\n\n{footer}"

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "", cfg["from_addr"]))
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(full_body)
    if html:
        msg.add_alternative(html, subtype="html")

    if dry_run:
        _audit({"to": to, "subject": msg["Subject"],
                "status": "draft_only" if draft_only else "dry_run", "auto": autonomous})
        return {"ok": True, "to": to, "subject": msg["Subject"], "dry_run": True,
                "draft_only": draft_only}

    ok, why = _rate_check()
    if not ok:
        _audit({"to": to, "subject": msg["Subject"], "status": "rate_limited", "auto": autonomous})
        return {"ok": False, "to": to, "error": why}

    try:
        TRANSPORT(cfg, [parseaddr(to)[1]], msg)
    except Exception as exc:
        _audit({"to": to, "subject": msg["Subject"], "auto": autonomous,
                "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "to": to, "error": f"send failed: {type(exc).__name__}: {exc}"}

    _rate_record()
    _audit({"to": to, "subject": msg["Subject"], "status": "sent", "auto": autonomous})
    return {"ok": True, "to": to, "subject": msg["Subject"], "dry_run": False}


def send_campaign(drafts: list, *, dry_run: bool = False, limit: int | None = None) -> dict:
    """Send a list of {to, subject, body} drafts. Stops at the rate limit and
    reports per-recipient results. Bulk sends should always be user-approved."""
    sent, failed, results = 0, 0, []
    for i, d in enumerate(drafts or []):
        if limit is not None and i >= limit:
            break
        r = send(d.get("to", ""), d.get("subject", ""), d.get("body", ""),
                 html=d.get("html"), dry_run=dry_run)
        results.append(r)
        if r.get("ok"):
            sent += 1
        else:
            failed += 1
            if "limit reached" in (r.get("error") or ""):
                break               # stop early once throttled
    return {"sent": sent, "failed": failed, "total": len(drafts or []),
            "dry_run": dry_run, "results": results}


# --------------------------------------------------------------------------- #
#  auto-pilot: unattended, end-to-end, inside the policy fence
# --------------------------------------------------------------------------- #
def pause_autonomous() -> dict:
    """Kill switch: immediately disarm all autonomous sending."""
    save_config({"autonomous_enabled": False})
    _audit({"to": "", "subject": "auto-pilot paused", "status": "paused", "auto": True})
    return status()


def run_autopilot(subject_tmpl: str, body_tmpl: str, contacts: list,
                  *, dry_run: bool = False) -> dict:
    """Execute an outreach job with no per-send approval. Enforces: armed switch,
    per-recipient allowlist, per-run cap, and the hourly/daily rate limits. Returns
    a full report. Designed to be safe to call unattended (e.g. from a schedule)."""
    cfg = load_config()
    if not is_configured():
        return {"ok": False, "error": "email is not configured", "sent": 0,
                "skipped": 0, "blocked": 0, "results": []}
    if not dry_run and not cfg.get("autonomous_enabled"):
        return {"ok": False, "error": "auto-pilot is disarmed", "sent": 0,
                "skipped": 0, "blocked": 0, "results": []}

    cap = int(cfg.get("max_per_run") or 25)
    drafts = build_campaign(subject_tmpl, body_tmpl, contacts)
    sent = failed = blocked = skipped = 0
    results = []
    for d in drafts:
        if not d["valid"]:
            skipped += 1
            continue
        if not _recipient_allowed(d["to"], cfg):
            blocked += 1
            results.append({"ok": False, "to": d["to"], "error": "not on allowlist"})
            continue
        if sent >= cap:
            results.append({"ok": False, "to": d["to"],
                            "error": f"per-run cap reached ({cap})"})
            skipped += 1
            continue
        r = send(d["to"], d["subject"], d["body"], dry_run=dry_run, autonomous=True)
        results.append(r)
        if r.get("ok"):
            sent += 1
        else:
            failed += 1
            if "limit reached" in (r.get("error") or ""):
                break               # throttled — stop the run cleanly
    return {"ok": True, "dry_run": dry_run, "sent": sent, "failed": failed,
            "blocked": blocked, "skipped": skipped, "cap": cap,
            "total": len(drafts), "results": results}
