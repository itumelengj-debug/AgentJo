"""Notifications — when Agent Jo needs you, it tells you, wherever you are.

Unattended work had one weak spot: when something needed a person it simply
waited. An email held for your OK sat in the queue until you happened to open
the app; a scheduled task that failed every morning was noticed days later; a
step blocked on a login stayed blocked. The phone companion could approve
things, but only if you thought to open it, and only on the same Wi-Fi.

So now it reaches out. Two channels, both opt-in, both off until you set them:

  ntfy      push to the free ntfy app (ntfy.sh or your own server). No
            account: you pick a hard-to-guess topic name and subscribe to it
            on the phone. One-way.
  Telegram  a bot you make with @BotFather. Two-way: a held call arrives with
            Approve / Refuse buttons, and /status, /pause, /resume work from
            the chat. Only the one chat id you configured is listened to;
            every other sender is ignored and audited.

What it tells you about (each switchable):

  held       something that would leave the machine is waiting for your OK
  failed     a scheduled task ended in an error
  breaker    a feature failed repeatedly and has been switched off for a while
  blocked    a task step needs you (a login, a file, a decision)
  goal       a goal was achieved, given up, or ran out of budget

A notification is itself something leaving the machine, so it carries the
same one-line summary the approval queue shows, never the full inputs, and
never the body of an email. Repeats are folded (the same thing twice within
ten minutes is sent once) and there is an hourly ceiling, so a crash loop
can't bury your phone.

The HTTP layer is a swappable POSTER, so all of this is testable offline.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config

EVENTS = {
    "held": "Something is waiting for your OK",
    "failed": "A scheduled task failed",
    "breaker": "A feature was switched off after repeated failures",
    "blocked": "A task needs you",
    "goal": "Goal progress (achieved, given up, out of budget)",
}
DEDUPE_SECONDS = 600
MAX_PER_HOUR = 30
_LOCK = threading.Lock()
_RECENT: dict = {}          # key -> last sent ts
_SENT: list = []            # timestamps sent in the last hour


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _path() -> Path:
    return Path(config.AGENT_HOME) / "notify.json"


def _defaults() -> dict:
    return {
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "events": {k: True for k in EVENTS},
        "open_url": "",
    }


def _secret_in(v: str) -> str:
    try:
        from . import crypto
        return crypto.encrypt_str(v) if v and not crypto.is_encrypted(v) else v
    except Exception:
        return v


def _secret_out(v: str) -> str:
    try:
        from . import crypto
        return crypto.decrypt_str(v) if v and crypto.is_encrypted(v) else v
    except Exception:
        return v


def settings() -> dict:
    """The stored settings with secrets decrypted (for use, not for display)."""
    s = _defaults()
    try:
        saved = json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    s["ntfy"]["token"] = _secret_out(s["ntfy"].get("token", ""))
    s["telegram"]["token"] = _secret_out(s["telegram"].get("token", ""))
    return s


def public_settings() -> dict:
    """What the Settings panel may see: secrets reduced to 'set or not'."""
    s = settings()
    s["ntfy"]["token"] = bool(s["ntfy"].get("token"))
    s["telegram"]["token"] = bool(s["telegram"].get("token"))
    s["event_labels"] = EVENTS
    s["poller"] = {"running": bool(_POLL["thread"] and _POLL["thread"].is_alive()),
                   "error": _POLL["error"]}
    return s


def save(update: dict) -> dict:
    """Merge an update in. An empty or missing token keeps the stored one, so
    the panel never has to read a secret back to save the form."""
    cur = settings()
    for ch in ("ntfy", "telegram"):
        if isinstance(update.get(ch), dict):
            new = dict(update[ch])
            if not new.get("token") or new.get("token") is True:
                new.pop("token", None)
            cur[ch].update(new)
    if isinstance(update.get("events"), dict):
        cur["events"].update({k: bool(v) for k, v in update["events"].items() if k in EVENTS})
    if "open_url" in update:
        cur["open_url"] = str(update.get("open_url") or "")[:300]
    cur["ntfy"]["topic"] = str(cur["ntfy"].get("topic") or "").strip().strip("/")
    cur["telegram"]["chat_id"] = str(cur["telegram"].get("chat_id") or "").strip()
    stored = json.loads(json.dumps(cur))
    stored["ntfy"]["token"] = _secret_in(stored["ntfy"].get("token", ""))
    stored["telegram"]["token"] = _secret_in(stored["telegram"].get("token", ""))
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, indent=1), "utf-8")
    tmp.replace(p)
    _audit("settings", "notification settings changed")
    return public_settings()


def enabled_channels(s: dict | None = None) -> list[str]:
    s = s or settings()
    out = []
    if s["ntfy"].get("enabled") and s["ntfy"].get("topic"):
        out.append("ntfy")
    if s["telegram"].get("enabled") and s["telegram"].get("token") and s["telegram"].get("chat_id"):
        out.append("telegram")
    return out


# --------------------------------------------------------------------------- #
# The wire — swappable for tests
# --------------------------------------------------------------------------- #
def _real_post(url: str, *, data: bytes, headers: dict, timeout: float = 15) -> tuple[int, str]:
    import urllib.request
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as exc:  # HTTPError carries a code; others don't
        code = getattr(exc, "code", 0) or 0
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
        except Exception:
            body = str(exc)
        return code, body


POSTER = _real_post
SYNC = False                # tests: send on the calling thread


def _tg_call(token: str, method: str, payload: dict, timeout: float = 15) -> dict:
    code, body = POSTER(f"https://api.telegram.org/bot{token}/{method}",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"}, timeout=timeout)
    try:
        out = json.loads(body or "{}")
    except ValueError:
        out = {}
    if code != 200 or not out.get("ok"):
        return {"ok": False, "error": (out.get("description") or body or f"HTTP {code}")[:200]}
    return out


def _send_ntfy(s: dict, title: str, body: str, priority: str, url: str) -> dict:
    n = s["ntfy"]
    headers = {"Title": title.encode("ascii", "replace").decode(),
               "Priority": priority, "Tags": "robot"}
    if url:
        headers["Click"] = url
    if n.get("token"):
        headers["Authorization"] = f"Bearer {n['token']}"
    code, resp = POSTER(f"{n['server'].rstrip('/')}/{n['topic']}",
                        data=body.encode("utf-8"), headers=headers)
    return {"ok": 200 <= code < 300, "error": "" if 200 <= code < 300 else f"HTTP {code}: {resp[:120]}"}


def _send_telegram(s: dict, title: str, body: str, buttons: list | None) -> dict:
    t = s["telegram"]
    payload = {"chat_id": t["chat_id"], "text": f"{title}\n\n{body}"[:4000],
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": label, "callback_data": data} for label, data in buttons]]}
    r = _tg_call(t["token"], "sendMessage", payload)
    return {"ok": r.get("ok", False), "error": r.get("error", "")}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _throttled(key: str, now: float) -> str:
    """'' if it may go, else the reason it's folded."""
    with _LOCK:
        while _SENT and now - _SENT[0] > 3600:
            _SENT.pop(0)
        if key and now - _RECENT.get(key, 0) < DEDUPE_SECONDS:
            return "repeat"
        if len(_SENT) >= MAX_PER_HOUR:
            return "hourly ceiling"
        if key:
            _RECENT[key] = now
        _SENT.append(now)
    return ""


