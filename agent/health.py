"""Health — one screen that answers "is this actually working?"

This app now has two dozen moving parts, several of which fail *quietly*: a
stale deploy still serving old code, Ollama not running, a neural-3D command
pointing at a python that no longer exists, an audit chain broken by a bad
edit, a backup that hasn't run in three weeks. Every one of those has cost a
debugging session that started from the wrong assumption.

So each check answers three things, in this order:

    state    ok / warn / fail
    detail   what is actually true right now
    fix      the specific next action, not "check your configuration"

Rules that keep this honest:
  • Every check is wrapped — a broken check reports itself as a failure
    rather than taking the whole board down with it.
  • Nothing here does slow work. Network probes get short timeouts and only
    touch local services; the board must return in about a second or people
    stop opening it.
  • A check that cannot determine the answer says "unknown" instead of
    guessing green. A dashboard that lies is worse than no dashboard.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
BACKUP_STALE_DAYS = 7
DISK_WARN_PCT = 90


def _c(name: str, group: str, state: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "group": group, "state": state,
            "detail": detail[:300], "fix": fix[:300]}


def _safe(fn, name, group):
    try:
        return fn()
    except Exception as exc:
        return _c(name, group, FAIL, f"{type(exc).__name__}: {exc}",
                  "This check itself failed — treat the component as unknown.")


# --------------------------------------------------------------------------- #
#  deploy — the one that has misled us most often
# --------------------------------------------------------------------------- #
def _check_location():
    """Running from a temp folder is running on borrowed time.

    Unzipping and launching in place is the natural thing to do, and Windows
    clears %TEMP% whenever it likes — taking the app, the venv and anything
    stored beside it. It works perfectly until the day it doesn't."""
    import os as _os
    from pathlib import Path as _P
    here = _P(__file__).resolve().parent.parent
    s = str(here).lower()
    temp = (_os.environ.get("TEMP") or _os.environ.get("TMP") or "").lower()
    in_temp = ("\\temp\\" in s or "/tmp/" in s
               or (temp and s.startswith(temp)))
    if in_temp:
        return _c("Where it lives", "Deploy", WARN,
                  f"running from a temporary folder: {here}",
                  "Windows empties this folder without warning, and your "
                  "conversations, memories and settings go with it. Move the "
                  "whole folder somewhere permanent — C:\\AgentJo, say — and "
                  "run install.bat there.")
    return _c("Where it lives", "Deploy", OK, str(here))


def _check_engine_config():
    """An engine that can't work should say so here, not at message time."""
    try:
        from . import brain as _b
        r = _b.check_engines()
        if r["problems"]:
            return _c("Engine setup", "Engines", FAIL, r["detail"],
                      r["problems"][0]["fix"])
        return _c("Engine setup", "Engines", OK,
                  "Every saved engine has a URL, a model and a key if it "
                  "needs one.")
    except Exception as exc:
        return _c("Engine setup", "Engines", UNKNOWN, str(exc)[:160])


def _check_engine_models():
    """An engine whose model field names another engine breaks every call
    through it, with an error that blames the model."""
    try:
        from . import brain as _b
        r = _b.repair_engine_models()
        if r["confused"]:
            return _c("Engine models", "Engines", FAIL, r["detail"], r["fix"])
        return _c("Engine models", "Engines", OK,
                  "Every engine has a model id, not another engine's name.")
    except Exception as exc:
        return _c("Engine models", "Engines", UNKNOWN, str(exc)[:160])


def _check_intercepts():
    """Is the gate actually on, and is anything waiting?

    It was built and never wired in, so the panel showed an empty queue while
    every call went through. A safety feature that exists only in the UI is
    worse than none, because it gets trusted."""
    try:
        from . import intercepts, tools
        wired = "intercepts" in _src_of(tools)
        if not wired:
            return _c("Review gate", "Safety", FAIL,
                      "Nothing is being held — the gate isn't wired into the "
                      "tool path.",
                      "Anything that leaves this machine runs without review. "
                      "This is a build problem, not a setting.")
        if not intercepts.enabled():
            return _c("Review gate", "Safety", WARN,
                      "Turned off — external calls run without review.",
                      "Set INTERCEPTS on in Settings if you want mail, form "
                      "submissions and shell commands to wait for you.")
        n = intercepts.summary()["waiting"]
        return _c("Review gate", "Safety", OK,
                  f"On. {n} call(s) waiting." if n else "On, nothing waiting.",
                  "Waiting calls are in Autonomy." if n else "")
    except Exception as exc:
        return _c("Review gate", "Safety", UNKNOWN, str(exc)[:160])


