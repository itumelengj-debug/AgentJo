"""Optional password protection for the web app.

Stays completely out of the way until a password is set: ``is_enabled()`` is
False and the server runs open (localhost-only, as before). Once a password is
configured, all data endpoints require a valid session cookie.

No third-party dependencies - passwords are stored as a salted PBKDF2-HMAC-SHA256
hash, and session cookies are stateless HMAC-signed tokens, so this works across
a future multi-worker deployment without a shared session store. Secrets live in
``AGENT_HOME/auth.json`` (chmod 600 where the OS allows it); protect that folder.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import agent.config as config

_PBKDF_ITERS = 200_000
_TOKEN_MAX_AGE = 30 * 24 * 3600        # sessions last 30 days
COOKIE = "aj_session"
_MIN_LEN = 4


def _auth_path() -> Path:
    return config.AGENT_HOME / "auth.json"


def _load() -> dict:
    try:
        return json.loads(_auth_path().read_text("utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    p = _auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), "utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def is_enabled() -> bool:
    d = _load()
    return bool(d.get("password_hash") and d.get("salt"))


def _hash(password: str, salt_hex: str, iters: int) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), iters)
    return dk.hex()


def set_password(password: str) -> None:
    if not password or len(password) < _MIN_LEN:
        raise ValueError(f"Password must be at least {_MIN_LEN} characters.")
    d = _load()
    salt = secrets.token_hex(16)
    d["salt"] = salt
    d["iterations"] = _PBKDF_ITERS
    d["password_hash"] = _hash(password, salt, _PBKDF_ITERS)
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(32)
    _save(d)


def verify_password(password: str) -> bool:
    d = _load()
    if not d.get("password_hash") or not d.get("salt"):
        return False
    calc = _hash(password, d["salt"], int(d.get("iterations", _PBKDF_ITERS)))
    return hmac.compare_digest(calc, d["password_hash"])


def disable() -> None:
    d = _load()
    for k in ("password_hash", "salt", "iterations"):
        d.pop(k, None)
    _save(d)


def _secret() -> bytes:
    d = _load()
    s = d.get("secret")
    if not s:
        s = secrets.token_hex(32)
        d["secret"] = s
        _save(d)
    return bytes.fromhex(s)


def make_token() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(_secret(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    ts, _, sig = token.partition(".")
    if not ts.isdigit():
        return False
    expected = hmac.new(_secret(), ts.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return (time.time() - int(ts)) < _TOKEN_MAX_AGE


def bootstrap_from_env() -> None:
    """Enable auth headlessly if AGENT_WEB_PASSWORD is set and none exists yet."""
    pw = os.environ.get("AGENT_WEB_PASSWORD", "").strip()
    if pw and not is_enabled():
        try:
            set_password(pw)
        except ValueError:
            pass