def send(event: str, title: str, body: str = "", *, key: str = "",
         buttons: list | None = None, priority: str = "default",
         wait: bool = False) -> dict:
    """Tell the person. Never raises, never blocks a turn (unless wait=True,
    which tests and the 'Send a test' button use)."""
    try:
        s = settings()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if event != "test" and not s["events"].get(event, False):
        return {"ok": False, "skipped": "event switched off"}
    chans = enabled_channels(s)
    if not chans:
        return {"ok": False, "skipped": "no channel set up"}
    why = _throttled(key or f"{event}:{title}", time.time()) if event != "test" else ""
    if why:
        return {"ok": False, "skipped": why}
    url = s.get("open_url") or ""
    body = (body or "")[:1500]

    def _go() -> dict:
        res = {}
        for ch in chans:
            try:
                if ch == "ntfy":
                    res[ch] = _send_ntfy(s, title, body, priority, url)
                else:
                    res[ch] = _send_telegram(s, title, body, buttons)
            except Exception as exc:
                res[ch] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        bad = {k: v["error"] for k, v in res.items() if not v.get("ok")}
        _audit(event, f"{title[:80]} -> {', '.join(chans)}"
               + (f" (failed: {bad})" if bad else ""))
        return {"ok": not bad, "channels": res}

    if wait or SYNC:
        return _go()
    threading.Thread(target=_go, daemon=True).start()
    return {"ok": True, "queued": chans}