def _src_of(mod) -> str:
    try:
        from pathlib import Path as _P
        return _P(mod.__file__).read_text("utf-8")
    except Exception:
        return ""


def _check_index_truncated():
    """An index that holds part of a folder answers as if it held all of it.

    Nothing else surfaces this: the search works, the answers look complete,
    and the material that was never read is invisible by definition."""
    try:
        from . import rag
        s = rag.DocumentStore()
        rows = s.folders() if hasattr(s, "folders") else []
        n = s.count() if hasattr(s, "count") else 0
        if n and n >= config.RAG_MAX_FILES:
            return _c("Document index", "Safety", WARN,
                      f"{n} file(s) indexed, at the {config.RAG_MAX_FILES} "
                      f"limit.",
                      "A folder bigger than the limit is indexed only as far "
                      "as the limit, and searches answer from that part "
                      "without saying so. Raise RAG_MAX_FILES in Settings, or "
                      "index the sub-folders that matter separately.")
        return _c("Document index", "Safety", OK,
                  f"{n} file(s) indexed, under the "
                  f"{config.RAG_MAX_FILES} limit.")
    except Exception as exc:
        return _c("Document index", "Safety", UNKNOWN, str(exc)[:160])


def _check_build():
    build = getattr(config, "BUILD_ID", "")
    if not build:
        return _c("Build", "Deploy", UNKNOWN, "No build stamp in this copy.",
                  "Deploy a current build.")
    return _c("Build", "Deploy", OK, f"Running build {build}.",
              "If this isn't the build you just deployed, the app wasn't "
              "restarted — close the console window and run "
              "start_agent_jo.bat again.")


def _check_interpreter():
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    frozen = getattr(sys, "frozen", False)
    if frozen:
        return _c("Interpreter", "Deploy", WARN,
                  f"Running a FROZEN build: {sys.executable}",
                  "Frozen builds go stale silently. Prefer running from "
                  "source with start_agent_jo.bat.")
    if not venv:
        return _c("Interpreter", "Deploy", WARN,
                  f"Not in a virtualenv: {sys.executable}",
                  "Start with start_agent_jo.bat so the app's own .venv and "
                  "its installed packages are used.")
    return _c("Interpreter", "Deploy", OK, f"venv python: {sys.executable}")


# --------------------------------------------------------------------------- #
#  engines
# --------------------------------------------------------------------------- #
def _check_cloud_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _c("Cloud engine", "Engines", OK,
                  "Anthropic API key is present.",
                  "A key being present says nothing about credit — if calls "
                  "fail with a billing error, top up or pin a local engine.")
    return _c("Cloud engine", "Engines", WARN, "No ANTHROPIC_API_KEY set.",
              "Fine if you work locally. Otherwise set the key and restart.")


def _check_ollama():
    base = "http://localhost:11434"
    try:
        import httpx
        r = httpx.get(base + "/api/tags", timeout=1.5)
        models = [m.get("name") for m in r.json().get("models", [])]
        if not models:
            return _c("Ollama", "Engines", WARN,
                      "Ollama is running but has no models pulled.",
                      "Run:  ollama pull qwen3.6")
        want = getattr(config, "OLLAMA_MODEL", "")
        if want and not any(m == want or m.startswith(want.split(":")[0])
                            for m in models):
            # Telling someone to download a model when they have twelve
            # installed is the wrong first suggestion. Offer the closest one
            # they already have; the download is the fallback.
            near = ""
            try:
                from . import modelbuild as _mbb  # noqa: F401
                import difflib as _dl
                stem = want.split(":")[0].lower()
                same = [m for m in models
                        if m.split(":")[0].lower() == stem]
                near = (same[0] if same else
                        (_dl.get_close_matches(want, models, n=1,
                                               cutoff=0.4) or [""])[0])
            except Exception:
                near = models[0] if models else ""
            return _c("Ollama", "Engines", WARN,
                      f"Running, but the configured model '{want}' isn't "
                      f"installed. Have: {', '.join(models[:4])}",
                      (f"Set OLLAMA_MODEL to '{near}' in Settings — you "
                       f"already have it. Or run:  ollama pull {want}"
                       if near else f"Run:  ollama pull {want}"))
        return _c("Ollama", "Engines", OK,
                  f"Running with {len(models)} model(s): "
                  f"{', '.join(models[:3])}")
    except Exception:
        return _c("Ollama", "Engines", WARN, "Not reachable on port 11434.",
                  "Start Ollama if you want local engines. Everything else "
                  "still works without it.")


