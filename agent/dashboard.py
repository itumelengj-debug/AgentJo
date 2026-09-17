"""Dashboard — what needs you, and what's running.

The old strip counted memories, skills, chats and documents. Those numbers go
up on their own and never ask anything of you; watching them tells you nothing
you'd act on.

Meanwhile this app deliberately holds work at human gates — a self-improvement
proposal waiting for approval, trends drafted but not adopted, job applications
held back because a draft made a claim the profile couldn't support, a tripped
breaker, a backup that hasn't run in a fortnight. Every one of those was
invisible unless you opened the right panel and remembered to look.

So the dashboard answers two questions, in this order:

    NEEDS YOU     things blocked on a human decision, each with where to go
    RUNNING       engine, spend against the ceiling, next scheduled job

Counts still appear, but underneath, as context rather than headline.

Every gatherer is wrapped: one broken feature must degrade its own row, not
blank the dashboard. Nothing here does network work — this refreshes while you
type, so it has to stay cheap.
"""
from __future__ import annotations

import time
import json
import re
from datetime import datetime, timezone

from . import config

# severity drives ordering and colour: act > review > note
ACT, REVIEW, NOTE = "act", "review", "note"


def _item(severity, title, detail, panel, why=""):
    return {"severity": severity, "title": title, "detail": detail[:200],
            "panel": panel, "why": why[:160]}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _selfimprove_items() -> list:
    from . import selfimprove
    p = selfimprove.proposal()
    if p.get("state") == "proposed" and p.get("tests_ok"):
        n = len(p.get("files") or [])
        return [_item(ACT, "Code change ready to apply",
                      f"{n} file(s) passed the test suite and are waiting for "
                      f"your approval.", "selfBtn",
                      "Only you can apply a self-improvement.")]
    return []


def _trend_items() -> list:
    from . import trendscout
    rep = trendscout.report()
    pending = [t for t in (rep.get("trends") or []) if not t.get("adopted")]
    if pending:
        return [_item(REVIEW, "Trends waiting on you",
                      f"{len(pending)} trend(s) drafted a skill you haven't "
                      f"adopted or dismissed.", "trendsBtn")]
    return []


def _job_items() -> list:
    from . import jobscout
    out = []
    # Read the same source the Held drafts tab reads, or the two disagree:
    # dismissing a claim doesn't change the draft's check, so counting failed
    # checks kept reporting "needs checking" for drafts whose every claim had
    # already been decided — and told the user to confirm claims that were no
    # longer there to confirm.
    hc = jobscout.held_claims()
    to_decide = hc.get("distinct", 0)
    to_rewrite = len(hc.get("needs_redraft") or [])
    if to_decide and not hc.get("profile_empty"):
        out.append(_item(ACT, "Applications need checking",
                         f"{to_decide} claim(s) across "
                         f"{hc.get('count', 0)} draft(s) aren't backed by "
                         f"your profile.", "jobsBtn",
                         "Jobs \u2192 Held drafts: confirm the true ones and "
                         "those drafts clear immediately."))
    if hc.get("profile_empty") and hc.get("count"):
        # the cause is one missing thing, not N claims to approve
        out.append(_item(ACT, "Your job profile is empty",
                         f"{hc['count']} draft(s) are held because there's "
                         f"nothing to check them against.", "jobsBtn",
                         "Ask in chat: \u201cbuild my job profile from my "
                         "CV\u201d. Everything unblocks at once."))
    elif not to_decide and not to_rewrite and hc.get("count"):
        # a draft can be held by a problem that names no specific term, and
        # counting only named claims made those invisible — the same fault
        # this card exists to prevent
        out.append(_item(ACT, "Applications need checking",
                         f"{hc['count']} draft(s) are held and won't be "
                         f"sent.", "jobsBtn",
                         "Jobs \u2192 Held drafts shows what each one says."))
    if to_rewrite:
        out.append(_item(ACT, "Drafts to rewrite",
                         f"{to_rewrite} draft(s) still say something you "
                         f"dismissed.", "jobsBtn",
                         "Confirming won't help these \u2014 Jobs \u2192 "
                         "Held drafts has a Redraft button for them."))
    fu = jobscout.follow_ups()
    if fu:
        out.append(_item(REVIEW, "Applications gone quiet",
                         f"{len(fu)} sent over a week ago with no reply.",
                         "jobsBtn"))
    ready = [r for r in jobscout.roles() if r.get("stage") == "found"
             and (r.get("fit") or {}).get("score") is None]
    if ready:
        out.append(_item(NOTE, "Roles not yet scored",
                         f"{len(ready)} role(s) recorded but unassessed.",
                         "jobsBtn"))
    return out


