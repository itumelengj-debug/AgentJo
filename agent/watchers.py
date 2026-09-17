"""Watchers — scheduled monitoring of a data stream with change detection.

A watcher fetches a source (a web page or a search query) on a cadence, compares
it to the last snapshot, and reports *what changed*. The scheduler runs it; on a
real change it hands the change to the agent, which decides what to do (draft or,
within the auto-pilot fence, send outreach). Read-only by itself — it never sends
anything; only the agent's `send_email` path can, and that still obeys the arm
switch + allowlist + caps.

The network fetch is behind a swappable FETCHER so this is testable offline.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from . import config

_LOCK = threading.Lock()
_MAX_SNAPSHOT = 20000           # chars of source text we keep for diffing
_DIFF_LINES = 12                # max new lines to surface to the agent


def _state_path():
    return config.AGENT_HOME / "watch_state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text("utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            p.write_text(json.dumps(state), "utf-8")
    except Exception:
        pass


def _log_path():
    return config.AGENT_HOME / "watchers_log.jsonl"


def record_run(name: str, changed: bool, summary: str) -> None:
    """Append a watcher fire to the run log (for the autonomy feed)."""
    try:
        with _LOCK, open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "name": name,
                                 "changed": bool(changed),
                                 "summary": (summary or "")[:200]}) + "\n")
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


def _normalize(text: str) -> str:
    # collapse whitespace so trivial reflowing doesn't read as a change
    return re.sub(r"\s+", " ", (text or "")).strip()


def signature(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- #
#  fetching (swappable for tests / alternative sources)
# --------------------------------------------------------------------------- #
def _real_fetch(source_type: str, source: str) -> tuple[bool, str, str]:
    """Return (ok, text, error). Uses Agent Jo's web layer."""
    try:
        from . import web
    except Exception as exc:
        return False, "", f"web module unavailable: {exc}"
    try:
        if source_type == "search":
            results = web.search(source, None)
            if not results:
                return True, "", ""        # empty result set is still a valid state
            lines = [f"{r.get('title','')} — {r.get('url','')}" for r in results]
            return True, "\n".join(lines), ""
        # default: fetch a page and extract readable text
        html = web.fetch(source)
        return True, web._extract_text(html), ""
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"


FETCHER = _real_fetch           # tests override this


def _whats_new(old_text: str, new_text: str) -> str:
    """A short, human-readable description of what appeared since last time."""
    old_lines = set(l.strip() for l in (old_text or "").splitlines() if l.strip())
    new_lines = [l.strip() for l in (new_text or "").splitlines() if l.strip()]
    added = [l for l in new_lines if l not in old_lines]
    if not added:
        return "Content changed (no clearly new lines; wording or order differs)."
    shown = added[:_DIFF_LINES]
    more = len(added) - len(shown)
    out = "New since last check:\n" + "\n".join(f"  • {l}" for l in shown)
    if more > 0:
        out += f"\n  • …and {more} more"
    return out


def check(watcher: dict, *, persist: bool = True) -> dict:
    """Fetch the source and compare to the stored snapshot.

    Returns {ok, changed, summary, new_text, signature, first_run, error}. Persists
    the new snapshot/signature only on a successful fetch (so the next run compares
    against this one) — pass persist=False to preview without consuming the change."""
    wid = str(watcher.get("id") or watcher.get("name") or "w")
    src_type = watcher.get("source_type", "url")
    source = watcher.get("source", "")
    if not source:
        return {"ok": False, "changed": False, "error": "no source configured"}

    ok, text, err = FETCHER(src_type, source)
    if not ok:
        return {"ok": False, "changed": False, "error": err}

    text = (text or "")[:_MAX_SNAPSHOT]
    sig = signature(text)
    state = load_state()
    prev = state.get(wid) or {}
    first_run = "sig" not in prev
    changed = (not first_run) and prev.get("sig") != sig

    if persist:
        state[wid] = {"sig": sig, "text": text, "ts": time.time()}
        save_state(state)

    if first_run:
        summary = "First check — baseline captured." if persist else "No baseline yet — first real run will capture one."
    elif changed:
        summary = _whats_new(prev.get("text", ""), text)
    else:
        summary = "No change since last check."
    return {"ok": True, "changed": changed, "first_run": first_run,
            "summary": summary, "new_text": text, "signature": sig, "error": ""}