def _check_voice():
    try:
        from . import voice
        reason = voice.stt_reason()
        if not reason:
            return _c("Voice input", "Engines", OK, "Speech-to-text ready.")
        return _c("Voice input", "Engines", WARN, reason[:200],
                  "Run:  pip install faster-whisper   (in the app's .venv), "
                  "then restart.")
    except Exception as exc:
        return _c("Voice input", "Engines", UNKNOWN, str(exc)[:200])


# --------------------------------------------------------------------------- #
#  external tools
# --------------------------------------------------------------------------- #
def _check_blender():
    try:
        from . import blenderlab
        exe = blenderlab.find_blender()
        if exe:
            return _c("Blender", "Tools", OK, f"Found: {exe}")
        return _c("Blender", "Tools", WARN, "Not found.",
                  "Install from blender.org, or set Settings → Blender path. "
                  "Only the 3D features need it.")
    except Exception as exc:
        return _c("Blender", "Tools", UNKNOWN, str(exc)[:200])


def _check_neural3d():
    try:
        from . import neural3d
        cmd = (getattr(config, "NEURAL3D_CMD", "") or "").strip()
        if not cmd:
            return _c("Neural 3D", "Tools", WARN, "Not configured.",
                      "Settings → Neural 3D command → Detect.")
        v = neural3d.validate(cmd)
        return _c("Neural 3D", "Tools", OK if v.get("ok") else FAIL,
                  v.get("detail", ""),
                  "" if v.get("ok") else "Settings → Neural 3D command → "
                                         "Detect, then Check.")
    except Exception as exc:
        return _c("Neural 3D", "Tools", UNKNOWN, str(exc)[:200])


def _check_mcp():
    try:
        from . import mcp
        servers = mcp.manager.statuses() if hasattr(mcp, "manager") else []
        if not servers:
            return _c("MCP servers", "Tools", OK, "None configured.")
        bad = [s for s in servers if not s.get("connected")]
        if bad:
            return _c("MCP servers", "Tools", WARN,
                      f"{len(servers) - len(bad)}/{len(servers)} connected. "
                      f"Down: {', '.join(s.get('name', '?') for s in bad[:3])}",
                      "Open the ⬡ MCP panel and hit Retry on the failing "
                      "server.")
        return _c("MCP servers", "Tools", OK,
                  f"All {len(servers)} connected.")
    except Exception as exc:
        return _c("MCP servers", "Tools", UNKNOWN, str(exc)[:200])


# --------------------------------------------------------------------------- #
#  data safety
# --------------------------------------------------------------------------- #
def _check_backups():
    try:
        from . import backup
        bs = backup.list_backups()
        if not bs:
            return _c("Backups", "Safety", FAIL, "No backups exist.",
                      "🛟 Backup → Back up now → Download. Everything this "
                      "app has learned is on one disk until you do.")
        newest = bs[0]
        age_days = (time.time() - (config.AGENT_HOME / "backups" /
                                   newest["name"]).stat().st_mtime) / 86400
        if age_days > BACKUP_STALE_DAYS:
            return _c("Backups", "Safety", WARN,
                      f"Newest backup is {int(age_days)} days old "
                      f"({len(bs)} kept).",
                      "🛟 Backup → tick Nightly so this can't drift again.")
        return _c("Backups", "Safety", OK,
                  f"{len(bs)} backup(s), newest {int(age_days)} day(s) old.",
                  "Backups live beside the data they protect — keep a copy "
                  "somewhere else too.")
    except Exception as exc:
        return _c("Backups", "Safety", UNKNOWN, str(exc)[:200])


