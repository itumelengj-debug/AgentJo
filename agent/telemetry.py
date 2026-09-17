"""Per-engine performance telemetry — latency, throughput, and reliability —
persisted to ``AGENT_HOME/engine_stats.json``.

The point: don't trust static "smartness" numbers forever. As you actually use
each engine, record how fast it responds, how many tokens it generates per
second, and whether it tends to finish your turns or get escalated away from —
then nudge its capability score up or down accordingly, so the smart-escalation
ladder reflects which engines really serve *your* work best over time.

Dependency-free (stdlib only) and thread-safe; failures are swallowed so
telemetry can never break a turn.
"""
from __future__ import annotations

import json
import threading
import time

from . import config

_LOCK = threading.RLock()
_CACHE: dict | None = None
_LAST_SAVE = 0.0
_MIN_CALLS = 3                    # don't adapt a score until we've seen this many


def _path():
    return config.AGENT_HOME / "engine_stats.json"


def _blank() -> dict:
    return {"calls": 0, "latency": 0.0, "out_tokens": 0,
            "finishes": 0, "struggles": 0, "last": 0.0}


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            data = json.loads(_path().read_text("utf-8"))
            _CACHE = data if isinstance(data, dict) else {}
        except Exception:
            _CACHE = {}
    return _CACHE


def _save(force: bool = False) -> None:
    # caller holds the lock
    global _LAST_SAVE
    now = time.time()
    if not force and now - _LAST_SAVE < 5:
        return
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(_CACHE), "utf-8")
        _LAST_SAVE = now
    except Exception:
        pass


def record_call(engine: str, latency_s: float, out_tokens: int) -> None:
    """One model call finished: how long it took and how many tokens it made."""
    if not engine:
        return
    try:
        with _LOCK:
            e = _load().setdefault(engine, _blank())
            e["calls"] += 1
            e["latency"] += max(0.0, float(latency_s or 0.0))
            e["out_tokens"] += max(0, int(out_tokens or 0))
            e["last"] = time.time()
            _save()
    except Exception:
        pass


def record_turn(finisher: str, struggled=()) -> None:
    """A turn ended: the engine that delivered the answer gets a 'finish'; any
    engine the turn had to escalate away from gets a 'struggle'."""
    try:
        with _LOCK:
            c = _load()
            if finisher:
                c.setdefault(finisher, _blank())["finishes"] += 1
            for s in (struggled or ()):
                if s and s != finisher:
                    c.setdefault(s, _blank())["struggles"] += 1
            _save(force=True)
    except Exception:
        pass


def _derived(e: dict) -> dict:
    calls = e.get("calls", 0)
    lat = e.get("latency", 0.0)
    out_tok = e.get("out_tokens", 0)
    fin, strug = e.get("finishes", 0), e.get("struggles", 0)
    rel = (fin / (fin + strug)) if (fin + strug) else None
    return {
        "calls": calls,
        "avg_latency": round(lat / calls, 2) if calls else 0.0,
        "tokens_per_sec": round(out_tok / lat, 1) if lat > 0 else 0.0,
        "reliability": round(rel, 2) if rel is not None else None,
        "finishes": fin, "struggles": strug,
    }


def stats() -> dict:
    """Per-engine derived metrics for display and scoring."""
    try:
        with _LOCK:
            return {label: _derived(e) for label, e in _load().items()}
    except Exception:
        return {}


def adjustment(engine: str) -> int:
    """How much observed behaviour should move this engine's score (±), or 0 with
    too little data. Reliability dominates (a proxy for 'smart enough for my
    tasks'); response speed / throughput is a smaller modifier — so a fast but
    unreliable engine won't outrank a slower capable one, but among comparable
    engines the faster, more dependable one rises."""
    try:
        s = stats().get(engine)
        if not s or s["calls"] < _MIN_CALLS:
            return 0
        adj = 0.0
        if s["reliability"] is not None:               # 0.75 = neutral
            adj += max(-18.0, min(8.0, (s["reliability"] - 0.75) * 24.0))
        tps = s["tokens_per_sec"]
        if tps:                                        # 25 tok/s = neutral
            adj += max(-6.0, min(6.0, (tps - 25.0) / 25.0 * 6.0))
        return int(round(adj))
    except Exception:
        return 0


def reset() -> None:
    global _CACHE
    try:
        with _LOCK:
            _CACHE = {}
            _save(force=True)
    except Exception:
        pass
