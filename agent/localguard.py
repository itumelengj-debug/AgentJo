"""Only this machine's own pages may drive the app.

Both servers listen on 127.0.0.1, and that was treated as the whole defence.
It isn't one. Every web page open in the browser can send requests to
127.0.0.1 — the browser stops a page *reading* another site's response, not
*sending* the request — and three things made that matter here:

  * /api/chat takes form fields, and a form post crosses sites without the
    CORS preflight that would otherwise stop it. The same request carried
    full_access=true, under which files are written and commands run without
    asking. With no password set — the default — any page could have asked.
  * CORS allowed every origin, so the JSON endpoints were open to them too.
  * Nothing checked the Host header, so a site whose name re-resolves to
    127.0.0.1 ("DNS rebinding") counted as the app itself, reads included.

This closes all three, and depends on nothing: the Jobs app vendors it, and a
guard that needs a framework is a guard a refactor can quietly drop.

  Host     A request must be addressed to this machine: a loopback name, an IP
           address (how a phone on the LAN reaches it), this computer's own
           name, or a name added to AGENT_ALLOWED_HOSTS. A rebinding site
           arrives under its own name and stops here.
  Changes  Anything but GET/HEAD/OPTIONS must come from the app's own page.
           Browsers state where a request came from in Sec-Fetch-Site, which a
           page cannot forge; older ones send Origin, which must then match
           the Host. A request with neither came from a script or curl, not
           from a page — those can reach the port directly anyway.

Reads stay open on purpose: following a link to the app is a cross-site GET,
and the browser already refuses to hand another site the response.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket

log = logging.getLogger("agent.localguard")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}
_MACHINE: list = []                     # this computer's names, looked up once


def _csv(name: str) -> list:
    return [x.strip().lower() for x in os.environ.get(name, "").split(",")
            if x.strip()]


def allowed_origins() -> list:
    """Other origins allowed to make changes — a dev server, say.

    Never "*": a wildcard here is exactly the hole this module closes, so it
    is ignored rather than honoured."""
    return [o.rstrip("/") for o in _csv("AGENT_ALLOWED_ORIGINS") if o != "*"]


def split_host(value: str) -> tuple:
    """'Example.com:8765' -> ('example.com', '8765'); '[::1]:8765' -> ('::1', '8765')."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return v, ""
        rest = v[end + 1:]
        return v[1:end], (rest[1:] if rest.startswith(":") else "")
    if v.count(":") == 1:
        host, _, port = v.partition(":")
        return host, port
    return v, ""                        # a bare name, or a bare IPv6 address


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _machine_names() -> set:
    if not _MACHINE:
        names = set()
        try:
            n = socket.gethostname().strip().lower().rstrip(".")
        except Exception:
            n = ""
        if n:
            short = n.split(".")[0]
            names.update({n, short, short + ".local"})
        _MACHINE.append(names)
    return _MACHINE[0]


def host_allowed(host_header: str) -> bool:
    """Is this request addressed to this machine?"""
    host = split_host(host_header)[0].rstrip(".")
    if not host:
        return True                     # no Host: not a browser, which always sends one
    if host in LOOPBACK or host.endswith(".localhost") or _is_ip(host):
        return True
    if host in _machine_names():
        return True
    for extra in _csv("AGENT_ALLOWED_HOSTS"):
        if extra == "*":
            return True                 # an explicit, documented opt-out
        if extra.startswith("."):
            if host.endswith(extra.rstrip(".")):
                return True
        elif host == split_host(extra)[0].rstrip("."):
            return True
    return False


def _netloc(host: str, port: str, scheme: str = "") -> str:
    host = host.rstrip(".")
    if port and (port == _DEFAULT_PORTS.get(scheme)
                 or (not scheme and port in ("80", "443"))):
        port = ""
    return f"{host}:{port}" if port else host


def _origin_netloc(origin: str) -> str:
    scheme, sep, rest = (origin or "").strip().lower().partition("://")
    if not sep:
        return ""
    return _netloc(*split_host(rest.split("/", 1)[0]), scheme)


def refusal(method: str, headers: dict, *, check_host: bool = True) -> str:
    """Why this request may not proceed, or "" if it may.

    ``headers`` is keyed by lower-case header name."""
    host_header = headers.get("host", "")
    if check_host and not host_allowed(host_header):
        name = split_host(host_header)[0] or host_header
        return (f"This app only answers to this computer's own address, and "
                f"this request was addressed to '{name}'. If that name really "
                f"is this computer (a reverse proxy or a Tailscale name, say), "
                f"add it to AGENT_ALLOWED_HOSTS and restart.")
    if (method or "GET").upper() in SAFE_METHODS:
        return ""
    site = headers.get("sec-fetch-site", "").strip().lower()
    origin = headers.get("origin", "").strip()
    if site in ("same-origin", "none"):
        return ""                       # the browser says it's the app's own page
    if origin and origin.lower() != "null":
        o = origin.lower().rstrip("/")
        if o in allowed_origins():
            return ""
        if not site and _origin_netloc(o) == _netloc(*split_host(host_header)):
            return ""                   # an older browser, on the app's own page
    elif not origin and not site:
        return ""                       # a script or curl, not a page in a browser
    return (f"Refused: this request came from {origin or 'another site'}, not "
            f"from the app's own page, and other websites may not make "
            f"changes here. To let a separately hosted front end in, add its "
            f"origin to AGENT_ALLOWED_ORIGINS.")


class LocalOnlyGuard:
    """ASGI middleware applying refusal() to every HTTP and WebSocket request.

    Plain ASGI rather than a framework's middleware class, so it adds nothing
    to a streamed reply (the chat streams) and needs nothing to import."""

    def __init__(self, app, check_host: bool = True):
        self.app = app
        self.check_host = check_host

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {}
        for k, v in scope.get("headers") or []:
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        # a WebSocket handshake is a GET that opens a two-way channel, and
        # CORS doesn't cover it at all — so it is held to the stricter rule
        method = scope.get("method", "GET") if kind == "http" else "WEBSOCKET"
        why = refusal(method, headers, check_host=self.check_host)
        if not why:
            return await self.app(scope, receive, send)
        log.warning("refused %s %s: %s", method, scope.get("path", ""), why)
        if kind == "websocket":
            await receive()                     # the handshake being declined
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": why}).encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