def _breaker_items() -> list:
    from . import breaker
    open_ = [b for b in breaker.status() if b["state"] == "open"]
    if open_:
        b = open_[0]
        return [_item(ACT, "A feature switched itself off",
                      f"{b['feature']}: {b.get('last_error', '')}",
                      "healthBtn",
                      f"Retries by itself in ~{b.get('retry_in_min', '?')} "
                      f"min.")]
    return []


def _backup_items() -> list:
    from . import backup
    bs = backup.list_backups()
    if not bs:
        return [_item(ACT, "No backup exists",
                      "Everything the agent has learned is on this one disk.",
                      "backupBtn", "One click, then download it somewhere "
                                   "else.")]
    try:
        newest = (config.AGENT_HOME / "backups" / bs[0]["name"]).stat().st_mtime
        days = int((time.time() - newest) / 86400)
        if days > 7:
            return [_item(REVIEW, "Backup is stale",
                          f"Newest is {days} days old.", "backupBtn",
                          "Turn on the nightly backup so it can't drift.")]
    except Exception:
        pass
    return []


def _watcher_items() -> list:
    from . import watchers
    log = watchers.recent_log(30) or []
    bad = [e for e in log if "needs a look" in (e.get("summary") or "")
           or "could not self-heal" in (e.get("summary") or "")]
    if bad:
        return [_item(REVIEW, "A watcher stopped matching",
                      f"{len(bad)} run(s) extracted nothing — the site "
                      f"probably changed.", "schedBtn")]
    return []


def _issue_items() -> list:
    from . import issues
    errs = issues.recent_errors(20) or []
    recent = [e for e in errs
              if (time.time() - float(e.get("ts", 0) or 0)) < 86400]
    if len(recent) >= 3:
        return [_item(REVIEW, "Errors piling up",
                      f"{len(recent)} logged in the last 24 hours.",
                      "issuesBtn",
                      "Copy the report and paste it into Claude.")]
    return []


def _watchdog_items() -> list:
    """Features that are on, were working, and have gone quiet.

    Every fault this app has had in real use was found by the person using
    it, not by the app. Health answers "is this reachable now"; this answers
    "has this produced anything lately", which is the question that would
    have caught all of them."""
    from . import watchdog
    out = []
    for q in (watchdog.report(_MEMORY.get("m")).get("quiet") or [])[:3]:
        out.append(_item(ACT, f"{q['feature']} has gone quiet",
                         f"No result for {q['days_quiet']:.0f} days, though "
                         f"{q['why_on']}.", "healthBtn",
                         f"It last worked on {q['last_success']}. Nothing is "
                         f"erroring — it simply isn't producing."))
    return out


_MEMORY = {}


def _capability_items() -> list:
    from . import capabilities
    st = capabilities.status()
    nxt = st.get("next_test")
    if nxt and st["counts"]["used"] < st["total"]:
        n = st["total"] - st["counts"]["used"]
        return [_item(NOTE, f"{n} capabilities never used here",
                      f"Next: {nxt['name']}.", "capsBtn", nxt.get("test", ""))]
    return []


GATHERERS = [_selfimprove_items, _trend_items, _job_items, _breaker_items,
             _backup_items, _watcher_items, _issue_items, _watchdog_items,
             _capability_items]


