"""Costs — where the money actually goes, and a ceiling that holds.

The app already priced tokens per engine, but the tally lived in memory: it
reset on every restart, so a monthly cap could never mean anything. And it was
grouped by *engine*, which tells you Claude cost $4 without telling you that
the trend digest spent it while you were asleep.

This keeps a persistent ledger — month → feature → engine → tokens — so:

  • A monthly ceiling survives restarts and can genuinely block.
  • You can see which FEATURE is expensive, not just which engine, which is
    the only view that tells you what to move onto a local model.
  • Each feature can be pinned to its own engine, so the grinding work runs
    on Ollama and only the reasoning-heavy paths reach for the cloud.

Pricing is not duplicated here — it reuses the engine price table the rest of
the app already uses, so the two can't drift apart. Local engines price at
zero, which is the honest number and also the point.
"""
from __future__ import annotations

import contextvars
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

_LOCK = threading.Lock()
_current_feature = contextvars.ContextVar("agentjo_cost_feature",
                                          default="chat")

# features that can be pinned to their own engine
KNOWN_FEATURES = ["chat", "trends", "crew", "jobs", "pipelines", "watchers",
                  "selfimprove", "subagents", "autonomy", "summaries"]


def _path() -> Path:
    config.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    return config.AGENT_HOME / "costs.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except Exception:
        return {"months": {}, "engines": {}}


def _save(data: dict) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2), "utf-8")
    except Exception:
        pass


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class attribute:
    """Mark which feature is spending, so cost lands in the right bucket.

        with costs.attribute("trends"):
            brain.chat(...)
    """

    def __init__(self, feature: str):
        self.feature = feature or "chat"
        self._token = None

    def __enter__(self):
        self._token = _current_feature.set(self.feature)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _current_feature.reset(self._token)
        return False


def current_feature() -> str:
    return _current_feature.get()


# The price table keys on ENGINE LABELS ("Claude"), but calls are often
# recorded under the MODEL string ("claude-sonnet-4-6"), which the table
# doesn't recognise and prices at zero. Left alone, real cloud spend would
# silently register as free — the ledger would under-report exactly the thing
# it exists to show. Normalise first, and flag anything still unpriced rather
# than quietly calling it $0.
# Locality used to be guessed from the name here. It is now decided from the
# engine's endpoint (see agent/engines.py), because "qwen" served from a hosted
# API costs money and a local proxy called "claude-local" doesn't.


def normalise_engine(engine: str) -> str:
    key = (engine or "").strip().lower()
    if not key:
        return "unknown"
    if "claude" in key or "anthropic" in key or "sonnet" in key \
            or "opus" in key or "haiku" in key:
        return "Claude"
    if "deepseek" in key and "r1:" not in key:
        return "DeepSeek"
    return (engine or "").strip()


def is_probably_local(engine: str) -> bool:
    try:
        from . import engines as _eng
        return _eng.is_local(engine)
    except Exception:
        return False


def _price(engine: str):
    try:
        from . import brain as brainmod
        return brainmod.engine_price(normalise_engine(engine))
    except Exception:
        return (0.0, 0.0)


def unpriced(engine: str) -> bool:
    """True when we genuinely don't know what this engine costs — as opposed
    to knowing it's free. The difference matters for an honest total."""
    try:
        from . import engines as _eng
        if _eng.billable(engine) is False:
            return False                 # known local: free, not unknown
    except Exception:
        pass
    # A bare Ollama tag ('qwen3.6:latest') is unambiguously local and
    # therefore free, even when no engine of that name is registered — the
    # ledger records what RAN, and a tag only ever runs locally.
    try:
        from . import brain as _b
        if _b.looks_local_tag(engine):
            return False
    except Exception:
        pass
    p_in, p_out = _price(engine)
    return p_in == 0.0 and p_out == 0.0


def gone(engine: str) -> bool:
    """Recorded in the ledger, but no engine of that name exists now.

    Different from "we don't know the price": the spend is real and
    historical, and no price can be set for something that isn't there.
    Saying "no price set" invites you to go and set one."""
    try:
        from . import engines as _eng, brain as _b
        if _eng.classify(engine)["kind"] != "unknown":
            return False
        if _b.looks_local_tag(engine):
            return False
        return engine.lower() not in ("claude", "auto", "ollama")
    except Exception:
        return False


def cost_of(engine: str, usage: dict) -> float:
    """USD for one call's tokens, using the app's existing price table."""
    p_in, p_out = _price(engine)
    u = usage or {}
    return (u.get("in", 0) * p_in
            + u.get("cache_write", 0) * p_in * 1.25
            + u.get("cache_read", 0) * p_in * 0.10
            + u.get("out", 0) * p_out) / 1_000_000


