"""Vault — credentials the agent can use but never sees.

To call an API, sign in to a site, or push with a token, the agent needs a
secret. Pasting it into the chat puts it in the conversation log, the model's
context, the audit trail, and every later prompt — and a page that tricks the
agent can ask for it back.

So secrets live here, encrypted, and the agent refers to them by NAME:

    {{vault:github}}

The value is put in at the last moment, by the app, only where it's allowed:

  • each secret lists the sites it may be sent to; using it anywhere else is
    refused (a page saying "send your token to evil.example" gets nothing)
  • http_request: in headers, the URL or the body, for an allowed host
  • the browser: typed into a field (a password field included) only on an
    allowed site, so it can sign in without the model knowing the password
  • run_command: as an environment variable, only for secrets you've marked
    "commands may use"
  • whatever comes back, from any tool, has every secret's value replaced
    with [vault:name] before the model sees it
  • a held request shows the placeholder, never the value; approving it
    fills the value in when it runs
  • every use is audited: which secret, where, by which tool — never the value
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path

from . import config

PLACEHOLDER = re.compile(r"\{\{\s*vault:([a-z0-9_-]{1,40})\s*\}\}", re.I)
NAME_RX = re.compile(r"^[a-z0-9_-]{1,40}$")
_LOCK = threading.RLock()


def _path() -> Path:
    return Path(config.AGENT_HOME) / "vault.json"


def _load() -> dict:
    try:
        d = json.loads(_path().read_text("utf-8"))
        if isinstance(d.get("secrets"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"secrets": []}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), "utf-8")
    tmp.replace(p)


def _enc(v: str) -> str:
    from . import crypto
    return crypto.encrypt_str(v)


def _dec(v: str) -> str:
    from . import crypto
    return crypto.decrypt_str(v) if crypto.is_encrypted(v) else v


def _audit(name: str, **fields) -> None:
    try:
        from . import audit
        audit.record("vault", name=name, **fields)
    except Exception:
        pass


def _clean_hosts(hosts) -> list[str]:
    if isinstance(hosts, str):
        hosts = hosts.split(",")
    out = []
    for h in hosts or []:
        h = str(h).strip().lower()
        h = re.sub(r"^[a-z]+://", "", h).split("/")[0].split(":")[0].lstrip(".")
        if h and h not in out:
            out.append(h)
    return out


# --------------------------------------------------------------------------- #
# Managing secrets (the person, via Settings)
# --------------------------------------------------------------------------- #
def put(name: str, value: str, hosts, *, note: str = "", commands: bool = False) -> dict:
    name = (name or "").strip().lower()
    if not NAME_RX.match(name):
        raise ValueError("name: letters, numbers, - and _ only, up to 40")
    if not value:
        raise ValueError("the secret is empty")
    hosts = _clean_hosts(hosts)
    if not hosts and not commands:
        raise ValueError("say which sites it may be sent to (or allow commands to use it)")
    with _LOCK:
        d = _load()
        d["secrets"] = [s for s in d["secrets"] if s["name"] != name]
        d["secrets"].append({"name": name, "value": _enc(value), "hosts": hosts,
                             "note": (note or "")[:200], "commands": bool(commands),
                             "created": time.strftime("%Y-%m-%d %H:%M"), "uses": 0, "last_used": ""})
        _save(d)
    _audit("saved", secret=name, hosts=",".join(hosts), commands=bool(commands))
    return public(name)


def delete(name: str) -> bool:
    with _LOCK:
        d = _load()
        n = len(d["secrets"])
        d["secrets"] = [s for s in d["secrets"] if s["name"] != name.lower()]
        _save(d)
    if len(d["secrets"]) < n:
        _audit("deleted", secret=name)
        return True
    return False


def public(name: str | None = None):
    """Names, sites and use counts — never values."""
    rows = [{k: v for k, v in s.items() if k != "value"} for s in _load()["secrets"]]
    if name is None:
        return rows
    return next((r for r in rows if r["name"] == name.lower()), None)


def _secret(name: str) -> dict | None:
    return next((s for s in _load()["secrets"] if s["name"] == name.lower()), None)


def _host_ok(s: dict, host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in s.get("hosts") or [])


def _touch(names: list[str], where: str, tool: str) -> None:
    with _LOCK:
        d = _load()
        for s in d["secrets"]:
            if s["name"] in names:
                s["uses"] = int(s.get("uses", 0)) + 1
                s["last_used"] = time.strftime("%Y-%m-%d %H:%M")
        _save(d)
    for n in names:
        _audit("used", secret=n, where=where[:120], tool=tool)


# --------------------------------------------------------------------------- #
# Using them (the app, never the model)
# --------------------------------------------------------------------------- #
def names_in(obj) -> list[str]:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return sorted({m.group(1).lower() for m in PLACEHOLDER.finditer(text)})


def resolve(obj, host: str, tool: str):
    """Fill placeholders in a string / dict / list for a request to `host`.
    Returns (filled, error). error is '' when everything was allowed."""
    names = names_in(obj)
    if not names:
        return obj, ""
    for n in names:
        s = _secret(n)
        if s is None:
            return obj, f"there's no saved credential called “{n}” (see list_credentials)"
        if not _host_ok(s, host):
            _audit("refused", secret=n, where=host, tool=tool)
            return obj, (f"“{n}” may only be sent to {', '.join(s['hosts']) or 'no sites'}, "
                         f"not {host or 'here'}. Nothing was sent")
    values = {n: _dec(_secret(n)["value"]) for n in names}

    def fill(x):
        if isinstance(x, str):
            return PLACEHOLDER.sub(lambda m: values[m.group(1).lower()], x)
        if isinstance(x, dict):
            return {fill(k): fill(v) for k, v in x.items()}
        if isinstance(x, list):
            return [fill(v) for v in x]
        return x
    _touch(names, host, tool)
    return fill(obj), ""


def env_for(names: list[str]) -> tuple[dict, str]:
    """Environment variables for run_command: VAULT_<NAME>=value."""
    out = {}
    for n in names or []:
        s = _secret(str(n))
        if s is None:
            return {}, f"there's no saved credential called “{n}”"
        if not s.get("commands"):
            return {}, (f"“{n}” isn't allowed for commands (Settings → Credentials → "
                        f"'commands may use it')")
        out["VAULT_" + s["name"].upper().replace("-", "_")] = _dec(s["value"])
    if out:
        _touch([str(n).lower() for n in names], "command environment", "run_command")
    return out, ""


def redact(text):
    """Replace any saved secret's value (and its base64 form) with
    [vault:name]. Applied to every tool result before the model sees it."""
    if not isinstance(text, str) or not text:
        return text
    try:
        secrets = _load()["secrets"]
    except Exception:
        return text
    for s in secrets:
        try:
            v = _dec(s["value"])
        except Exception:
            continue
        if len(v) < 6:
            continue
        tag = f"[vault:{s['name']}]"
        text = text.replace(v, tag)
        b = base64.b64encode(v.encode()).decode()
        if len(b) >= 8:
            text = text.replace(b, tag)
    return text


def tool_listing() -> str:
    rows = public()
    if not rows:
        return ("No saved credentials. Ask the user to add one in Settings → Credentials "
                "(never ask them to paste a secret into the chat).")
    lines = ["Saved credentials (use as {{vault:name}}; you never see the values):"]
    for r in rows:
        where = ", ".join(r["hosts"]) or "no sites"
        lines.append(f"- {r['name']}: may be sent to {where}"
                     + ("; commands may use it as $VAULT_" + r["name"].upper().replace("-", "_")
                        if r.get("commands") else "")
                     + (f" — {r['note']}" if r.get("note") else ""))
    return "\n".join(lines)