def _running(memory) -> dict:
    """Cheap live status — no network calls, this refreshes often."""
    out = {}
    from . import costs
    b = _safe(costs.check, {}) or {}
    out["spend"] = {"usd": b.get("spent", 0.0), "cap": b.get("cap", 0.0),
                    "pct": b.get("pct", 0.0), "blocked": b.get("blocked",
                                                               False)}
    top = _safe(lambda: next(iter(costs.month_report()["by_feature"]), None))
    out["top_spender"] = top
    if memory is not None:
        def _next_job():
            rows = memory.conn.execute(
                "SELECT name, next_run FROM schedules WHERE enabled = 1 "
                "AND next_run IS NOT NULL ORDER BY next_run LIMIT 1"
            ).fetchone()
            if not rows:
                return None
            mins = int((rows["next_run"] - time.time()) / 60)
            return {"name": rows["name"],
                    "in_minutes": mins,
                    "overdue": mins < -60}
        out["next_job"] = _safe(_next_job)
        out["active_schedules"] = _safe(lambda: memory.conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE enabled = 1"
        ).fetchone()[0], 0)
    out["engine"] = getattr(config, "DEFAULT_ENGINE", "Auto")
    out["privacy"] = getattr(config, "PRIVACY_MODE", "off")
    return out


_ORDER = {ACT: 0, REVIEW: 1, NOTE: 2}


def report(memory=None) -> dict:
    _MEMORY["m"] = memory
    items = []
    for fn in GATHERERS:
        items.extend(_safe(fn, []) or [])
    items.sort(key=lambda i: _ORDER.get(i["severity"], 9))
    counts = {ACT: 0, REVIEW: 0, NOTE: 0}
    for i in items:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return {"at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "needs_you": items,
            "counts": counts,
            "all_clear": not any(i["severity"] in (ACT, REVIEW)
                                 for i in items),
            "running": _running(memory),
            # never let a tile blank the whole board
            "tiles": _safe(lambda: _tiles_persisted(memory), []) or [],
            "all_tiles": _safe(lambda: [
                {"key": x["key"], "label": x["label"]}
                for x in tiles(memory)], []) or [],
            "prefs": _safe(prefs, {"hidden": [], "order": []})}


# --------------------------------------------------------------------------- #
#  tiles — the numbers worth a glance, each with something to compare against
# --------------------------------------------------------------------------- #
def _compact(n) -> str:
    """1_250_000 -> 1.25M. A raw seven-digit number is unreadable at tile size."""
    try:
        n = float(n)
    except Exception:
        return "0"
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            v = n / cut
            return f"{v:.2f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _key(label: str) -> str:
    """A stable id for a tile. The label is what people read and may be
    reworded; history and preferences hang off this instead, so renaming a
    tile doesn't silently orphan its history."""
    return re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:40]


def _tile(label, value, sub="", tone="", progress=None, spark=None,
          panel="", title="", num=None):
    t = {"key": _key(label), "label": label, "value": value, "sub": sub,
         "tone": tone, "panel": panel, "title": title}
    if progress is not None:
        t["progress"] = max(0.0, min(100.0, float(progress)))
    if spark:
        t["spark"] = spark
    if num is not None:
        try:
            t["num"] = float(num)
        except (TypeError, ValueError):
            pass
    return t