def _check_audit():
    """The chain, and — crucially — WHY it broke.

    Telling someone "something edited your audit log" when two of their own
    processes wrote at once is a false accusation. It sends them hunting for
    an intruder instead of pressing Reseal."""
    try:
        from . import audit
        v = audit.verify()
        n = v.get("entries", 0)
        if v.get("ok"):
            return _c("Audit chain", "Safety", OK,
                      f"{n} entries, chain intact.")
        where = v.get("break_at")
        cause = v.get("cause", "")
        detail = v.get("cause_detail", "")
        if cause in ("concurrent-write", "out-of-order"):
            return _c("Audit chain", "Safety", WARN,
                      f"Breaks at line {where}: {detail}.",
                      "Nothing was tampered with. This is what the old "
                      "thread-only lock allowed; the cross-process lock "
                      "prevents new ones. Use Audit \u2192 Tidy up \u2192 "
                      "Reseal to archive this trail and start a clean chain.")
        if cause == "unreadable":
            return _c("Audit chain", "Safety", FAIL,
                      f"Line {where} isn't readable: {detail}.",
                      "The file was truncated, probably by a crash mid-write. "
                      "Reseal keeps it and starts a clean chain.")
        return _c("Audit chain", "Safety", FAIL,
                  f"Breaks at line {where}."
                  + (f" {detail}." if detail else ""),
                  "This one is worth looking at: no earlier entry matches "
                  "what that line claims to follow. Restore from a backup if "
                  "the trail needs to be evidential, or Reseal to archive it "
                  "and start again.")
    except Exception as exc:
        return _c("Audit chain", "Safety", UNKNOWN, str(exc)[:200])




def _check_disk():
    try:
        usage = shutil.disk_usage(str(config.AGENT_HOME))
        pct = 100.0 * usage.used / usage.total
        free_gb = usage.free / 1e9
        state = WARN if pct >= DISK_WARN_PCT else OK
        return _c("Disk space", "Safety", state,
                  f"{pct:.0f}% used, {free_gb:.1f} GB free on the data drive.",
                  "Blender renders, neural meshes and Time Machine snapshots "
                  "are the usual culprits." if state == WARN else "")
    except Exception as exc:
        return _c("Disk space", "Safety", UNKNOWN, str(exc)[:200])


def _check_database(memory=None):
    try:
        if memory is None:
            return _c("Database", "Safety", UNKNOWN, "Store not available.")
        row = memory.conn.execute("PRAGMA quick_check").fetchone()
        result = (row[0] if row else "").lower()
        if result == "ok":
            n = memory.conn.execute(
                "SELECT COUNT(*) FROM memories").fetchone()[0]
            return _c("Database", "Safety", OK,
                      f"Integrity check passed ({n} memories).")
        return _c("Database", "Safety", FAIL, f"quick_check said: {result}",
                  "Restore from a backup — 🛟 Backup verifies before it "
                  "replaces anything.")
    except Exception as exc:
        return _c("Database", "Safety", UNKNOWN, str(exc)[:200])


# --------------------------------------------------------------------------- #
#  automation
# --------------------------------------------------------------------------- #
def _check_schedules(memory=None):
    try:
        if memory is None:
            return _c("Schedules", "Automation", UNKNOWN,
                      "Store not available.")
        rows = memory.conn.execute(
            "SELECT name, next_run, enabled FROM schedules").fetchall()
        active = [r for r in rows if r["enabled"]]
        if not active:
            return _c("Schedules", "Automation", OK,
                      f"{len(rows)} defined, none enabled.")
        now = time.time()
        overdue = [r for r in active
                   if r["next_run"] and r["next_run"] < now - 3600]
        if overdue:
            return _c("Schedules", "Automation", WARN,
                      f"{len(overdue)} schedule(s) more than an hour overdue: "
                      f"{', '.join(r['name'] for r in overdue[:3])}",
                      "The app has to be running for schedules to fire. If it "
                      "was closed, they catch up on the next start.")
        return _c("Schedules", "Automation", OK,
                  f"{len(active)} enabled, none overdue.")
    except Exception as exc:
        return _c("Schedules", "Automation", UNKNOWN, str(exc)[:200])


