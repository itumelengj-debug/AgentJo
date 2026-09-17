"""First-run setup — so someone who isn't you can actually use this.

Until now the only way to give the app an engine was to set ANTHROPIC_API_KEY
in the environment before launching. That's fine on the machine it was built
on and a dead end for anyone you hand it to: the app opens, looks complete,
and every message comes back "no engine is configured" with no way to fix it
from the screen in front of you.

So: the key can be entered in the app, and it is stored the same way the
custom-engine keys already are — sealed at rest with AGENT_HOME/secret.key,
which lives outside any backup by default. A shared copy of the app therefore
carries no credentials, and the person you gave it to uses their own.

Ollama is offered as the alternative on equal footing rather than a footnote,
because "free, private, already on your machine" is the better answer for a
lot of people and the app supports it fully.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config

CRED_FILE = "credentials.json"
_ANTHROPIC_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def _path() -> Path:
    config.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    return config.AGENT_HOME / CRED_FILE


def _read() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except Exception:
        return {}


def _write(data: dict) -> None:
    p = _path()
    p.write_text(json.dumps(data, indent=2), "utf-8")
    try:                       # best effort on POSIX; a no-op on Windows
        os.chmod(p, 0o600)
    except Exception:
        pass


def load_saved_credentials() -> bool:
    """Put a stored key into the environment so the engines find it. Called
    once at startup, before anything constructs a brain."""
    data = _read()
    sealed = data.get("anthropic_api_key")
    if not sealed:
        return False
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True            # an explicit env var always wins
    try:
        from . import crypto
        key = crypto.decrypt_str(sealed) if crypto.is_encrypted(sealed) \
            else sealed
    except Exception:
        return False
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        return True
    return False


def ollama_reachable() -> bool:
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
        return bool(r.json().get("models"))
    except Exception:
        return False


def has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("AGENT_API_KEY"))


def state() -> dict:
    """What the setup screen needs to know, and nothing secret."""
    ollama = ollama_reachable()
    key = has_key()
    try:
        from . import brain as brainmod
        customs = list(brainmod.custom_engine_names())
    except Exception:
        customs = []
    return {
        "configured": bool(key or ollama or customs),
        "has_key": key,
        "key_source": ("environment" if os.environ.get("ANTHROPIC_API_KEY")
                       and not _read().get("anthropic_api_key")
                       else "saved" if _read().get("anthropic_api_key")
                       else ""),
        "ollama": ollama,
        "custom_engines": customs,
        "data_dir": str(config.AGENT_HOME),
        "build": getattr(config, "BUILD_ID", "unknown"),
    }


def validate_key(key: str) -> str:
    """'' when it looks usable, else what's wrong. Format only — a live check
    costs money and time, and a typo is the common case."""
    key = (key or "").strip()
    if not key:
        return "Paste your Anthropic API key, or choose Ollama instead."
    # order matters: check for damage BEFORE accepting on prefix, or a key
    # pasted with a stray space is stored broken and fails later with a
    # confusing 401 instead of here with a clear message
    if re.search(r"\s", key):
        return "That contains a space or line break — copy the key on its own."
    if not key.startswith("sk-"):
        return "Anthropic keys start with 'sk-ant-'. That isn't one."
    if not key.startswith("sk-ant-"):
        return ("That looks like a different provider's key. Anthropic keys "
                "start with 'sk-ant-' — other providers go under Engines.")
    if len(key) < 30:
        return "That looks truncated — check you copied the whole key."
    return ""


def save_api_key(key: str) -> dict:
    key = (key or "").strip()
    why = validate_key(key)
    if why:
        return {"ok": False, "error": why}
    data = _read()
    try:
        from . import crypto
        data["anthropic_api_key"] = crypto.encrypt_str(key)
        sealed = True
    except Exception:
        data["anthropic_api_key"] = key      # better stored than refused
        sealed = False
    _write(data)
    os.environ["ANTHROPIC_API_KEY"] = key
    _audit("key-saved", "sealed" if sealed else "stored unsealed")
    return {"ok": True, "sealed": sealed,
            "note": ("Saved. It's sealed with this machine's secret.key, "
                     "which backups leave out by default."
                     if sealed else
                     "Saved, but this machine couldn't seal it — treat the "
                     "data folder as sensitive.")}


def clear_api_key() -> dict:
    data = _read()
    data.pop("anthropic_api_key", None)
    _write(data)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    _audit("key-cleared", "")
    return {"ok": True}


def _audit(name: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("setup", name=name, summary=summary[:200])
    except Exception:
        pass