# --- the events, each in one place ---------------------------------------- #
def held(item: dict) -> dict:
    iid = item.get("id", "")
    return send("held", "Agent Jo is waiting for your OK",
                f"{item.get('summary', item.get('tool', ''))}\n"
                f"From: {item.get('source', 'chat')}"
                + (f"\nWhy held: {item['why_held']}" if item.get("why_held") else ""),
                key=f"held:{iid}", priority="high",
                buttons=[("Approve", f"ij:y:{iid}"), ("Refuse", f"ij:n:{iid}")])


def schedule_failed(name: str, summary: str) -> dict:
    return send("failed", f"Scheduled task failed: {name}", summary[:400],
                key=f"failed:{name}")


def breaker_opened(feature: str, detail: str) -> dict:
    return send("breaker", f"Switched off for now: {feature}", detail[:400],
                key=f"breaker:{feature}", priority="high")


def step_blocked(task: dict, step: str, needs: str) -> dict:
    return send("blocked", f"Task needs you: {task.get('title', '')[:80]}",
                f"Step: {step[:200]}\nNeeds: {needs[:400]}",
                key=f"blocked:{task.get('id')}:{step[:40]}")


def goal_event(goal: dict, what: str, detail: str = "") -> dict:
    return send("goal", f"Goal {what}: {goal.get('title', '')[:80]}", detail[:600],
                key=f"goal:{goal.get('id')}:{what}")


# --------------------------------------------------------------------------- #
# Telegram, the other way: buttons and a few commands, from one chat only
# --------------------------------------------------------------------------- #
_POLL = {"thread": None, "stop": None, "error": "", "offset": 0}
HELP = ("/status — what's waiting and what's scheduled\n"
        "/pause — turn every scheduled task off\n"
        "/resume — turn back on what /pause turned off")


def _status_text() -> str:
    from . import phone
    st = phone.state()
    lines = [f"{len(st['held'])} waiting for your OK, "
             f"{st['running']} scheduled task(s) on" + (" (paused)" if st["paused"] else "")]
    for h in st["held"][:5]:
        lines.append(f"• {h['summary'][:120]}")
    bad = [s for s in st["schedules"] if s["last_status"] == "error"][:3]
    for s in bad:
        lines.append(f"✗ {s['name']}: {s['last_summary'][:100]}")
    return "\n".join(lines)


