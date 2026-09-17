"""MCP client — connect Agent Jo to Model Context Protocol servers.

Adds external tool servers (GitHub, filesystems, databases, Slack, and hundreds
more) as first-class tools the agent can call. Configuration lives in
AGENT_HOME/mcp.json:

    {"servers": {
        "github":  {"transport": "stdio", "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "..."}, "enabled": true},
        "remote":  {"transport": "http", "url": "https://host/mcp",
                    "headers": {"Authorization": "Bearer ..."}, "enabled": true}
    }}

Design choices, deliberately:
  • No new dependencies — a hand-rolled JSON-RPC client (stdio is one JSON
    object per line; HTTP is the streamable-HTTP POST flavour via httpx, which
    the app already ships).
  • Tools only. The MCP spec also covers resources, prompts and sampling; this
    client implements the initialize handshake, tools/list and tools/call —
    the part that makes servers useful to an agent. Server→client requests get
    a polite method-not-found (pings are answered) so nothing deadlocks.
  • Lazy + fail-safe. Servers connect on first use, a broken server degrades to
    an error string instead of breaking the turn, and reconnects are
    cooldown-limited so a dead server can't stall every message.

Exposed tool names are namespaced `mcp_<server>_<tool>` so they can never
collide with built-ins, and each description is prefixed with its origin.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

from . import config

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "agent-jo", "version": "1.0"}
INIT_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
RECONNECT_COOLDOWN = 30.0
_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _cfg_path():
    return config.AGENT_HOME / "mcp.json"


def load_config() -> dict:
    try:
        data = json.loads(_cfg_path().read_text("utf-8"))
        return data if isinstance(data.get("servers"), dict) else {"servers": {}}
    except Exception:
        return {"servers": {}}


def save_config(data: dict) -> None:
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), "utf-8")


def _safe(name: str) -> str:
    return (_SAFE.sub("_", name).strip("_") or "srv")[:40]


# --------------------------------------------------------------------------- #
#  transports — send one JSON-RPC message, receive routed replies
# --------------------------------------------------------------------------- #
class _StdioTransport:
    """Spawn the server as a subprocess; newline-delimited JSON both ways."""

    def __init__(self, command: str, args: list, env: dict | None):
        resolved = shutil.which(command) or command   # Windows: npx -> npx.cmd
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in (env or {}).items()})
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [resolved] + list(args or []), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env, text=True, encoding="utf-8", bufsize=1, **kw)
        self.stderr_tail: deque = deque(maxlen=12)
        self._pending: dict = {}
        self._plock = threading.Lock()
        self._wlock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _stderr_loop(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail.append(line.rstrip()[:300])
        except Exception:
            pass

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self._route(msg)
        except Exception:
            pass
        # process ended: release all waiters
        with self._plock:
            for slot in self._pending.values():
                slot["msg"] = {"error": {"message": "server exited"}}
                slot["ev"].set()

    def _route(self, msg: dict):
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._plock:
                slot = self._pending.pop(msg["id"], None)
            if slot:
                slot["msg"] = msg
                slot["ev"].set()
        elif msg.get("method") == "ping" and "id" in msg:
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif "id" in msg:                      # server->client request we don't do
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": "not supported"}})
        # notifications from the server are ignored

    def _write(self, msg: dict):
        with self._wlock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None, timeout: float,
                _id_counter=[0]) -> dict:
        _id_counter[0] += 1
        rid = _id_counter[0]
        slot = {"ev": threading.Event(), "msg": None}
        with self._plock:
            self._pending[rid] = slot
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params or {}})
        if not slot["ev"].wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
        msg = slot["msg"] or {}
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None):
        self._write({"jsonrpc": "2.0", "method": method,
                     "params": params or {}})

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


class _HttpTransport:
    """Streamable-HTTP flavour: POST each JSON-RPC message; accept a plain JSON
    reply or a single SSE stream carrying it. Session id header is echoed back
    once the server assigns one. Minimal but sufficient for tool use."""

    def __init__(self, url: str, headers: dict | None):
        import httpx
        self.url = url
        self.headers = dict(headers or {})
        self.session_id = None
        self.client = httpx.Client(timeout=30.0)
        self.stderr_tail: deque = deque(maxlen=1)   # parity with stdio
        self.alive = True

    def _hdrs(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(self.headers)
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def request(self, method: str, params: dict | None, timeout: float,
                _id_counter=[0]) -> dict:
        _id_counter[0] += 1
        rid = _id_counter[0]
        body = {"jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {}}
        r = self.client.post(self.url, json=body, headers=self._hdrs(),
                             timeout=timeout)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            msg = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        cand = json.loads(line[5:].strip())
                        if cand.get("id") == rid:
                            msg = cand
                    except Exception:
                        continue
            if msg is None:
                raise RuntimeError("no matching SSE response")
        else:
            msg = r.json()
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None):
        try:
            self.client.post(self.url, headers=self._hdrs(),
                             json={"jsonrpc": "2.0", "method": method,
                                   "params": params or {}}, timeout=10.0)
        except Exception:
            pass

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.alive = False


# --------------------------------------------------------------------------- #
#  one configured server
# --------------------------------------------------------------------------- #
class ServerConn:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.transport = None
        self.server_info: dict = {}
        self.tools: list = []
        self.error: str = ""
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.spec.get("enabled", True))

    @property
    def connected(self) -> bool:
        return bool(self.transport and getattr(self.transport, "alive", False)
                    and not self.error)

    def ensure(self) -> bool:
        """Connect + handshake if needed. False (with .error set) on failure."""
        with self._lock:
            if not self.enabled:
                return False
            if self.connected and self.tools is not None:
                return True
            if time.time() - self._last_attempt < RECONNECT_COOLDOWN and self.error:
                return False
            self._last_attempt = time.time()
            self.error = ""
            try:
                if self.transport:
                    self.transport.close()
                if self.spec.get("transport", "stdio") == "http":
                    self.transport = _HttpTransport(self.spec["url"],
                                                    self.spec.get("headers"))
                else:
                    self.transport = _StdioTransport(
                        self.spec["command"], self.spec.get("args", []),
                        self.spec.get("env"))
                init = self.transport.request(
                    "initialize",
                    {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                     "clientInfo": CLIENT_INFO}, INIT_TIMEOUT)
                self.server_info = init.get("serverInfo", {})
                self.transport.notify("notifications/initialized")
                listed = self.transport.request("tools/list", {}, INIT_TIMEOUT)
                self.tools = listed.get("tools", []) or []
                return True
            except Exception as exc:
                tail = ""
                try:
                    tail = " | ".join(list(self.transport.stderr_tail)[-3:])
                except Exception:
                    pass
                self.error = f"{type(exc).__name__}: {exc}" + (
                    f" [{tail}]" if tail else "")
                try:
                    from . import issues
                    issues.note_error(f"mcp:{self.name}", self.error)
                except Exception:
                    pass
                return False

    def call(self, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT) -> str:
        if not self.ensure():
            return f"MCP server '{self.name}' unavailable: {self.error}"
        try:
            res = self.transport.request(
                "tools/call", {"name": tool, "arguments": arguments or {}},
                timeout)
        except Exception as exc:
            # a call-level failure (timeout, bad args) doesn't mean the server
            # is dead — leave connection state alone; a genuinely dead process
            # is caught by ensure() via transport.alive on the next use
            return f"MCP tool error ({self.name}/{tool}): {type(exc).__name__}: {exc}"
        parts = []
        for c in res.get("content", []) or []:
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"[{c.get('type','content')} omitted]")
        out = "\n".join(p for p in parts if p) or "(no content returned)"
        if res.get("isError"):
            out = f"MCP tool reported an error:\n{out}"
        return out[:12000]

    def close(self):
        if self.transport:
            self.transport.close()
        self.transport = None
        self.tools = []


# --------------------------------------------------------------------------- #
#  manager — the app-facing singleton
# --------------------------------------------------------------------------- #
class Manager:
    def __init__(self):
        self.servers: dict[str, ServerConn] = {}
        self._route: dict[str, tuple] = {}     # namespaced -> (server, tool)
        self.reload()

    def reload(self):
        cfg = load_config()
        for name, conn in list(self.servers.items()):
            if name not in cfg["servers"]:
                conn.close()
                del self.servers[name]
        for name, spec in cfg["servers"].items():
            if name in self.servers:
                self.servers[name].spec = spec
            else:
                self.servers[name] = ServerConn(name, spec)

    # -- config mutation ---------------------------------------------------- #
    def add(self, name: str, spec: dict):
        cfg = load_config()
        cfg["servers"][name] = spec
        save_config(cfg)
        self.reload()

    def remove(self, name: str):
        cfg = load_config()
        cfg["servers"].pop(name, None)
        save_config(cfg)
        self.reload()

    def set_enabled(self, name: str, enabled: bool):
        cfg = load_config()
        if name in cfg["servers"]:
            cfg["servers"][name]["enabled"] = bool(enabled)
            save_config(cfg)
        self.reload()
        conn = self.servers.get(name)
        if conn:
            if enabled:                        # user asked: retry right now
                conn._last_attempt = 0.0
                conn.error = ""
            else:
                conn.close()

    # -- tool bridge --------------------------------------------------------- #
    def tool_definitions(self) -> list:
        """TOOL_DEFINITIONS-shaped entries for every connected server's tools."""
        defs = []
        self._route = {}
        for name, conn in self.servers.items():
            if not conn.enabled:
                continue
            conn.ensure()
            for t in conn.tools or []:
                base = f"mcp_{_safe(name)}_{_safe(t.get('name', 'tool'))}"[:64]
                nm, i = base, 2
                while nm in self._route:
                    nm = f"{base[:60]}_{i}"; i += 1
                self._route[nm] = (name, t.get("name", ""))
                schema = t.get("inputSchema") or {"type": "object",
                                                  "properties": {}}
                defs.append({
                    "name": nm,
                    "description": (f"[MCP:{name}] "
                                    + (t.get("description") or ""))[:900],
                    "input_schema": schema,
                })
        return defs

    def call(self, namespaced: str, arguments: dict) -> str:
        if namespaced not in self._route:
            self.tool_definitions()            # refresh routing once
        route = self._route.get(namespaced)
        if not route:
            return f"Unknown MCP tool: {namespaced}"
        sname, tool = route
        conn = self.servers.get(sname)
        if not conn:
            return f"MCP server '{sname}' is not configured."
        return conn.call(tool, arguments)

    def statuses(self) -> list:
        out = []
        for name, conn in sorted(self.servers.items()):
            out.append({"name": name,
                        "transport": conn.spec.get("transport", "stdio"),
                        "target": conn.spec.get("url")
                        or " ".join([conn.spec.get("command", "")]
                                    + list(conn.spec.get("args", []))),
                        "enabled": conn.enabled,
                        "connected": conn.connected,
                        "tools": len(conn.tools or []),
                        "server": conn.server_info.get("name", ""),
                        "error": conn.error})
        return out

    def summary(self) -> dict:
        st = self.statuses()
        return {"servers": len(st),
                "connected": sum(1 for s in st if s["connected"]),
                "tools": sum(s["tools"] for s in st if s["enabled"])}

    def connect_now(self, name: str) -> dict:
        conn = self.servers.get(name)
        if not conn:
            return {"ok": False, "error": "unknown server"}
        conn._last_attempt = 0.0               # bypass cooldown for manual retry
        ok = conn.ensure()
        return {"ok": ok, "error": conn.error,
                "tools": len(conn.tools or [])}


manager = Manager()