def build_agent_prompt(watcher: dict, change_summary: str, new_text: str) -> str:
    """Compose the instruction handed to the agent when a change is detected."""
    src = watcher.get("source", "")
    instr = (watcher.get("instruction") or
             "Summarise what changed and, if it's relevant to my contacts, draft "
             "outreach about it. Do not send unless auto-pilot is armed and the "
             "recipient is approved.")
    excerpt = (new_text or "")[:4000]
    return (f"A monitored source changed. Source: {src}\n\n"
            f"{change_summary}\n\n"
            f"--- current content (excerpt) ---\n{excerpt}\n\n"
            f"--- your task ---\n{instr}")


# =========================================================================== #
#  Structured extraction — items, not "the page changed"
#
#  Text-diffing a listing page tells you something moved; it can't tell you
#  THREE NEW TENDERS. Structured mode pulls items with fields, keys each one,
#  and reports only the genuinely new ones. And because selectors rot — the
#  single reason a scraper has to be rebuilt every few months — a watcher that
#  suddenly extracts nothing tries to re-derive its own selectors from the
#  page before giving up, and says so plainly when it can't.
#
#  The extractor is stdlib-only (html.parser), like the rest of this app's
#  infrastructure: no bs4, no lxml, nothing to install or keep in step.
# =========================================================================== #
from html.parser import HTMLParser as _HTMLParser

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_parts")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.text_parts = []

    @property
    def classes(self):
        return set((self.attrs.get("class") or "").split())

    def text(self):
        out = list(self.text_parts)
        for c in self.children:
            out.append(c.text())
        return re.sub(r"\s+", " ", " ".join(p for p in out if p)).strip()

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class _DOM(_HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if data and data.strip():
            self._stack[-1].text_parts.append(data.strip())


def parse_html(html: str):
    d = _DOM()
    try:
        d.feed(html or "")
    except Exception:
        pass
    return d.root


_SEL_PART = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*)?"
    r"(?P<id>#[\w-]+)?"
    r"(?P<classes>(?:\.[\w-]+)*)"
    r"(?P<attr>\[[^\]]+\])?$")


def _matches(node, part: str) -> bool:
    m = _SEL_PART.match(part)
    if not m:
        return False
    if m.group("tag") and node.tag != m.group("tag").lower():
        return False
    if m.group("id") and node.attrs.get("id") != m.group("id")[1:]:
        return False
    want = {c for c in (m.group("classes") or "").split(".") if c}
    if want and not want <= node.classes:
        return False
    a = m.group("attr")
    if a:
        body = a[1:-1]
        if "=" in body:
            k, v = body.split("=", 1)
            if node.attrs.get(k.strip()) != v.strip().strip("\"'"):
                return False
        elif body.strip() not in node.attrs:
            return False
    return True


def select(root, selector: str) -> list:
    """A useful subset of CSS: tag, .class, #id, [attr], [attr=value], and
    descendant combinators. Enough for real listing pages; deliberately not a
    full CSS engine, because the failure mode of a half-right one is worse."""
    parts = [p for p in (selector or "").strip().split() if p]
    if not parts:
        return []
    current = [root]
    for part in parts:
        nxt = []
        for base in current:
            for node in base.walk():
                if node is not base and _matches(node, part):
                    nxt.append(node)
        current = nxt
        if not current:
            return []
    return current


def _field_value(node, spec) -> str:
    if isinstance(spec, str):
        spec = {"selector": spec}
    sel = (spec or {}).get("selector") or ""
    attr = (spec or {}).get("attr")
    target = node
    if sel:
        hits = select(node, sel)
        if not hits:
            return ""
        target = hits[0]
    if attr:
        return (target.attrs.get(attr) or "").strip()
    return target.text()