def record(engine: str, usage: dict, feature: str = "") -> float:
    """Add one call to the ledger. Returns what it cost."""
    if not usage:
        return 0.0
    feature = feature or current_feature()
    spend = cost_of(engine, usage)
    with _LOCK:
        data = _load()
        m = data.setdefault("months", {}).setdefault(_month(), {})
        f = m.setdefault(feature, {})
        e = f.setdefault(engine or "unknown",
                         {"in": 0, "out": 0, "cache_write": 0,
                          "cache_read": 0, "calls": 0, "usd": 0.0})
        for k in ("in", "out", "cache_write", "cache_read"):
            e[k] = e.get(k, 0) + int(usage.get(k, 0) or 0)
        e["calls"] = e.get("calls", 0) + 1
        e["usd"] = round(e.get("usd", 0.0) + spend, 6)
        # a per-day roll-up: monthly totals alone can't show a trend, and a
        # trend is what tells you something changed
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = data.setdefault("days", {}).setdefault(day,
                                                   {"tokens": 0, "usd": 0.0,
                                                    "calls": 0})
        d["tokens"] += int(usage.get("in", 0) or 0) + int(
            usage.get("out", 0) or 0)
        d["usd"] = round(d["usd"] + spend, 6)
        d["calls"] += 1
        if len(data["days"]) > 120:          # keep it bounded
            for k in sorted(data["days"])[:-120]:
                data["days"].pop(k, None)
        if unpriced(engine):
            e["unpriced"] = True
        _save(data)
    return spend


def month_report(month: str = "") -> dict:
    month = month or _month()
    data = _load().get("months", {}).get(month, {})
    by_feature, by_engine, total = {}, {}, 0.0
    tok_in = tok_out = cache = calls = 0
    for feature, engines in data.items():
        f_total = 0.0
        f_tokens = 0
        for engine, e in engines.items():
            usd = float(e.get("usd", 0.0))
            f_total += usd
            _i = int(e.get("in", 0)); _o = int(e.get("out", 0))
            _c = int(e.get("cache_read", 0)) + int(e.get("cache_write", 0))
            tok_in += _i; tok_out += _o; cache += _c
            calls += int(e.get("calls", 0))
            f_tokens += _i + _o
            be = by_engine.setdefault(engine, {"usd": 0.0, "calls": 0})
            be["usd"] = round(be["usd"] + usd, 6)
            be["calls"] += int(e.get("calls", 0))
        by_feature[feature] = {"usd": round(f_total, 6),
                               "tokens": f_tokens,
                               "calls": sum(int(e.get("calls", 0))
                                            for e in engines.values())}
        total += f_total
    unknown = sorted({eng for f in data.values()
                      for eng, e in f.items() if e.get("unpriced")})
    return {"month": month, "total_usd": round(total, 4),
            "tokens": {"in": tok_in, "out": tok_out, "cache": cache,
                       "total": tok_in + tok_out},
            "calls": calls,
            "unpriced_engines": unknown,
            "by_feature": dict(sorted(by_feature.items(),
                                      key=lambda kv: -kv[1]["usd"])),
            "by_engine": dict(sorted(by_engine.items(),
                                     key=lambda kv: -kv[1]["usd"]))}


def check(cap: float | None = None) -> dict:
    """Is the ceiling reached? cap<=0 means no ceiling."""
    cap = float(cap if cap is not None
                else getattr(config, "BUDGET_USD", 0) or 0)
    spent = month_report()["total_usd"]
    if cap <= 0:
        return {"cap": 0.0, "spent": spent, "blocked": False, "pct": 0.0,
                "detail": "No monthly ceiling set."}
    pct = 100.0 * spent / cap if cap else 0.0
    blocked = spent >= cap
    return {"cap": round(cap, 2), "spent": spent, "blocked": blocked,
            "pct": round(pct, 1),
            "detail": (f"${spent:.2f} of ${cap:.2f} this month"
                       + (" — ceiling reached, paid engines are blocked."
                          if blocked else f" ({pct:.0f}%)."))}


# --------------------------------------------------------------------------- #
#  per-feature engine defaults
# --------------------------------------------------------------------------- #
def engine_map() -> dict:
    return _load().get("engines", {}) or {}


def engine_for(feature: str, fallback: str = "Auto") -> str:
    return (engine_map().get(feature) or fallback).strip() or fallback


def set_engine(feature: str, engine: str) -> dict:
    with _LOCK:
        data = _load()
        engines = data.setdefault("engines", {})
        if not engine or engine.strip().lower() in ("", "auto", "default"):
            engines.pop(feature, None)
        else:
            engines[feature] = engine.strip()
        _save(data)
    return engine_map()


def reset_month(month: str = "") -> dict:
    month = month or _month()
    with _LOCK:
        data = _load()
        data.get("months", {}).pop(month, None)
        _save(data)
    return {"ok": True, "month": month}


def daily(days: int = 14) -> list:
    """Most recent N days, oldest first, with gaps filled so a sparkline
    shows a real timeline rather than only the days that had traffic."""
    from datetime import timedelta
    data = _load().get("days", {}) or {}
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        d = data.get(key) or {}
        out.append({"day": key, "tokens": int(d.get("tokens", 0)),
                    "usd": round(float(d.get("usd", 0.0)), 6),
                    "calls": int(d.get("calls", 0))})
    return out
