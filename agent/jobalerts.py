"""Job alerts by email — the way in that boards actually support.

PNet, Careers24, CareerJunction, LinkedIn, Indeed and most others block
automated readers, and they are entitled to. But every one of them will
happily **email you** the same listings if you save a search as an alert.
That is the sanctioned route, and it is better than scraping in every way
that matters:

  It does not break. No headers to tune, no browser to keep ahead of a
  detection vendor, nothing that works today and fails silently in a month.

  It is what the board wants. You asked for these; they sent them.

  It is filtered at the source. The board's own matching is usually better
  than a keyword pass over a listing page, because it sees the fields the
  page only renders.

  It reaches the pages a reader can't. An alert email links directly to the
  advert, including ones behind a search interface.

So: parse the alert emails into roles. The rest of the pipeline — screening,
scoring, drafting, the fabrication check — is unchanged and does not care
where a role came from.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone

# Boards whose alerts we recognise, and how their mail is shaped. Anything
# unrecognised still goes through the generic link extractor below.
KNOWN_SENDERS = {
    "pnet.co.za": "PNet",
    "careers24.com": "Careers24",
    "careerjunction.co.za": "CareerJunction",
    "linkedin.com": "LinkedIn",
    "indeed.com": "Indeed",
    "glassdoor.com": "Glassdoor",
    "offerzen.com": "OfferZen",
    "remotive.com": "Remotive",
    "weworkremotely.com": "We Work Remotely",
    "wellfound.com": "Wellfound",
    "himalayas.app": "Himalayas",
    "otta.com": "Otta",
    "jobmail.co.za": "JobMail",
    "bizcommunity.com": "Bizcommunity",
}

# Links in a job alert that are never a job.
_NOT_A_JOB = re.compile(
    r"unsubscribe|preferences|privacy|terms|manage.?alert|update.?profile|"
    r"view.?in.?browser|app.?store|play\.google|facebook|twitter|linkedin\.com/"
    r"(company|school)|instagram|youtube|help|support|contact|login|signin|"
    r"settings|feedback|survey|blog|/cdn-cgi/|mailto:", re.I)

# Link text that is navigation rather than a role.
# Navigation is often a PHRASE — "View all jobs", "See more results" — so
# matching whole single words let those through as roles.
_NAV_TEXT = re.compile(
    r"^\s*(?:"
    r"(?:view|see|show|browse|search|find|explore|discover)\s+"
    r"(?:all|more|other|similar|these|your|new)?\s*"
    r"(?:jobs?|roles?|results?|matches|vacancies|opportunities)?"
    r"|more|all|apply|click|here|read more|next|previous|home|jobs?"
    r"|unsubscribe|manage|update|log ?in|sign ?in|my account"
    r")\s*$", re.I)

_A_TAG = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                    re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


def sender_board(address: str) -> str:
    """Which board sent this, if we know it."""
    a = (address or "").lower()
    for domain, name in KNOWN_SENDERS.items():
        if domain in a:
            return name
    m = re.search(r"@([\w.-]+)", a)
    return (m.group(1) if m else "").replace("www.", "") or "email alert"


def _text(fragment: str) -> str:
    return " ".join(_html.unescape(_TAGS.sub(" ", fragment or "")).split())


def _looks_like_a_role(title: str) -> bool:
    if not title or len(title) < 6 or len(title) > 140:
        return False
    if _NAV_TEXT.match(title):
        return False
    # a role has words; a tracking link is a blob
    return len(title.split()) >= 2


def _company_near(chunk: str, title: str) -> str:
    """Alerts usually put the employer next to the title — after a dash, or
    on the following line. Neither is guaranteed, so this stays a guess and
    an empty company is fine."""
    after = chunk.split(title, 1)[-1][:160]
    t = _text(after)
    # stop at the comma: "Standard Bank, Johannesburg" is an employer and a
    # city, and running past it swallowed the next role's title
    m = re.match(r"^\s*(?:[-–—@|·]|at\s)\s*([A-Z][\w&.' -]{2,45}?)"
                 r"(?=[,|·]|\s{2,}|$)", t)
    if m:
        return m.group(1).strip(" ,.-")
    m2 = re.match(r"^([A-Z][\w&.' -]{2,45}?)(?=[,|·]|\s{2,}|$)", t)
    return m2.group(1).strip(" ,.-") if m2 else ""


def parse_alert(message: dict) -> dict:
    """Turn one job-alert email into roles.

    Deliberately forgiving: alert formats change constantly, and a parser
    that only handles today's markup would be another thing that fails
    quietly. Anything link-shaped with role-shaped text is a candidate; the
    rest of the pipeline screens them anyway."""
    body = str(message.get("html") or message.get("body") or "")
    frm = str(message.get("from") or "")
    board = sender_board(frm)
    subject = str(message.get("subject") or "")

    found, seen = [], set()
    for m in _A_TAG.finditer(body):
        href, inner = m.group(1).strip(), m.group(2)
        if not href.lower().startswith("http") or _NOT_A_JOB.search(href):
            continue
        title = _text(inner)
        if not _looks_like_a_role(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        chunk = body[m.start():m.start() + 500]
        found.append({
            "title": title,
            "company": _company_near(chunk, title),
            "url": href,
            "source": f"{board} alert",
            "summary": _text(chunk)[:600],
        })

    if not found:
        # some alerts are plain text: one role per line, often "Title - Company"
        for line in _text(body).split("  "):
            line = line.strip()
            if _looks_like_a_role(line) and (" - " in line or " at " in line):
                title, _, rest = re.split(r"\s+-\s+|\s+at\s+", line, 1)[0], \
                    None, ""
                if _looks_like_a_role(title):
                    found.append({"title": title.strip(), "company": "",
                                  "url": "", "source": f"{board} alert",
                                  "summary": line[:300]})
    return {"ok": True, "board": board, "subject": subject,
            "roles": found[:60], "count": len(found),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def ingest(messages: list) -> dict:
    """Read a batch of alert emails and track what they contain."""
    from . import jobscout
    all_roles, per_board, skipped = [], {}, 0
    for msg in messages or []:
        try:
            r = parse_alert(msg)
        except Exception:
            skipped += 1
            continue
        if not r["roles"]:
            skipped += 1
            continue
        per_board[r["board"]] = per_board.get(r["board"], 0) + len(r["roles"])
        all_roles.extend(r["roles"])
    if not all_roles:
        return {"ok": False,
                "error": ("No job links found in those messages. If they're "
                          "alert emails, forward one and I'll look at what "
                          "shape it is.")}
    res = jobscout.add_roles(all_roles)
    try:
        from . import audit
        audit.record("jobscout", name="alert-ingest",
                     summary=f"{res.get('added', 0)} new from "
                             f"{', '.join(per_board) or 'email'}")
    except Exception:
        pass
    return {"ok": True, "added": res.get("added", 0),
            "seen": len(all_roles), "by_board": per_board,
            "messages_without_jobs": skipped,
            "note": ("These came from alerts the boards sent you, which is "
                     "why it works where reading their pages doesn't.")}


def setup_guide() -> list:
    """What to switch on, per board. Five minutes, once."""
    return [
        {"board": "PNet", "url": "https://www.pnet.co.za/",
         "how": "Run your search, then 'Create job alert' — choose daily."},
        {"board": "Careers24", "url": "https://www.careers24.com/",
         "how": "Search, then 'Get job alerts' under the results."},
        {"board": "CareerJunction", "url": "https://www.careerjunction.co.za/",
         "how": "Save your search as a 'Job Alert' in My CareerJunction."},
        {"board": "LinkedIn", "url": "https://www.linkedin.com/jobs/",
         "how": "Search, set filters, then the 'Job alert' toggle. Daily."},
        {"board": "Indeed", "url": "https://za.indeed.com/",
         "how": "Search, then 'Get new jobs for this search by email'."},
        {"board": "OfferZen", "url": "https://www.offerzen.com/",
         "how": "Interview requests and matches arrive by email already."},
    ]


# --------------------------------------------------------------------------- #
#  Getting the emails in
#
#  A parser with nothing feeding it is not a feature. Three ways in, in order
#  of how little they ask of you:
#
#    Paste one. Works this minute, no setup, no credentials.
#    Drop .eml files in a folder. Outlook and Thunderbird both save that way,
#      and a mail rule can do it automatically.
#    Forward to the inbox server, if you've connected one.
# --------------------------------------------------------------------------- #
def from_raw(raw: str, sender: str = "") -> dict:
    """Parse a pasted email — headers and all, or just the body.

    Accepts whatever lands: a full message with headers, an HTML fragment
    copied out of a mail client, or plain text. Being fussy about the format
    would defeat the point of a paste box."""
    text = raw or ""
    frm, subject = sender, ""
    # a pasted message usually still carries its headers
    m = re.search(r"^From:\s*(.+)$", text, re.M | re.I)
    if m:
        frm = frm or m.group(1).strip()
    m2 = re.search(r"^Subject:\s*(.+)$", text, re.M | re.I)
    if m2:
        subject = m2.group(1).strip()
    looks_html = "<a" in text.lower() or "<html" in text.lower()
    return parse_alert({"from": frm, "subject": subject,
                        "html": text if looks_html else "",
                        "body": "" if looks_html else text})


def from_eml(path) -> dict:
    """Read a saved .eml file, including its HTML part."""
    import email
    from email import policy
    from pathlib import Path
    p = Path(path)
    with open(p, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    html = plain = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and not html:
                html = part.get_content()
            elif ctype == "text/plain" and not plain:
                plain = part.get_content()
    else:
        body = msg.get_content()
        if "<a" in str(body).lower():
            html = body
        else:
            plain = body
    return parse_alert({"from": msg.get("From", ""),
                        "subject": msg.get("Subject", ""),
                        "html": html, "body": plain})


def alerts_dir():
    from . import config
    d = config.AGENT_HOME / "jobscout" / "alerts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ingest_folder(move_done: bool = True) -> dict:
    """Read every .eml dropped in the alerts folder.

    Processed files are moved aside rather than deleted — if the parse was
    wrong you still have the original to show me."""
    from . import jobscout
    d = alerts_dir()
    done = d / "processed"
    files = sorted([f for f in d.glob("*.eml") if f.is_file()])
    if not files:
        return {"ok": False,
                "error": (f"No .eml files in {d}. Save an alert email there "
                          f"(File \u2192 Save As in Outlook), or paste one "
                          f"instead.")}
    all_roles, per_board, empty = [], {}, []
    for f in files:
        try:
            r = from_eml(f)
        except Exception as exc:
            empty.append(f"{f.name}: {type(exc).__name__}")
            continue
        if r["roles"]:
            per_board[r["board"]] = per_board.get(r["board"], 0) \
                + len(r["roles"])
            all_roles.extend(r["roles"])
        else:
            empty.append(f"{f.name}: no job links found")
        if move_done:
            try:
                done.mkdir(parents=True, exist_ok=True)
                f.rename(done / f.name)
            except Exception:
                pass
    if not all_roles:
        return {"ok": False,
                "error": f"Read {len(files)} file(s) but found no job links. "
                         f"{'; '.join(empty[:3])}"}
    res = jobscout.add_roles(all_roles)
    return {"ok": True, "files": len(files), "added": res.get("added", 0),
            "seen": len(all_roles), "by_board": per_board,
            "skipped": empty,
            "folder": str(d)}