def extract_items(html: str, item_selector: str, fields: dict) -> list:
    root = parse_html(html)
    out = []
    for node in select(root, item_selector):
        item = {k: _field_value(node, spec) for k, spec in (fields or {}).items()}
        if any(v for v in item.values()):
            out.append(item)
    return out


def item_key(item: dict) -> str:
    basis = (item.get("url") or item.get("link") or "") + "|" + \
            (item.get("title") or item.get("name") or "")
    if not basis.strip("|"):
        basis = json.dumps(item, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
#  self-healing selectors
# --------------------------------------------------------------------------- #
_HEAL_SYSTEM = (
    "A web scraper's CSS selectors stopped matching because the page changed. "
    "You are given a SAMPLE of the page's HTML structure and the FIELDS the "
    "scraper needs. Propose new selectors. Reply with ONLY raw JSON: "
    "{\"item_selector\": str, \"fields\": {name: {\"selector\": str, "
    "\"attr\": str or null}}}. item_selector must match ONE repeating "
    "listing element (a card/row), and each field selector is relative to it. "
    "Use class names actually present in the sample. No prose, no fences.")


def _structure_sample(html: str, limit: int = 6000) -> str:
    """A compact skeleton of the page: tags with their classes, no text.
    Keeps the prompt small and stops page copy leaking into the model."""
    root = parse_html(html)
    lines, seen = [], set()
    for node in root.walk():
        if node.tag == "#root":
            continue
        cls = ".".join(sorted(node.classes)[:4])
        sig = f"{node.tag}{'.' + cls if cls else ''}"
        if sig in seen:
            continue
        seen.add(sig)
        lines.append(sig)
        if sum(len(x) + 1 for x in lines) > limit:
            break
    return "\n".join(lines)


def heal(watcher: dict, html: str, brain, model=None) -> dict:
    """Re-derive selectors from the live page, and only accept them if they
    actually extract something. A healer that trusts the model without
    verifying would quietly replace working selectors with worse ones."""
    fields = watcher.get("fields") or {}
    payload = json.dumps({"FIELDS": list(fields.keys()) or ["title", "url"],
                          "STRUCTURE": _structure_sample(html)})[:12000]
    try:
        kw = {"model": model} if model else {}
        resp = brain.chat([{"role": "user", "content": payload}],
                          [_HEAL_SYSTEM], None, **kw)
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        start = text.find("{")
        data = json.loads(text[start:] if start >= 0 else text)
    except Exception as exc:
        return {"ok": False, "error": f"healer failed: {type(exc).__name__}: "
                                      f"{exc}"}
    new_item = (data.get("item_selector") or "").strip()
    new_fields = data.get("fields") or {}
    if not new_item:
        return {"ok": False, "error": "healer returned no item selector"}
    test = extract_items(html, new_item, new_fields)
    if not test:
        return {"ok": False,
                "error": "healer's selectors still extract nothing — the page "
                         "may need a human look"}
    return {"ok": True, "item_selector": new_item, "fields": new_fields,
            "sample_count": len(test)}


def check_structured(watcher: dict, brain=None, *, persist: bool = True,
                     model=None) -> dict:
    """Fetch, extract items, and report only the NEW ones.

    On a total extraction failure (page fetched fine, zero items) the watcher
    tries to heal itself once, then re-extracts. Whether it healed or gave up,
    it says so — a silently-empty scraper that reports 'no changes' every day
    is the worst outcome of all, and it's the one this exists to prevent."""
    wid = str(watcher.get("id") or watcher.get("name") or "w")
    source = watcher.get("source", "")
    if not source:
        return {"ok": False, "error": "no source configured"}
    item_sel = watcher.get("item_selector") or ""
    if not item_sel:
        return {"ok": False, "error": "no item_selector — this watcher is in "
                                      "text mode; use check() instead"}

    ok, html, err = FETCHER(watcher.get("source_type", "url"), source)
    if not ok:
        return {"ok": False, "error": err, "fetched": False}

    fields = watcher.get("fields") or {}
    items = extract_items(html, item_sel, fields)
    healed = None
    if not items and brain is not None:
        h = heal(watcher, html, brain, model=model)
        if h.get("ok"):
            item_sel = h["item_selector"]
            fields = h["fields"]
            items = extract_items(html, item_sel, fields)
            healed = {"item_selector": item_sel, "fields": fields,
                      "found": len(items)}
        else:
            return {"ok": False, "fetched": True, "items": [], "new": [],
                    "error": ("extracted nothing and could not self-heal: "
                              + h.get("error", "")),
                    "needs_attention": True}
    if not items:
        return {"ok": False, "fetched": True, "items": [], "new": [],
                "error": "extracted nothing (selectors no longer match)",
                "needs_attention": True}

    state = load_state()
    prev = state.get(wid) or {}
    seen = set(prev.get("keys") or [])
    first_run = "keys" not in prev
    new = [i for i in items if item_key(i) not in seen]

    if persist:
        entry = {"keys": [item_key(i) for i in items][-500:],
                 "ts": time.time(), "count": len(items)}
        if healed:
            entry["healed_at"] = time.time()
            entry["item_selector"] = healed["item_selector"]
            entry["fields"] = healed["fields"]
        state[wid] = {**prev, **entry}
        save_state(state)

    if first_run:
        summary = f"First run — {len(items)} item(s) recorded as the baseline."
        new = []
    elif new:
        summary = f"{len(new)} new item(s) of {len(items)} on the page."
    else:
        summary = f"No new items ({len(items)} on the page)."
    if healed:
        summary = ("Selectors had stopped matching; re-derived them from the "
                   f"page and recovered {healed['found']} item(s). " + summary)
    return {"ok": True, "changed": bool(new), "first_run": first_run,
            "items": items, "new": new, "summary": summary,
            "healed": healed, "error": ""}


# --------------------------------------------------------------------------- #
#  Watchers that have stopped working
#
#  A watcher whose host stops resolving fails every hour, for ever. In the real
#  trail one did exactly that for two days and only the circuit breaker
#  noticed — which then suppressed every OTHER watcher on the same schedule.
#  Repeated identical failures are a signal, not noise: count them, and pause
#  the watcher once it's clear the site isn't coming back.
# --------------------------------------------------------------------------- #
STRIKES_BEFORE_PAUSE = 5


def _strikes_path():
    # this module stores state directly under AGENT_HOME; there is no _dir()
    return config.AGENT_HOME / "watch_strikes.json"


def _strikes() -> dict:
    try:
        return json.loads(_strikes_path().read_text("utf-8"))
    except Exception:
        return {}


def _save_strikes(d: dict) -> None:
    try:
        p = _strikes_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), "utf-8")
    except Exception:
        pass


def note_failure(name: str, error: str) -> dict:
    """Record a failed check. Returns {strikes, paused, reason}."""
    d = _strikes()
    e = " ".join(str(error or "").split())[:200]
    cur = d.get(name) or {"count": 0, "last": ""}
    cur["count"] = cur.get("count", 0) + 1 if cur.get("last") == e else 1
    cur["last"] = e
    d[name] = cur
    _save_strikes(d)
    permanent = any(s in e.lower() for s in
                    ("could not resolve", "name or service not known",
                     "nodename nor servname", "404"))
    if cur["count"] >= STRIKES_BEFORE_PAUSE or (permanent
                                                and cur["count"] >= 3):
        return {"strikes": cur["count"], "paused": True,
                "reason": (f"paused after {cur['count']} identical failures: "
                           f"{e[:120]}")}
    return {"strikes": cur["count"], "paused": False, "reason": ""}


def note_success(name: str) -> None:
    d = _strikes()
    if name in d:
        d.pop(name, None)
        _save_strikes(d)


def strikes_for(name: str) -> int:
    return int((_strikes().get(name) or {}).get("count", 0))