def _check_watchers():
    try:
        from . import watchers
        log = watchers.recent_log(30)
        bad = [e for e in log
               if "needs a look" in (e.get("summary") or "")
               or "could not self-heal" in (e.get("summary") or "")]
        if bad:
            return _c("Watchers", "Automation", WARN,
                      f"{len(bad)} watcher run(s) reported a problem; newest: "
                      f"{bad[0].get('name', '?')}",
                      "A watcher extracting nothing usually means the site "
                      "changed. Open its schedule and re-check the selectors.")
        healed = [e for e in log if "self-healed" in (e.get("summary") or "")]
        if healed:
            return _c("Watchers", "Automation", OK,
                      f"{len(log)} recent run(s); {len(healed)} self-healed "
                      f"after a site change.")
        return _c("Watchers", "Automation", OK,
                  f"{len(log)} recent run(s), no problems reported.")
    except Exception as exc:
        return _c("Watchers", "Automation", UNKNOWN, str(exc)[:200])


def _check_gone_quiet(memory=None):
    """Something switched on that has stopped producing anything."""
    try:
        from . import watchdog
        rep = watchdog.report(memory)
        quiet = rep.get("quiet") or []
        if not quiet:
            return _c("Still producing", "Automation", OK,
                      f"All {rep.get('watching', 0)} enabled feature(s) have "
                      f"produced something recently.")
        q = quiet[0]
        more = f" (+{len(quiet) - 1} more)" if len(quiet) > 1 else ""
        return _c("Still producing", "Automation", FAIL,
                  f"{q['feature']} hasn't produced anything for "
                  f"{q['days_quiet']:.0f} days{more}, though {q['why_on']}.",
                  (f"It last worked on {q['last_success']}. "
                   + (f"Why: {q['why_quiet']}"
                      if q.get("why_quiet") else
                      "Nothing is erroring, so check it actually still "
                      "runs.")))
    except Exception as exc:
        return _c("Still producing", "Automation", UNKNOWN, str(exc)[:200])


def _check_retired_models():
    """A provider retiring a model is permanent, and silent until something
    tries to use it. In the real trail a scheduled brief failed the same way
    every day for weeks."""
    try:
        from . import audit, engines
        recent = audit.recent(400) or []
        hits = {}
        for e in recent:
            blob = f"{e.get('summary','')} {e.get('detail','')}"
            if engines.is_retired(blob):
                who = e.get("name") or e.get("kind") or "a scheduled job"
                hits[who] = engines.retired_model_name(blob) or "the model"
        if not hits:
            return _c("Retired models", "Engines", OK,
                      "No engine is pointing at a retired model.")
        who = ", ".join(list(hits)[:3])
        model = list(hits.values())[0]
        return _c("Retired models", "Engines", FAIL,
                  f"{who} is calling {model}, which the provider has retired.",
                  "Pick another engine for it, or update that engine's model "
                  "under Engines — it will fail identically until you do.")
    except Exception as exc:
        return _c("Retired models", "Engines", UNKNOWN, str(exc)[:200])


def _check_engine_mix():
    try:
        from . import engines
        inv = engines.inventory()
        bits = []
        if inv["local"]:
            bits.append(f"{len(inv['local'])} local")
        if inv["cloud"]:
            bits.append(f"{len(inv['cloud'])} cloud")
        unknown = inv.get("unknown") or []
        if unknown:
            return _c("Engine mix", "Engines", WARN,
                      (", ".join(bits) or "none") + f", {len(unknown)} "
                      f"unclassified: "
                      + ", ".join(u["name"] for u in unknown[:3]),
                      "Add each one's URL under Engines — until then its cost "
                      "can't be counted and it won't be used as a fallback.")
        if inv["local"] and inv["cloud"]:
            return _c("Engine mix", "Engines", OK,
                      ", ".join(bits) + " — either can cover for the other.")
        if not inv["local"]:
            return _c("Engine mix", "Engines", OK,
                      ", ".join(bits) + " — no local engine, so nothing can "
                      "cover for the cloud.",
                      "Install Ollama for a free fallback when credit runs "
                      "out.")
        return _c("Engine mix", "Engines", OK, ", ".join(bits) or "none")
    except Exception as exc:
        return _c("Engine mix", "Engines", UNKNOWN, str(exc)[:200])


