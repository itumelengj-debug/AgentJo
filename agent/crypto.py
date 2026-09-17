"""At-rest encryption for stored secrets (currently custom-engine API keys).

Dependency-free. A 32-byte master key is generated once and kept in
``AGENT_HOME/secret.key`` (chmod 600 where the OS allows). Values are sealed with
an HMAC-SHA256 keystream (CTR-style PRF) and authenticated with a separate
HMAC-SHA256 tag - encrypt-then-MAC, with independent encryption and MAC subkeys
derived from the master key.

Honest threat model: this protects the secrets if ``engines.json`` is copied,
synced, or backed up *without* ``secret.key`` (the two must be combined to read a
key). It does **not** defend against an attacker who already has full read access
to ``AGENT_HOME`` - guard that folder. For a hardware-backed or per-session
passphrase model, a future version could derive the master key from the login
password instead.

Both the web app and the desktop import this, so encryption is transparent: keys
are encrypted on write and decrypted on read, and any pre-existing plaintext key
is migrated automatically the first time it's loaded.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

import agent.config as config

_PREFIX = "enc:v1:"
_NONCE = 16
_TAG = 32


def _key_path() -> Path:
    return config.AGENT_HOME / "secret.key"


_DPAPI_PREFIX = "dpapi:v1:"


def _read_key_text(txt: str) -> bytes | None:
    """Decode the on-disk key, whether DPAPI-sealed (Windows) or legacy hex."""
    txt = txt.strip()
    if not txt:
        return None
    if txt.startswith(_DPAPI_PREFIX):
        import base64
        from . import windpapi
        return windpapi.unprotect(base64.b64decode(txt[len(_DPAPI_PREFIX):]))
    return bytes.fromhex(txt)


def _serialize_key(key: bytes) -> str:
    """DPAPI-seal on Windows (bound to the login account); plain hex elsewhere."""
    from . import windpapi
    if windpapi.available:
        import base64
        return _DPAPI_PREFIX + base64.b64encode(windpapi.protect(key)).decode("ascii")
    return key.hex()


def _master_key() -> bytes:
    from . import windpapi
    p = _key_path()
    try:
        txt = p.read_text("utf-8")
        key = _read_key_text(txt)
        if key and len(key) == 32:
            # transparently upgrade an older plaintext key to DPAPI on Windows
            if windpapi.available and not txt.strip().startswith(_DPAPI_PREFIX):
                try:
                    p.write_text(_serialize_key(key), "utf-8")
                    os.chmod(p, 0o600)
                except Exception:
                    pass
            return key
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_serialize_key(key), "utf-8")
        os.chmod(p, 0o600)
    except Exception:
        pass
    return key


def _subkeys(master: bytes):
    enc = hmac.new(master, b"agentjo-enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"agentjo-mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(enc_key, nonce + counter.to_bytes(4, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])


def is_encrypted(s) -> bool:
    return isinstance(s, str) and s.startswith(_PREFIX)


def encrypt_str(plaintext: str) -> str:
    if not plaintext:
        return ""
    data = plaintext.encode("utf-8")
    enc_key, mac_key = _subkeys(_master_key())
    nonce = secrets.token_bytes(_NONCE)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt_str(token: str) -> str:
    if not is_encrypted(token):
        return token or ""             # plaintext passthrough (migration)
    try:
        raw = base64.urlsafe_b64decode(token[len(_PREFIX):].encode("ascii"))
        nonce, ct, tag = raw[:_NONCE], raw[_NONCE:-_TAG], raw[-_TAG:]
        enc_key, mac_key = _subkeys(_master_key())
        if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + ct,
                                                 hashlib.sha256).digest()):
            return ""                  # tampered or wrong key
        pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
        return pt.decode("utf-8", "replace")
    except Exception:
        return ""