def handle_update(upd: dict, s: dict | None = None) -> dict:
    """Act on one Telegram update. Pure apart from the calls it makes, so a
    test can feed it updates directly. Returns what it did."""
    s = s or settings()
    t = s["telegram"]
    allowed = str(t.get("chat_id") or "")
    cq = upd.get("callback_query")
    msg = upd.get("message") or (cq or {}).get("message") or {}
    chat = str(((msg.get("chat") or {}).get("id")) or "")
    sender = str(((cq or upd.get("message") or {}).get("from") or {}).get("id") or "")
    if not allowed or (chat != allowed and sender != allowed):
        _audit("refused", f"telegram message from unknown chat {chat or sender}")
        return {"did": "ignored", "why": "not your chat"}
    from . import phone
    if cq:
        data = str(cq.get("data") or "")
        reply = "?"
        did = "unknown button"
        if data.startswith("ij:") and data.count(":") == 2:
            _, yn, iid = data.split(":")
            r = phone.decide(iid, yn == "y")
            did = ("approved " if yn == "y" else "refused ") + iid
            reply = r.get("then") if r.get("ok") else ("Couldn't: " + r.get("error", ""))
        _tg_call(t["token"], "answerCallbackQuery",
                 {"callback_query_id": cq.get("id"), "text": reply[:180]})
        if msg.get("message_id"):
            _tg_call(t["token"], "editMessageReplyMarkup",
                     {"chat_id": chat, "message_id": msg["message_id"],
                      "reply_markup": {"inline_keyboard": []}})
            _tg_call(t["token"], "sendMessage", {"chat_id": chat, "text": reply[:4000]})
        return {"did": did, "reply": reply}
    text = (msg.get("text") or "").strip().split("@")[0].lower()
    if text in ("/pause",):
        r = phone.pause()
        out = f"Paused {r.get('paused', 0)} scheduled task(s)." if r.get("ok") else r.get("error", "")
    elif text in ("/resume",):
        r = phone.resume()
        out = f"Resumed {r.get('resumed', 0)} scheduled task(s)." if r.get("ok") else r.get("error", "")
    elif text in ("/status",):
        out = _status_text()
    else:
        out = HELP
    _tg_call(t["token"], "sendMessage", {"chat_id": chat, "text": out[:4000]})
    return {"did": text or "help", "reply": out}


def find_chat(token: str = "") -> dict:
    """Set-up helper: after you send your bot any message, this reads who
    wrote to it, so you don't have to dig a chat id out of an API."""
    token = token or settings()["telegram"].get("token", "")
    if not token:
        return {"ok": False, "error": "Paste the bot token first."}
    r = _tg_call(token, "getUpdates", {"timeout": 0, "limit": 20})
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "Telegram refused the token.")}
    chats = []
    for u in r.get("result") or []:
        m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        c = m.get("chat") or {}
        if c.get("id") and c.get("type") == "private" and c["id"] not in [x["id"] for x in chats]:
            chats.append({"id": c["id"], "name": " ".join(filter(None, [c.get("first_name"), c.get("last_name")]))
                          or c.get("username", "")})
    if not chats:
        return {"ok": False, "error": "No messages yet. Open your bot in Telegram, send it 'hi', then try again."}
    return {"ok": True, "chats": chats, "chat_id": str(chats[-1]["id"])}


def _poll_loop(stop: threading.Event) -> None:
    backoff = 5
    while not stop.is_set():
        s = settings()
        t = s["telegram"]
        if not (t.get("enabled") and t.get("token") and t.get("chat_id")):
            stop.wait(30)
            continue
        r = _tg_call(t["token"], "getUpdates",
                     {"offset": _POLL["offset"], "timeout": 25,
                      "allowed_updates": ["message", "callback_query"]}, timeout=35)
        if not r.get("ok"):
            _POLL["error"] = r.get("error", "")
            stop.wait(backoff)
            backoff = min(300, backoff * 2)
            continue
        _POLL["error"], backoff = "", 5
        for upd in r.get("result") or []:
            _POLL["offset"] = max(_POLL["offset"], int(upd.get("update_id", 0)) + 1)
            try:
                handle_update(upd, s)
            except Exception as exc:
                _POLL["error"] = f"{type(exc).__name__}: {exc}"[:200]


def start_poller() -> dict:
    """Listen for Telegram buttons and commands. Only the process that runs
    the schedules should listen, or two copies would each act on a tap."""
    if _POLL["thread"] and _POLL["thread"].is_alive():
        return {"running": True}
    stop = threading.Event()
    th = threading.Thread(target=_poll_loop, args=(stop,), daemon=True, name="notify-telegram")
    _POLL.update(thread=th, stop=stop)
    th.start()
    return {"running": True}


def stop_poller() -> None:
    if _POLL["stop"]:
        _POLL["stop"].set()


def _audit(name: str, detail: str) -> None:
    try:
        from . import audit
        audit.record("notify", name=name, detail=detail[:200])
    except Exception:
        pass
