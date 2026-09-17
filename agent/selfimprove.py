"""Self-improvement — Agent Jo can build its own next feature, safely.

Ask the agent to add a feature or fix a bug in ITSELF, the same way you'd ask
any coding assistant. The pipeline is deliberately shaped so it can't hurt you:

  1. SANDBOX  — the agent works on a full COPY of its own source
                (never the live files), with writes auto-approved only there.
  2. TEST GATE — the modified copy must pass the app's own test suite
                 (the same ~500 checks that gate every human change).
  3. PROPOSAL — you get a per-file diff + the test result in the ⇪ panel.
  4. HUMAN APPLY — only your click copies changes into the live tree, and
                   every replaced file is snapshotted to the Time Machine
                   first, so an applied upgrade is one click from undone.
  5. AUDIT    — every stage lands in the tamper-evident audit trail.

The agent can propose; only you can apply. There is no autonomous path to
self-modification, and the whole feature has a kill-switch (SELFIMPROVE).
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

# the app's own source root; tests point this at a temp tree
LIVE_ROOT = Path(__file__).resolve().parent.parent
SUITE_TIMEOUT = 420
_IGNORE = shutil.ignore_patterns(
    ".venv", "venv", "env", "__pycache__", "*.pyc", ".git", "dist", "build",
    "node_modules", ".pytest_cache")
_DIFF_CAP = 20_000


def _base() -> Path:
    d = config.AGENT_HOME / "selfimprove"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspace_path() -> Path:
    return _base() / "workspace"


def _proposal_path() -> Path:
    return _base() / "proposal.json"


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def create_workspace(request: str) -> dict:
    """Fresh sandbox copy of the live source. Replaces any prior workspace."""
    ws = workspace_path()
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(LIVE_ROOT, ws, ignore=_IGNORE)
    _proposal_path().write_text(json.dumps({
        "request": request[:600], "created": _iso(), "state": "working",
        "tests_ok": False, "tests_summary": "not run yet", "files": []}),
        "utf-8")
    return {"workspace": str(ws)}


def run_suite() -> dict:
    """Run the app's own test suite INSIDE the workspace, with an isolated
    AGENT_HOME so tests can't touch real data. The gate every change must pass."""
    ws = workspace_path()
    if not ws.exists():
        return {"ok": False, "summary": "no workspace — start first"}
    import os
    import tempfile
    env = dict(os.environ)
    env["AGENT_HOME"] = tempfile.mkdtemp(prefix="selfimprove-tests-")
    env.setdefault("ANTHROPIC_API_KEY", "test-key-unused")
    try:
        proc = subprocess.run(
            [sys.executable, "tests/run_tests.py"], cwd=str(ws),
            capture_output=True, text=True, timeout=SUITE_TIMEOUT, env=env)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"(\d+) checks passed OK", out)
        ok = proc.returncode == 0 and m is not None
        if ok:
            summary = f"{m.group(1)} checks passed"
        else:
            fail_lines = [ln for ln in out.strip().splitlines()
                          if "FAILED" in ln or "AssertionError" in ln
                          or "Error" in ln]
            summary = "FAILED — " + (fail_lines[-1][:200] if fail_lines
                                     else (out.strip().splitlines()[-1][:200]
                                           if out.strip() else "no output"))
        tail = "\n".join(out.strip().splitlines()[-25:])[:4000]
    except subprocess.TimeoutExpired:
        ok, summary, tail = False, "FAILED — test suite timed out", ""
    except Exception as exc:
        ok, summary, tail = False, f"FAILED — {type(exc).__name__}: {exc}", ""
    _update_proposal(tests_ok=ok, tests_summary=summary, state="tested")
    return {"ok": ok, "summary": summary, "output_tail": tail}


def _rel_files(root: Path) -> set:
    out = set()
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root)
            parts = rel.parts
            if any(x in ("__pycache__", ".git", ".venv", "venv", "env",
                         "dist", "build", "node_modules") for x in parts):
                continue
            if rel.suffix == ".pyc":
                continue
            out.add(str(rel))
    return out