def _check_budget():
    try:
        from . import costs
        b = costs.check()
        rep = costs.month_report()
        extra = ""
        if rep.get("unpriced_engines"):
            from . import costs as _cst
            names = rep["unpriced_engines"]
            missing = [n for n in names if _cst.gone(n)]
            unknown = [n for n in names if n not in missing]
            bits = []
            if unknown:
                bits.append(", ".join(unknown[:2])
                            + " have no price set, so the total is "
                              "incomplete.")
            if missing:
                # a price can't be set for an engine that no longer exists —
                # telling someone to set one sends them looking for a field
                # that isn't there
                bits.append(", ".join(missing[:2])
                            + " ran under a name that is no longer a "
                              "configured engine, so that spend can't be "
                              "priced now. It's historical.")
            extra = " Note: " + " ".join(bits) if bits else ""
        if b["blocked"]:
            return _c("Spend", "Safety", FAIL, b["detail"] + extra,
                      "Raise the ceiling in Settings, or pin expensive "
                      "features to a local engine.")
        top = next(iter(rep["by_feature"]), None)
        detail = b["detail"] + (f" Biggest: {top} "
                                f"(${rep['by_feature'][top]['usd']:.2f})."
                                if top else "") + extra
        state = WARN if (b["cap"] and b["pct"] >= 80) else OK
        return _c("Spend", "Safety", state, detail,
                  "Approaching the ceiling — consider moving that feature to "
                  "a local engine." if state == WARN else "")
    except Exception as exc:
        return _c("Spend", "Safety", UNKNOWN, str(exc)[:200])


def _check_breakers():
    """A tripped breaker is the app telling you something is properly broken —
    it must be impossible to miss."""
    try:
        from . import breaker
        st = breaker.status()
        open_ = [b for b in st if b["state"] == "open"]
        if open_:
            b = open_[0]
            return _c("Circuit breakers", "Automation", FAIL,
                      f"{len(open_)} feature(s) switched off after repeated "
                      f"failures. {b['feature']}: {b.get('last_error', '')}",
                      f"Fix the cause, then reset it here. It retries by "
                      f"itself in ~{b.get('retry_in_min', '?')} min.")
        half = [b for b in st if b["state"] == "half-open"]
        if half:
            return _c("Circuit breakers", "Automation", WARN,
                      f"{len(half)} feature(s) waiting on a trial run after "
                      f"earlier failures.",
                      "The next scheduled run decides whether they recover.")
        return _c("Circuit breakers", "Automation", OK,
                  "Nothing tripped.")
    except Exception as exc:
        return _c("Circuit breakers", "Automation", UNKNOWN, str(exc)[:200])


def _check_evals():
    """Untested engine pinning is a guess; this says whether it was measured."""
    try:
        from . import evals, costs
        pinned = {f: e for f, e in costs.engine_map().items()}
        if not pinned:
            return _c("Engine evals", "Automation", OK,
                      "No features pinned to a specific engine.",
                      "If you pin one, run the evals to check that engine can "
                      "actually hold the feature's contract.")
        untested = []
        failing = []
        for feature, engine in pinned.items():
            last = evals.last_for(engine)
            if not last:
                untested.append(f"{feature}→{engine}")
            elif feature in (last.get("unsafe_features") or []):
                failing.append(f"{feature}→{engine}")
        if failing:
            return _c("Engine evals", "Automation", FAIL,
                      f"Pinned to an engine that failed that feature's "
                      f"contract: {', '.join(failing)}",
                      "Repin to a stronger engine, or re-run the evals if "
                      "the engine has changed.")
        if untested:
            return _c("Engine evals", "Automation", WARN,
                      f"Pinned but never measured: {', '.join(untested)}",
                      "Run the evals against that engine to confirm it can "
                      "hold the contract.")
        return _c("Engine evals", "Automation", OK,
                  f"All {len(pinned)} pinned feature(s) measured and passing.")
    except Exception as exc:
        return _c("Engine evals", "Automation", UNKNOWN, str(exc)[:200])


