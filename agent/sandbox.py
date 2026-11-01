"""Sandbox — what a command may do when nobody is watching.

Unattended runs (schedules, goals, triggers, the crew) can run shell
commands: read-only ones, ones matching a rule you saved, or anything at all
under full access. Those commands ran as you, with every API key in their
environment and the whole disk in reach. One misleading web page, read by an
agent with full access at 3am, was one `curl -d @~/.ssh/id_rsa` from a bad
morning.

Three modes (Settings → Sandbox), applied to unattended runs by default or
to every run if you choose:

  guard    (default) Before a command runs, it's checked against a short
           list of things no unattended run should do: wipe a disk or a home
           folder, shut down, escalate (sudo/runas), install persistence
           (crontab, schtasks, registry run keys, services), read credential
           stores (~/.ssh, ~/.aws, the agent's own data folder, browser
           profiles), send data out (curl -d/-F/-T, wget --post, scp, nc,
           Invoke-WebRequest -Method Post), force-push. A match is refused,
           with the reason, and audited. And the command's ENVIRONMENT is
           scrubbed: API keys, tokens, passwords and secrets are removed.
           Pattern matching is a fence, not a wall: it stops the obvious and
           the injected, not a determined adversary.

  isolate  An OS sandbox, where one exists, as well as everything in guard:
             Linux   bubblewrap (bwrap): the whole system read-only, only the
                     writable folders you list are writable, credential
                     folders and the agent's own data hidden, optionally no
                     network.
             macOS   sandbox-exec, with the same rules.
             Windows no OS sandbox is available here; it falls back to guard
                     and says so.

  off      Commands run as before.

Pure functions (check, scrub_env, wrap) are testable on any machine.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from pathlib import Path

from . import config

UNATTENDED_PREFIXES = ("sched-", "goal-", "trigger-", "crew-", "chain-")
MODES = ("guard", "isolate", "off")

# (category, reason, pattern) — matched case-insensitively against the command
_RULES = [
    ("destroy", "deletes a whole drive, home folder or system path",
     r"\brm\s+(-[a-z]*\s+)*-?[a-z]*[rf][a-z]*\s+(-[a-z]+\s+)*(/|~|\$home|\*|\.\.)(\s|/?\*?\s*$|$)"),
    ("destroy", "deletes a whole drive", r"\b(del|erase|rd|rmdir)\b[^\n]*\s/s\b[^\n]*\b[a-z]:\\?(\s|$)"),
    ("destroy", "deletes a whole drive or profile",
     r"remove-item\b[^\n]*-recurse[^\n]*(\b[a-z]:\\?(\s|$)|\$env:userprofile|~(\s|$))"),
    ("destroy", "formats or overwrites a disk",
     r"\b(mkfs(\.\w+)?|diskpart|format\s+[a-z]:|wipefs|shred)\b|\bdd\b[^\n]*\bof=/dev/"),
    ("system", "shuts down or restarts the machine",
     # only where a command starts, so a file called shutdown-plan.md is fine
     r"(^|[;&|(]\s*|\bsudo\s+)(shutdown|reboot|poweroff|halt|stop-computer|restart-computer)(\s|$)"),
    ("escalate", "asks for administrator rights", r"(^|[;&|(]\s*|\s)(sudo|doas|runas|su\s+-)\b"),
    ("persist", "installs something that runs on its own later",
     r"\bcrontab\s+(-e|-r|\S+\.txt|-)|\bschtasks\s+/create\b|\breg(\.exe)?\s+(add|delete)\b"
     r"|set-itemproperty\b[^\n]*hk(lm|cu)|new-service\b|\bsc(\.exe)?\s+create\b"
     r"|\blaunchctl\s+(load|bootstrap)\b|\bsystemctl\s+(--user\s+)?(enable|link)\b"),
    ("secrets", "reads a credential store",
     r"(\.ssh\b|id_rsa|id_ed25519|\.aws[/\\]|\.gnupg|\.netrc|\.git-credentials|\.docker[/\\]config"
     r"|signing-key\.private|browser-profile|login data|\.local_agent|keychain|credential manager"
     r"|cmdkey\b|security\s+find-(generic|internet)-password)"),
    ("exfiltrate", "sends data off the machine",
     r"\bcurl\b[^\n]*(\s-d\b|\s--data|\s-f\b|\s--form|\s-t\b|\s--upload-file|-x\s*(post|put)|--request\s+(post|put))"
     r"|\bwget\b[^\n]*--post-(data|file)|invoke-(webrequest|restmethod)\b[^\n]*-method\s+(post|put)"
     r"|\b(scp|sftp|ftp|nc|ncat|netcat|socat)\b|\brsync\b[^\n]*\S+:"),
    ("history", "rewrites shared history", r"\bgit\s+push\b[^\n]*(\s--force\b|\s-f\b|\s\+\S)"),
]
_COMPILED = [(c, why, re.compile(p, re.I)) for c, why, p in _RULES]
_SECRET_ENV = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|_PAT$|SESSION_KEY)", re.I)
_KEEP_ENV = {"PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG", "SYSTEMROOT",
             "COMSPEC", "PATHEXT", "WINDIR", "SHELL", "USER", "USERNAME", "TERM", "PWD"}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _path() -> Path:
    return Path(config.AGENT_HOME) / "sandbox.json"


def default_workspace() -> Path:
    return Path.home() / "AgentJo Workspace"


def settings() -> dict:
    s = {"mode": "guard", "applies_to": "unattended", "network": True,
         "writable": [str(default_workspace())]}
    try:
        s.update(json.loads(_path().read_text("utf-8")))
    except (OSError, ValueError):
        pass
    if s["mode"] not in MODES:
        s["mode"] = "guard"
    if s["applies_to"] not in ("unattended", "always"):
        s["applies_to"] = "unattended"
    s["writable"] = [str(Path(p).expanduser()) for p in s.get("writable") or [] if str(p).strip()]
    return s


def save(update: dict) -> dict:
    s = settings()
    for k in ("mode", "applies_to", "network", "writable"):
        if k in update and update[k] is not None:
            s[k] = update[k]
    if s["mode"] not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=1), "utf-8")
    _audit("settings", detail=f"mode {s['mode']}, {s['applies_to']}, network {s['network']}")
    return status()


def is_unattended(session_id: str) -> bool:
    return str(session_id or "").startswith(UNATTENDED_PREFIXES)


def applies(session_id: str, s: dict | None = None) -> bool:
    s = s or settings()
    if s["mode"] == "off":
        return False
    return s["applies_to"] == "always" or is_unattended(session_id)


# --------------------------------------------------------------------------- #
# The pure parts
# --------------------------------------------------------------------------- #
def check(command: str) -> dict:
    """{'ok': bool, 'category', 'why'} for one command line."""
    cmd = " ".join(str(command or "").split())
    for cat, why, rx in _COMPILED:
        if rx.search(cmd):
            return {"ok": False, "category": cat, "why": why}
    return {"ok": True, "category": "", "why": ""}


def scrub_env(env: dict | None = None) -> dict:
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items()
            if k.upper() in _KEEP_ENV or not _SECRET_ENV.search(k)}


def hidden_paths() -> list[str]:
    home = Path.home()
    cands = [home / ".ssh", home / ".aws", home / ".gnupg", home / ".netrc",
             home / ".git-credentials", home / ".docker", home / ".config" / "gcloud",
             home / ".kube", Path(config.AGENT_HOME)]
    return [str(p) for p in cands if p.exists()]


def isolation_kind() -> str:
    sysname = platform.system()
    if sysname == "Linux" and shutil.which("bwrap"):
        return "bwrap"
    if sysname == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return ""


def wrap_bwrap(command: str, writable: list[str], hide: list[str], network: bool, cwd: str) -> list[str]:
    argv = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp"]
    for h in hide:                       # hidden first …
        argv += ["--tmpfs", h] if Path(h).is_dir() else ["--ro-bind", "/dev/null", h]
    for w in writable:                   # … so a workspace inside one is still reachable
        argv += ["--bind", w, w]
    if not network:
        argv.append("--unshare-net")
    argv += ["--chdir", cwd, "/bin/sh", "-c", command]
    return argv


def _sb_quote(p: str) -> str:
    return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(writable: list[str], hide: list[str], network: bool) -> str:
    rules = ["(version 1)", "(allow default)",
             "(deny file-write* (require-not (require-any "
             + " ".join(f"(subpath {_sb_quote(w)})" for w in writable + ["/private/tmp", "/private/var/folders", "/dev"])
             + ")))"]
    if hide:
        rules.append("(deny file-read* " + " ".join(f"(subpath {_sb_quote(h)})" for h in hide) + ")")
    if not network:
        rules.append("(deny network-outbound (remote ip))")
    return "\n".join(rules)


# --------------------------------------------------------------------------- #
# The one call run_command makes
# --------------------------------------------------------------------------- #
def prepare(command: str, session_id: str = "") -> dict:
    """How to run this command, or why not.

    {'refuse': str} | {'argv'|'shell': ..., 'env': dict, 'cwd': str|None, 'note': str}"""
    s = settings()
    if not applies(session_id, s):
        return {"shell": command, "env": None, "cwd": None, "note": ""}
    verdict = check(command)
    if not verdict["ok"]:
        _audit("refused", category=verdict["category"], command=command[:160], session=session_id)
        return {"refuse": (f"Refused by the sandbox: this command {verdict['why']} "
                           f"({verdict['category']}), and this is an unattended run. Nothing ran. "
                           f"If it's genuinely needed, it's the person's call: mark the step "
                           f"'blocked' with exactly what to run, and carry on with the rest.")}
    env = scrub_env()
    if s["mode"] == "isolate":
        kind = isolation_kind()
        writable = [w for w in s["writable"] if Path(w).exists()]
        for w in s["writable"]:
            Path(w).mkdir(parents=True, exist_ok=True)
        writable = [w for w in s["writable"] if Path(w).is_dir()]
        cwd = writable[0] if writable else str(Path.home())
        if kind == "bwrap":
            return {"argv": wrap_bwrap(command, writable, hidden_paths(), s["network"], cwd),
                    "env": env, "cwd": cwd, "note": "isolated (bubblewrap)"}
        if kind == "sandbox-exec":
            return {"argv": ["sandbox-exec", "-p", macos_profile(writable, hidden_paths(), s["network"]),
                             "/bin/sh", "-c", command],
                    "env": env, "cwd": cwd, "note": "isolated (sandbox-exec)"}
        return {"shell": command, "env": env, "cwd": None,
                "note": "guarded (no OS sandbox on this machine; the command list and a clean environment still apply)"}
    return {"shell": command, "env": env, "cwd": None, "note": "guarded"}


def status() -> dict:
    s = settings()
    kind = isolation_kind()
    return {**s, "isolation_available": bool(kind), "isolation_kind": kind or "none",
            "hidden": hidden_paths(), "rules": [{"category": c, "why": w} for c, w, _ in _RULES]}


def _audit(name: str, **fields) -> None:
    try:
        from . import audit
        audit.record("sandbox", name=name, **fields)
    except Exception:
        pass