def diff_against_live() -> list:
    """Per-file changes workspace vs live: added / modified (deletions are
    reported but never applied — safer)."""
    ws = workspace_path()
    if not ws.exists():
        return []
    changes = []
    ws_files, live_files = _rel_files(ws), _rel_files(LIVE_ROOT)
    import difflib
    for rel in sorted(ws_files):
        wf, lf = ws / rel, LIVE_ROOT / rel
        try:
            new = wf.read_bytes()
            old = lf.read_bytes() if rel in live_files else None
        except Exception:
            continue
        if old == new:
            continue
        entry = {"path": rel,
                 "status": "added" if old is None else "modified"}
        try:
            d = "\n".join(difflib.unified_diff(
                (old or b"").decode("utf-8").splitlines(),
                new.decode("utf-8").splitlines(),
                fromfile=f"live/{rel}", tofile=f"proposed/{rel}",
                lineterm=""))[:_DIFF_CAP]
            entry["diff"] = d or "(no text difference)"
        except Exception:
            entry["diff"] = "(binary file)"
        if rel.startswith(("agent/audit", "agent/timemachine",
                           "agent/privacy")):
            entry["warning"] = "changes a safety module — review carefully"
        changes.append(entry)
    for rel in sorted(live_files - ws_files):
        changes.append({"path": rel, "status": "deleted in workspace",
                        "diff": "(deletions are never applied "
                                "automatically — remove by hand if intended)"})
    return changes


def finalize_proposal(summary: str) -> dict:
    files = diff_against_live()
    applied_kinds = [f for f in files if f["status"] in ("added", "modified")]
    _update_proposal(state="proposed", summary=summary[:800],
                     files=[{k: f[k] for k in ("path", "status")}
                            for f in files])
    return {"ok": True, "changed_files": len(applied_kinds),
            "note": "Proposal ready — review and apply it from the "
                    "⇪ Self-improve panel."}


def proposal() -> dict:
    try:
        p = json.loads(_proposal_path().read_text("utf-8"))
    except Exception:
        return {"state": "none"}
    if workspace_path().exists() and p.get("state") != "applied":
        p["diffs"] = diff_against_live()
    return p


def _update_proposal(**kw) -> None:
    try:
        p = json.loads(_proposal_path().read_text("utf-8"))
    except Exception:
        p = {}
    p.update(kw)
    _proposal_path().write_text(json.dumps(p), "utf-8")


def apply() -> dict:
    """Copy tested changes into the live tree. Every replaced file is
    snapshotted to the Time Machine first; the whole apply is audited."""
    p = proposal()
    if p.get("state") in ("none", "applied"):
        return {"ok": False, "error": "no proposal to apply"}
    if not p.get("tests_ok"):
        return {"ok": False,
                "error": "tests have not passed — refusing to apply"}
    from . import timemachine
    applied, snapshots = [], []
    for f in diff_against_live():
        if f["status"] not in ("added", "modified"):
            continue
        src = workspace_path() / f["path"]
        dst = LIVE_ROOT / f["path"]
        # keep the snapshot id, so this improvement can be undone as a unit
        eid = timemachine.snapshot(dst, tool="selfimprove")
        snapshots.append({"path": f["path"], "entry_id": eid,
                          "existed": dst.exists()})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        applied.append(f["path"])
    imp_id = uuid.uuid4().hex[:12]
    _update_proposal(state="applied", applied=applied, applied_at=_iso(),
                     improvement_id=imp_id)
    _record_applied({"id": imp_id, "at": _iso(),
                     "summary": p.get("summary", "")[:300],
                     "request": p.get("request", "")[:300],
                     "files": applied, "snapshots": snapshots,
                     "tests": p.get("tests", {})})
    try:
        from . import audit
        audit.record("selfimprove", name="apply",
                     detail=",".join(applied)[:280],
                     summary=f"{len(applied)} file(s) updated — "
                             f"restart to activate")
    except Exception:
        pass
    return {"ok": True, "applied": applied,
            "note": "Restart Agent Jo to activate the new code. Each replaced "
                    "file is in ⟲ Undo if you want it back."}


def discard() -> dict:
    shutil.rmtree(workspace_path(), ignore_errors=True)
    try:
        _proposal_path().unlink()
    except Exception:
        pass
    return {"ok": True}


# =========================================================================== #
#  History, revert, and knowing what to improve
#
#  The machinery was sound — sandbox, full suite, human gate, snapshots — but
#  everything around it was thin. Once a change was applied it vanished from
#  view: no record of what had been built, and no way to undo one improvement
#  without hunting through the Time Machine for the right files. And it only
#  ever built what you thought to ask for, while the app was already sitting
#  on a pile of evidence about its own faults.
# =========================================================================== #
def _history_path() -> Path:
    return _base() / "history.jsonl"


def history(n: int = 30) -> list:
    try:
        lines = _history_path().read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
        if len(out) >= n:
            break
    return out