def _check_unused():
    """Untested capability is untested assumption — worth seeing, not nagging."""
    try:
        from . import capabilities
        st = capabilities.status()
        never = st["counts"]["ready"] + st["counts"]["needs setup"]
        if never == 0:
            return _c("Capability coverage", "Automation", OK,
                      f"All {st['total']} capabilities have been exercised "
                      f"at least once.")
        nxt = st.get("next_test") or {}
        return _c("Capability coverage", "Automation", OK,
                  f"{st['counts']['used']}/{st['total']} exercised. "
                  f"{never} never run here.",
                  (f"Try {nxt.get('name', '')}: {nxt.get('test', '')}"
                   if nxt else ""))
    except Exception as exc:
        return _c("Capability coverage", "Automation", UNKNOWN, str(exc)[:200])


def _check_errors():
    try:
        from . import issues
        errs = issues.recent_errors(20) if hasattr(issues, "recent_errors") \
            else []
        recent = [e for e in errs
                  if (time.time() - float(e.get("ts", 0) or 0)) < 86400]
        if len(recent) >= 5:
            return _c("Recent errors", "Automation", WARN,
                      f"{len(recent)} error(s) logged in the last 24h.",
                      "Open 🐞 Issues, copy the report, and paste it into "
                      "Claude — that format is built for diagnosis.")
        if recent:
            return _c("Recent errors", "Automation", OK,
                      f"{len(recent)} error(s) in the last 24h.")
        return _c("Recent errors", "Automation", OK,
                  "No errors logged in the last 24h.")
    except Exception as exc:
        return _c("Recent errors", "Automation", UNKNOWN, str(exc)[:200])


# --------------------------------------------------------------------------- #
#  the board
# --------------------------------------------------------------------------- #
def report(memory=None) -> dict:
    checks = [
        _safe(_check_build, "Build", "Deploy"),
        _safe(_check_interpreter, "Interpreter", "Deploy"),
        _safe(_check_cloud_key, "Cloud engine", "Engines"),
        _safe(_check_ollama, "Ollama", "Engines"),
        _safe(_check_voice, "Voice input", "Engines"),
        _safe(_check_engine_mix, "Engine mix", "Engines"),
        _safe(_check_retired_models, "Retired models", "Engines"),
        _safe(lambda: _check_gone_quiet(memory), "Still producing",
              "Automation"),
        _safe(_check_blender, "Blender", "Tools"),
        _safe(_check_neural3d, "Neural 3D", "Tools"),
        _safe(_check_mcp, "MCP servers", "Tools"),
        _safe(lambda: _check_database(memory), "Database", "Safety"),
        _safe(_check_audit, "Audit chain", "Safety"),
        _safe(_check_backups, "Backups", "Safety"),
        _safe(_check_index_truncated, "Document index", "Safety"),
        _safe(_check_intercepts, "Review gate", "Safety"),
        _safe(_check_engine_models, "Engine models", "Engines"),
        _safe(_check_engine_config, "Engine setup", "Engines"),
        _safe(_check_location, "Where it lives", "Deploy"),
        _safe(_check_disk, "Disk space", "Safety"),
        _safe(_check_budget, "Spend", "Safety"),
        _safe(lambda: _check_schedules(memory), "Schedules", "Automation"),
        _safe(_check_watchers, "Watchers", "Automation"),
        _safe(_check_breakers, "Circuit breakers", "Automation"),
        _safe(_check_evals, "Engine evals", "Automation"),
        _safe(_check_unused, "Capability coverage", "Automation"),
        _safe(_check_errors, "Recent errors", "Automation"),
    ]
    counts = {OK: 0, WARN: 0, FAIL: 0, UNKNOWN: 0}
    for c in checks:
        counts[c["state"]] = counts.get(c["state"], 0) + 1
    if counts[FAIL]:
        overall = FAIL
    elif counts[WARN]:
        overall = WARN
    else:
        overall = OK
    return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "build": getattr(config, "BUILD_ID", "unknown"),
            "overall": overall, "counts": counts, "checks": checks}
