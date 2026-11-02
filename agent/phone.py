"""The phone companion for Agent Jo: the OK it's waiting for, from your phone.

Anything that would change something outside this machine — an email, a
form, a post to a service — is held with its exact inputs until you approve
it. A scheduled task that hits one at 7am while you're out simply waits. This
serves a small page to your phone with exactly those decisions:

  * every held call, with exactly what would be sent; approve it (it runs
    once, then and there) or refuse it;
  * your scheduled tasks and how their last run went;
  * pause every scheduled task in one tap (and resume the ones it paused).

It can't chat, read your documents or memory, run commands or change
settings. It is OFF until you turn it on.

How it's kept to you is the same as Agent Jo Jobs' phone companion (the two
apps share no code; this is its own copy): a separate listener (port 8768),
a 192-bit key in the link's #fragment sent as a header and compared in
constant time, lock-out after ten wrong keys, no cookies or CORS, strict CSP,
every action audited. On home Wi-Fi it isn't encrypted — use Tailscale away
from home.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

DEFAULT_PORT = 8768
MAX_BODY = 64 * 1024
LOCK_AFTER, LOCK_WINDOW = 10, 600
STATIC = Path(__file__).resolve().parents[1] / "web_phone"
_SERVER = {"httpd": None, "thread": None, "port": None, "error": ""}
_FAILS: dict = {}
_LOCK = threading.Lock()


# --- settings -------------------------------------------------------------- #
_CTX = {"memory": None, "console": None}


def configure(memory, console) -> None:
    """The app's memory store and console, so an approval can run the call."""
    _CTX.update(memory=memory, console=console)


def _path() -> Path:
    d = config.AGENT_HOME
    d.mkdir(parents=True, exist_ok=True)
    return d / "phone.json"


def settings() -> dict:
    try:
        s = json.loads(_path().read_text("utf-8"))
    except Exception:
        s = {}
    return {"enabled": False, "port": DEFAULT_PORT, "key": "", **s}


def _save(s: dict) -> None:
    p = _path()
    p.write_text(json.dumps(s, indent=1), "utf-8")
    try:
        os.chmod(p, 0o600)            # the key is a password
    except Exception:
        pass


def new_key() -> str:
    return secrets.token_urlsafe(24)


def addresses() -> list:
    """This computer's addresses a phone could reach: the LAN one, and a
    Tailscale one (100.x) when Tailscale is running."""
    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))      # no packet is sent
            out.append(s.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except Exception:
        pass
    ts = [ip for ip in out if ip.startswith("100.")]
    lan = [ip for ip in out if not ip.startswith("100.")]
    return [{"ip": ip, "kind": "tailscale"} for ip in ts] + [{"ip": ip, "kind": "wifi"} for ip in lan]


def status() -> dict:
    s = settings()
    running = _SERVER["httpd"] is not None
    port = _SERVER["port"] or s["port"]
    links = []
    if s["enabled"] and s["key"]:
        for a in addresses():
            url = f"http://{a['ip']}:{port}/#k={s['key']}"
            item = {"url": url, "kind": a["kind"], "ip": a["ip"]}
            try:
                from . import qr
                item["qr"] = qr.svg(url, scale=5)
            except Exception:
                item["qr"] = ""
            links.append(item)
    return {"enabled": s["enabled"], "running": running, "port": port,
            "links": links, "error": _SERVER["error"]}


def set_enabled(on: bool) -> dict:
    s = settings()
    s["enabled"] = bool(on)
    if on and not s["key"]:
        s["key"] = new_key()
    _save(s)
    _audit(f"phone companion turned {'on' if on else 'off'}")
    if on:
        start()
    else:
        stop()
    return status()


def rotate_key() -> dict:
    s = settings()
    s["key"] = new_key()
    _save(s)
    _audit("phone companion key changed — the old address no longer works")
    return status()


def _audit(text: str) -> None:
    try:
        from . import audit
        audit.record("phone", name="phone", summary=text[:200])
    except Exception:
        pass


# --- what the phone may see and do --------------------------------------- #
def state() -> dict:
    from . import intercepts
    held = [{"id": i["id"], "tool": i["tool"], "summary": i["summary"], "why": i.get("why_held", ""),
             "at": i["at"], "source": i.get("source", ""),
             "args": __import__("json").dumps(i.get("args") or {}, indent=1, default=str)[:4000]}
            for i in intercepts.pending() if i.get("state") == "waiting"]
    recent = [{"tool": i["tool"], "summary": i["summary"], "state": i["state"],
               "result": str(i.get("result") or "")[:300]}
              for i in intercepts.pending()[::-1] if i.get("state") != "waiting"][:5]
    sched = []
    mem = _CTX["memory"]
    if mem is not None:
        for s in mem.list_schedules():
            sched.append({"id": s["id"], "name": s.get("name", ""), "enabled": bool(s.get("enabled")),
                          "last_status": s.get("last_status") or "", "last_summary": (s.get("last_summary") or "")[:200],
                          "next_run": s.get("next_run")})
    paused = settings().get("paused_ids") or []
    return {"held": held[::-1], "recent": recent, "schedules": sched,
            "paused": bool(paused), "running": sum(1 for x in sched if x["enabled"])}


def decide(item_id: str, approve: bool) -> dict:
    from . import intercepts, tools
    r = intercepts.decide(str(item_id), bool(approve), "decided on the phone")
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "Couldn't decide that.")}
    _audit(f"{'approved' if approve else 'refused'} {r['tool']}: {r['summary'][:100]}")
    if not approve:
        return {"ok": True, "then": "Refused. It won't run."}
    if _CTX["memory"] is None:
        return {"ok": True, "then": "Approved — it runs when Agent Jo next has it open."}
    run = tools.run_held(str(item_id), _CTX["memory"], _CTX["console"])
    return {"ok": True, "then": "Done." if run.get("ok") else
            "Approved, but it failed: " + str(run.get("result") or run.get("error") or "")[:200]}