def _record_applied(entry: dict) -> None:
    try:
        _base().mkdir(parents=True, exist_ok=True)
        with open(_history_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def revert(improvement_id: str) -> dict:
    """Undo one applied improvement, using the snapshots taken as it landed.

    Restores newest-first so a file touched twice ends up at the state it had
    before THIS improvement, not before the earlier one."""
    entry = next((h for h in history(200)
                  if h.get("id") == improvement_id), None)
    if entry is None:
        return {"ok": False, "error": "no such improvement"}
    if entry.get("reverted_at"):
        return {"ok": False, "error": "that one was already reverted"}
    from . import timemachine
    restored, failed = [], []
    for snap in reversed(entry.get("snapshots") or []):
        if not snap.get("entry_id"):
            failed.append(snap.get("path", "?"))
            continue
        r = timemachine.restore(snap["entry_id"])
        (restored if r.get("ok") else failed).append(snap.get("path", "?"))
    if not restored:
        return {"ok": False,
                "error": ("nothing could be restored — the snapshots have "
                          "been pruned. The Time Machine keeps a limited "
                          "history.")}
    lines = []
    try:
        lines = _history_path().read_text("utf-8").splitlines()
    except Exception:
        pass
    out = []
    for ln in lines:
        try:
            h = json.loads(ln)
            if h.get("id") == improvement_id:
                h["reverted_at"] = _iso()
            out.append(json.dumps(h))
        except Exception:
            out.append(ln)
    try:
        _history_path().write_text("\n".join(out) + "\n", "utf-8")
    except Exception:
        pass
    try:
        from . import audit
        audit.record("selfimprove", name="revert", detail=improvement_id,
                     summary=f"{len(restored)} file(s) rolled back")
    except Exception:
        pass
    return {"ok": True, "restored": restored, "failed": failed,
            "note": "Restart Agent Jo for the rollback to take effect."}


# --------------------------------------------------------------------------- #
#  What's worth improving — drawn from the app's own record, not invented
# --------------------------------------------------------------------------- #
def suggestions(memory=None) -> list:
    """Concrete things to fix, each with the evidence behind it.

    The app already knows where it hurts: repeated errors, failing health
    checks, features that have never worked here, breakers that keep tripping.
    Asking a model to invent improvement ideas would produce plausible noise;
    reading the record produces things that have actually gone wrong."""
    out = []

    def _add(title, why, request, weight):
        out.append({"title": title, "why": why, "request": request,
                    "weight": weight})

    # 1. errors that keep happening
    try:
        from . import issues
        from collections import Counter
        errs = issues.recent_errors(80) or []
        seen = Counter()
        for e in errs:
            # the field is "message"; reading "error" found nothing at all
            msg = str(e.get("message") or e.get("error")
                      or e.get("summary") or "")[:90]
            if msg:
                seen[msg] += 1
        for msg, n in seen.most_common(3):
            if n >= 3:
                _add(f"A repeating error: {msg[:60]}",
                     f"logged {n} times",
                     f"Find and fix the cause of this recurring error, then "
                     f"add a test that fails without the fix: {msg}",
                     100 + n)
    except Exception:
        pass

    # 2. health checks that are failing right now
    try:
        from . import health
        rep = health.report(memory)
        for c in rep.get("checks", []):
            if c.get("state") == "fail":
                _add(f"Health check failing: {c['name']}",
                     c.get("detail", "")[:120],
                     f"Fix the cause of the failing health check "
                     f"'{c['name']}': {c.get('detail','')}. "
                     f"{c.get('fix','')}",
                     90)
    except Exception:
        pass

    # 3. breakers that keep tripping — a feature failing repeatedly
    try:
        from . import breaker
        for b in breaker.status():
            if b.get("state") == "open" or (b.get("trips") or 0) >= 2:
                _add(f"{b['feature']} keeps failing",
                     f"the circuit breaker has tripped "
                     f"{b.get('trips', 1)} time(s)",
                     f"Investigate why {b['feature']} keeps failing "
                     f"({b.get('last_error','')}) and make it resilient.",
                     80)
    except Exception:
        pass

    # 4. capabilities that have never worked on this machine
    try:
        from . import capabilities
        st = capabilities.status()
        gaps = [c for c in st.get("capabilities", [])
                if c.get("state") == "needs setup"]
        if gaps:
            g = gaps[0]
            # capabilities describe the gap in "gap"/"test", not "detail" —
            # reading the wrong key left the reason blank, which is exactly
            # the uninformative suggestion this is meant to avoid
            why = (g.get("blocker") or g.get("test")
                   or "never exercised on this machine")
            _add(f"{g['name']} has never worked here", str(why)[:120],
                 f"Make {g['name']} work on this machine, or state plainly "
                 f"in the UI what's missing. {why}", 60)
    except Exception:
        pass

    out.sort(key=lambda x: -x["weight"])
    return out[:6]
