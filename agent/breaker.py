"""Circuit breakers — a feature that keeps failing should stop, not keep trying.

The motivating case is in this app's own error log: the trend scan failed four
times in a row, and each attempt fetched sources and called a paid engine
before dying at the same point. Nobody was watching, so it just kept costing
money to fail identically.

A breaker sits in front of anything scheduled or expensive:

    CLOSED     normal. Failures are counted.
    OPEN       too many consecutive failures — calls are refused immediately,
               cheaply, with the ORIGINAL error preserved so the reason is
               still visible days later.
    HALF-OPEN  after the cooldown, exactly one trial call is allowed. Success
               closes the breaker and clears the count; failure re-opens it
               with a longer cooldown (doubling, capped), so a persistently
               broken thing backs off instead of hammering.

Two deliberate choices:

  Consecutive, not cumulative. A feature that fails once a week isn't broken;
  one that fails three times running is. Any success resets the count.

  Refusing is not silent. An open breaker is a first-class thing you can see:
  it shows on the Health board with the error that opened it and how long
  until it retries, and both opening and closing are audited. A breaker that
  quietly disables a feature would be worse than the bleeding it prevents.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

THRESHOLD = 3            # consecutive failures before opening
BASE_COOLDOWN = 1800     # 30 minutes, then doubling
MAX_COOLDOWN = 86400     # never wait more than a day before a trial
_LOCK = threading.Lock()


def _path() -> Path:
    d = config.AGENT_HOME
    d.mkdir(parents=True, exist_ok=True)
    return d / "breakers.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2), "utf-8")
    except Exception:
        pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def state(feature: str) -> str:
    """closed | open | half-open"""
    b = _load().get(feature) or {}
    if not b.get("opened_at"):
        return "closed"
    if time.time() >= b["opened_at"] + b.get("cooldown", BASE_COOLDOWN):
        return "half-open"
    return "open"


def allow(feature: str) -> tuple[bool, str]:
    """Ask before doing expensive work. Returns (allowed, reason_if_not)."""
    st = state(feature)
    if st != "open":
        return True, ""
    b = _load().get(feature) or {}
    retry_at = b["opened_at"] + b.get("cooldown", BASE_COOLDOWN)
    mins = max(1, int((retry_at - time.time()) / 60))
    return False, (
        f"'{feature}' is switched off after {b.get('failures', 0)} failures "
        f"in a row. Last error: {b.get('last_error', 'unknown')}. It will try "
        f"again by itself in about {mins} minute(s), or reset it from the "
        f"Health panel once the cause is fixed.")


def record_success(feature: str) -> dict:
    with _LOCK:
        data = _load()
        b = data.get(feature) or {}
        was_open = bool(b.get("opened_at"))
        data[feature] = {"failures": 0, "opened_at": None,
                         "cooldown": BASE_COOLDOWN,
                         "last_error": "", "last_ok": time.time(),
                         "trips": b.get("trips", 0)}
        _save(data)
    if was_open:
        _audit(feature, "closed", "recovered on a trial call")
    return {"state": "closed", "recovered": was_open}


def record_failure(feature: str, error: str = "") -> dict:
    with _LOCK:
        data = _load()
        b = data.get(feature) or {}
        was_open = bool(b.get("opened_at"))
        failures = int(b.get("failures", 0)) + 1
        cooldown = int(b.get("cooldown", BASE_COOLDOWN))
        opened = False
        if was_open:
            # this was the half-open trial and it failed too — back off harder
            cooldown = min(MAX_COOLDOWN, cooldown * 2)
            opened_at = time.time()
            opened = True
        elif failures >= THRESHOLD:
            opened_at = time.time()
            opened = True
        else:
            opened_at = None
        data[feature] = {"failures": failures, "opened_at": opened_at,
                         "cooldown": cooldown, "last_error": (error or "")[:300],
                         "last_ok": b.get("last_ok"),
                         "trips": int(b.get("trips", 0)) + (1 if opened else 0)}
        _save(data)
    if opened:
        _audit(feature, "opened",
               f"{failures} consecutive failure(s); retry in "
               f"{cooldown // 60}min. Last error: {(error or '')[:120]}")
    return {"state": "open" if opened else "closed", "failures": failures,
            "opened": opened, "cooldown": cooldown}


def reset(feature: str) -> dict:
    with _LOCK:
        data = _load()
        if feature not in data:
            return {"ok": False, "error": "no such breaker"}
        data.pop(feature, None)
        _save(data)
    _audit(feature, "reset", "cleared by hand")
    return {"ok": True}


def status() -> list:
    out = []
    for name, b in sorted(_load().items()):
        st = state(name)
        item = {"feature": name, "state": st,
                "failures": b.get("failures", 0),
                "trips": b.get("trips", 0),
                "last_error": b.get("last_error", "")}
        if b.get("opened_at"):
            retry = b["opened_at"] + b.get("cooldown", BASE_COOLDOWN)
            item["opened_at"] = _iso(b["opened_at"])
            item["retry_at"] = _iso(retry)
            item["retry_in_min"] = max(0, int((retry - time.time()) / 60))
        if b.get("last_ok"):
            item["last_ok"] = _iso(b["last_ok"])
        out.append(item)
    return out


def guard(feature: str, fn, *args, **kwargs):
    """Run fn under the breaker. fn should return a dict with 'ok', or raise.

    Returns the function's result, or a refusal dict when the breaker is open —
    which costs nothing, which is the whole point."""
    allowed, why = allow(feature)
    if not allowed:
        return {"ok": False, "error": why, "breaker_open": True}
    try:
        res = fn(*args, **kwargs)
    except Exception as exc:
        record_failure(feature, f"{type(exc).__name__}: {exc}")
        raise
    failed = isinstance(res, dict) and (
        res.get("ok") is False or res.get("error"))
    if failed:
        record_failure(feature, str(res.get("error", ""))[:280])
    else:
        record_success(feature)
    return res


def _audit(feature: str, action: str, detail: str) -> None:
    try:
        from . import audit
        audit.record("breaker", name=feature, detail=action,
                     summary=detail[:250])
    except Exception:
        pass