def tiles(memory=None) -> list:
    """A number on its own is trivia. Each tile carries the thing that makes
    it mean something: a split, a share, a cap, or a fortnight of history."""
    out = []
    from . import costs

    rep = _safe(costs.month_report, {}) or {}
    tok = rep.get("tokens") or {}
    series = _safe(lambda: costs.daily(14), []) or []
    spark = [d["tokens"] for d in series]
    total = int(tok.get("total", 0))
    out.append(_tile(
        "Tokens this month", _compact(total),
        sub=f"{_compact(tok.get('in', 0))} in \u00b7 "
            f"{_compact(tok.get('out', 0))} out"
            + (f" \u00b7 {_compact(tok.get('cache', 0))} cached"
               if tok.get("cache") else ""),
        num=total,
        spark=spark if any(spark) else None,
        title="Input, output and cached tokens across every engine this month."))

    today = series[-1]["tokens"] if series else 0
    week = sum(d["tokens"] for d in series[-7:])
    out.append(_tile("Tokens today", _compact(today), num=today,
                     sub=f"{_compact(week)} over 7 days",
                     title="Today's usage against the last week."))

    budget = _safe(costs.check, {}) or {}
    cap = float(budget.get("cap", 0) or 0)
    spent = float(budget.get("spent", 0) or 0)
    if cap > 0:
        out.append(_tile("Spend", f"${spent:.2f}", num=spent,
                         sub=f"of ${cap:.2f} this month",
                         tone="bad" if budget.get("blocked")
                         else "warn" if budget.get("pct", 0) >= 80 else "good",
                         progress=budget.get("pct", 0),
                         title=budget.get("detail", "")))
    else:
        out.append(_tile("Spend this month", f"${spent:.2f}",
                         "no ceiling set", panel="settingsBtn", num=spent,
                         title="Set a monthly ceiling in Settings and this "
                               "becomes a limit rather than a running total."))

    by_feat = rep.get("by_feature") or {}
    if by_feat:
        top = next(iter(by_feat))
        share = (100.0 * (by_feat[top].get("tokens") or 0) / total
                 if total else 0)
        out.append(_tile("Biggest spender", top,
                         f"${by_feat[top]['usd']:.2f} \u00b7 "
                         f"{share:.0f}% of tokens",
                         progress=share, panel="capsBtn",
                         title="Move this one to a local engine first."))
    out.append(_tile("Requests", _compact(rep.get("calls", 0)),
                     "engine calls this month",
                     num=rep.get("calls", 0)))

    # --- what the agent has accumulated -------------------------------
    if memory is not None:
        _mem = _safe(memory.memory_count, 0) or 0
        out.append(_tile("Memories", _compact(_mem), "long-term facts",
                         panel="memoryBtn", num=_mem))
        _sk = _safe(memory.skill_count, 0) or 0
        out.append(_tile("Skills", _compact(_sk), "taught procedures",
                         panel="memoryBtn", num=_sk))
        _docs = _safe(lambda: __import__("agent.rag", fromlist=["rag"])
                      .get_store().doc_count(), 0) or 0
        out.append(_tile("Documents", _compact(_docs), "indexed for search",
                         panel="documentsBtn", num=_docs))
        learned = (_safe(memory.playbook_count, 0) or 0) + \
                  (_safe(memory.lesson_count, 0) or 0)
        out.append(_tile("Learned", _compact(learned), num=learned,
                         sub="playbooks and lessons from its own work",
                         title="Distilled by the agent from completed and "
                               "blocked tasks."))
        tasks = _safe(lambda: len(memory.active_tasks()), 0) or 0
        if tasks:
            out.append(_tile("Open tasks", str(tasks), "still in progress",
                             panel="autonomyBtn"))
        sched = _safe(lambda: memory.conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE enabled = 1"
        ).fetchone()[0], 0)
        out.append(_tile("Scheduled jobs", str(sched), "running unattended",
                         panel="schedBtn", num=sched))

    # --- turbo, if it's on: is it actually paying off? ------------------
    if getattr(config, "TURBO", False):
        from . import turbo as _tb
        st = _safe(_tb.stats, {}) or {}
        if st.get("turns"):
            sub = (f"{st['local_only']} of {st['turns']} attempted turns "
                   f"answered locally")
            if st.get("net_usd"):
                sub += f" · saved ${st['net_usd']:.2f}"
            out.append(_tile(
                "Turbo hit rate", f"{st['hit_rate']:.0f}%",
                num=st["hit_rate"],
                sub=sub,
                progress=st["hit_rate"],
                tone="good" if st["hit_rate"] >= 60
                else "warn" if st["hit_rate"] >= 35 else "bad",
                title=st.get("advice", "")))

    # --- how much of the app is actually proven here -------------------
    from . import capabilities
    st = _safe(capabilities.status, {}) or {}
    if st:
        used = st["counts"]["used"]
        tot = st["total"]
        out.append(_tile("Capabilities used", f"{used}/{tot}", num=used,
                         sub="exercised on this machine",
                         progress=100.0 * used / tot if tot else 0,
                         tone="warn" if used < tot / 2 else "good",
                         panel="capsBtn",
                         title="Anything unused is an untested assumption."))
    return out