def pause() -> dict:
    """Turn every scheduled task off, remembering which were on."""
    mem = _CTX["memory"]
    if mem is None:
        return {"ok": False, "error": "Agent Jo isn't running its scheduler right now."}
    on = [s["id"] for s in mem.list_schedules() if s.get("enabled")]
    for sid in on:
        mem.set_schedule_enabled(sid, False, None)
    st = settings()
    st["paused_ids"] = sorted(set((st.get("paused_ids") or []) + on))
    _save(st)
    _audit(f"paused {len(on)} scheduled task(s) from the phone")
    return {"ok": True, "paused": len(on)}


def resume() -> dict:
    """Turn back on only what pause turned off."""
    mem = _CTX["memory"]
    if mem is None:
        return {"ok": False, "error": "Agent Jo isn't running its scheduler right now."}
    from . import scheduler
    st = settings()
    ids = st.get("paused_ids") or []
    n = 0
    for sid in ids:
        sch = mem.get_schedule(sid)
        if sch and not sch.get("enabled"):
            try:
                nxt = scheduler.next_run(scheduler.parse_spec(sch["spec"]))
            except Exception:
                nxt = None
            mem.set_schedule_enabled(sid, True, nxt)
            n += 1
    st["paused_ids"] = []
    _save(st)
    _audit(f"resumed {n} scheduled task(s) from the phone")
    return {"ok": True, "resumed": n}


# --- the listener ------------------------------------------------------------ #
def _locked_out(ip: str) -> bool:
    now = time.time()
    with _LOCK:
        recent = [t for t in _FAILS.get(ip, []) if now - t < LOCK_WINDOW]
        _FAILS[ip] = recent
        return len(recent) >= LOCK_AFTER


def _failed(ip: str) -> None:
    with _LOCK:
        _FAILS.setdefault(ip, []).append(time.time())


_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
          ".webmanifest": "application/manifest+json"}
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY", "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentJoPhone"
    sys_version = ""

    def log_message(self, *a):          # no request log: nothing to leak
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in _HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode("utf-8"))

    def _authorised(self) -> bool:
        ip = self.client_address[0]
        if _locked_out(ip):
            self._json(429, {"error": "Too many wrong keys — try again in ten minutes."})
            return False
        key = settings()["key"]
        got = self.headers.get("X-Phone-Key", "")
        if not key or not got or not hmac.compare_digest(got.encode(), key.encode()):
            _failed(ip)
            self._json(401, {"error": "This link's key isn't valid any more. Scan the code "
                                      "again from Agent Jo on your computer (Settings)."})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            if not self._authorised():
                return
            if path == "/api/state":
                return self._json(200, state())
            return self._json(404, {"error": "not found"})
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file() \
                or f.suffix not in _TYPES:
            return self._send(404, b"not found", "text/plain")
        self._send(200, f.read_bytes(), _TYPES[f.suffix])

    def do_POST(self):
        if not self._authorised():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return self._json(413, {"error": "too large"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "JSON only"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad request"})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/approve":
                res = decide(str(body.get("id", "")), True)
            elif path == "/api/refuse":
                res = decide(str(body.get("id", "")), False)
            elif path == "/api/pause":
                res = pause()
            elif path == "/api/resume":
                res = resume()
            else:
                return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}"})
        self._json(200 if res.get("ok") else 400, res)


def start(port: int | None = None, host: str = "0.0.0.0") -> dict:
    """Start the listener if it's switched on. Safe to call twice."""
    s = settings()
    if not s["enabled"] or not s["key"]:
        return status()
    if _SERVER["httpd"] is not None:
        return status()
    p = int(port or s.get("port") or DEFAULT_PORT)
    try:
        httpd = ThreadingHTTPServer((host, p), _Handler)
    except OSError as exc:
        _SERVER["error"] = (f"Port {p} is in use or not allowed ({exc.strerror or exc}). "
                            "Close whatever uses it, or change the port in phone.json (in your Agent Jo folder).")
        return status()
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    _SERVER.update(httpd=httpd, thread=t, port=httpd.server_address[1], error="")
    return status()


def stop() -> None:
    h = _SERVER.get("httpd")
    if h is not None:
        h.shutdown()
        h.server_close()
    _SERVER.update(httpd=None, thread=None, port=None)


# Telegram buttons and commands (agent/notify.py) act through these.
try:
    from . import notify as _notify
    _notify.use_actions(decide=decide, pause=pause, resume=resume, state=state)
except Exception:
    pass