# --------------------------------------------------------------------------- #
#  persistence — history for every tile, and a snapshot to paint immediately
# --------------------------------------------------------------------------- #
HISTORY_DAYS = 30


def _hist_path():
    config.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    return config.AGENT_HOME / "dashboard_history.json"


def _snap_path():
    config.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    return config.AGENT_HOME / "dashboard_last.json"


def _prefs_path():
    config.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    return config.AGENT_HOME / "dashboard_prefs.json"


def _read_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _write_json(path, data):
    try:
        path.write_text(json.dumps(data), "utf-8")
    except Exception:
        pass


def history() -> dict:
    return _read_json(_hist_path(), {}) or {}


def record_history(tiles_list: list) -> dict:
    """One value per tile per day. Counters only move a little day to day, so
    a daily point is the right resolution — and it keeps the file small
    enough to read on every dashboard refresh."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = history()
    for t in tiles_list:
        if "num" not in t:
            continue                      # a name, not a measurement
        series = data.setdefault(t["key"], {})
        series[day] = t["num"]            # last value of the day wins
        if len(series) > HISTORY_DAYS:
            for old_day in sorted(series)[:-HISTORY_DAYS]:
                series.pop(old_day, None)
    _write_json(_hist_path(), data)
    return data


def _spark_for(key: str, data: dict, days: int = 14) -> list:
    """Fill gaps so the line is a timeline, not just the days with traffic.
    A day with no reading carries the previous value forward — a counter that
    wasn't observed didn't drop to zero."""
    from datetime import timedelta
    series = data.get(key) or {}
    if len(series) < 2:
        return []
    today = datetime.now(timezone.utc).date()
    out, last = [], None
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in series:
            last = series[d]
        out.append(float(last) if last is not None else 0.0)
    return out if any(out) else []


def prefs() -> dict:
    p = _read_json(_prefs_path(), {}) or {}
    return {"hidden": list(p.get("hidden") or []),
            "order": list(p.get("order") or [])}


def save_prefs(hidden=None, order=None) -> dict:
    p = prefs()
    if hidden is not None:
        p["hidden"] = [str(k) for k in hidden]
    if order is not None:
        p["order"] = [str(k) for k in order]
    _write_json(_prefs_path(), p)
    return p


def _apply_prefs(tiles_list: list) -> list:
    """Hide what was hidden, order what was ordered — and put anything the
    user has never seen at the end rather than dropping it, so a new tile
    still appears for someone with saved preferences."""
    p = prefs()
    hidden = set(p["hidden"])
    order = p["order"]
    visible = [t for t in tiles_list if t["key"] not in hidden]
    if not order:
        return visible
    rank = {k: i for i, k in enumerate(order)}
    return sorted(visible, key=lambda t: rank.get(t["key"], 10_000))


def last_snapshot() -> dict:
    return _read_json(_snap_path(), {}) or {}


def _tiles_persisted(memory=None) -> list:
    """Build the tiles, remember today's numbers, hand every tile whatever
    history it has, then apply the user's show/hide and order."""
    built = tiles(memory)
    data = _safe(lambda: record_history(built), None) or history()
    for t in built:
        if not t.get("spark"):
            s = _safe(lambda k=t["key"]: _spark_for(k, data), [])
            if s:
                t["spark"] = s
    return _apply_prefs(built)


def save_snapshot(rep: dict) -> None:
    """Keep the last board so the next launch can paint instantly instead of
    showing an empty pane while the first request is in flight."""
    _write_json(_snap_path(), {"at": rep.get("at"),
                               "tiles": rep.get("tiles", []),
                               "running": rep.get("running", {}),
                               "needs_you": rep.get("needs_you", []),
                               "all_clear": rep.get("all_clear", True)})
