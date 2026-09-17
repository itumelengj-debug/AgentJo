"""Time Machine — an undo button for the agent's file changes.

The audit trail tells you WHAT the agent did; this lets you TAKE IT BACK.
Before every write_file mutation, the previous version of the file is copied
into a local shadow store (AGENT_HOME/timemachine/). From the ⟲ Undo panel you
can see what changed, diff it, and restore any version with one click.

Design decisions, deliberately:
  • Snapshot BEFORE the mutation, at the tool boundary — so the safety net
    can't be forgotten by the model; it's structural.
  • A restore is itself snapshotted first, so you can undo an undo.
  • Creations are recorded too: undoing a "create" deletes the file the agent
    made (after snapshotting it, so even that is reversible).
  • Bounded: per-file cap (big binaries are recorded but not stored) and a
    pruned store (oldest entries fall off) so it can't eat the disk.

Honest scope: this protects the agent's own write_file tool. It cannot cover
run_command side effects (arbitrary programs) or writes made by external MCP
servers (their tools, their side effects) — those remain visible in the audit
trail but are not restorable from here.
"""
from __future__ import annotations

import difflib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_FILE_BYTES = 5_000_000          # per-snapshot cap; larger = recorded only
MAX_ENTRIES = 400                   # prune oldest beyond this
MAX_STORE_BYTES = 200_000_000       # total shadow-store budget
_LOCK = threading.Lock()


def _dir() -> Path:
    d = config.AGENT_HOME / "timemachine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index() -> Path:
    return _dir() / "index.jsonl"


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _append(entry: dict) -> None:
    with open(_index(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _load() -> list:
    try:
        out = []
        for ln in _index().read_text("utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except FileNotFoundError:
        return []


def _save_all(entries: list) -> None:
    _index().write_text(
        "".join(json.dumps(e) + "\n" for e in entries), "utf-8")


def snapshot(path, tool: str = "write_file", session_id: str = "") -> str:
    """Record the state of `path` BEFORE a mutation. Never raises — a safety
    net that broke the thing it protects would be worse than none.

    Returns the entry id, so a caller that changes several files can undo
    exactly its own set later. It used to return nothing, which meant an
    applied self-improvement could not be reverted as a unit."""
    if not getattr(config, "TIMEMACHINE", True):
        return ""
    try:
        p = Path(path).expanduser()
        eid = uuid.uuid4().hex[:12]
        entry = {"id": eid, "ts": round(time.time(), 3), "iso": _iso(),
                 "path": str(p), "tool": tool, "session": session_id}
        if p.exists() and p.is_file():
            size = p.stat().st_size
            entry["action"] = "overwrite"
            entry["size"] = size
            if size <= MAX_FILE_BYTES:
                (_dir() / f"{eid}.snap").write_bytes(p.read_bytes())
                entry["stored"] = True
            else:
                entry["stored"] = False
                entry["note"] = "previous version too large to store"
        else:
            entry["action"] = "create"
            entry["size"] = 0
            entry["stored"] = True          # restorable: undo = remove file
        with _LOCK:
            _append(entry)
        _prune()
    except Exception:
        pass
    return eid

def entries(n: int = 60) -> list:
    out = list(reversed(_load()))[:max(1, n)]
    for e in out:
        e["restorable"] = bool(e.get("stored"))
    return out


def _find(entry_id: str):
    for e in _load():
        if e.get("id") == entry_id:
            return e
    return None


def diff(entry_id: str) -> dict:
    e = _find(entry_id)
    if not e:
        return {"ok": False, "error": "unknown entry"}
    p = Path(e["path"])
    if e["action"] == "create":
        old_text = ""
    else:
        snap = _dir() / f"{e['id']}.snap"
        if not snap.exists():
            return {"ok": False, "error": e.get("note",
                                                "snapshot not stored")}
        try:
            old_text = snap.read_bytes().decode("utf-8")
        except Exception:
            return {"ok": True, "binary": True,
                    "text": "(binary file — diff unavailable; restore works)"}
    try:
        new_text = p.read_text("utf-8") if p.exists() else ""
    except Exception:
        return {"ok": True, "binary": True,
                "text": "(binary file — diff unavailable; restore works)"}
    ud = "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"before ({e['iso']})", tofile="now", lineterm=""))
    return {"ok": True, "binary": False,
            "text": ud or "(no differences — file is back to this version)"}


def restore(entry_id: str, session_id: str = "") -> dict:
    """Put the file back the way it was at this entry. The current state is
    snapshotted first, so a restore is itself undoable."""
    e = _find(entry_id)
    if not e:
        return {"ok": False, "error": "unknown entry"}
    if not e.get("stored"):
        return {"ok": False, "error": e.get("note", "snapshot not stored")}
    p = Path(e["path"])
    try:
        snapshot(p, tool="restore", session_id=session_id)   # undo the undo
        if e["action"] == "create":
            if p.exists():
                p.unlink()
            result = f"removed {p} (undid its creation)"
        else:
            data = (_dir() / f"{e['id']}.snap").read_bytes()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            result = f"restored {p} to its {e['iso']} version"
        try:
            from . import audit
            audit.record("undo", name=str(p), detail=e["action"],
                         session=session_id, summary=result[:200])
        except Exception:
            pass
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def store_size() -> int:
    try:
        return sum(f.stat().st_size for f in _dir().glob("*.snap"))
    except Exception:
        return 0


def _prune() -> None:
    try:
        with _LOCK:
            ents = _load()
            drop = []
            while len(ents) - len(drop) > MAX_ENTRIES:
                drop.append(ents[len(drop)])
            kept = ents[len(drop):]
            total = store_size()
            i = 0
            while total > MAX_STORE_BYTES and i < len(kept):
                snapf = _dir() / f"{kept[i]['id']}.snap"
                if snapf.exists():
                    total -= snapf.stat().st_size
                drop.append(kept[i])
                i += 1
            if not drop:
                return
            kept = [e for e in ents if e not in drop]
            for e in drop:
                f = _dir() / f"{e['id']}.snap"
                if f.exists():
                    f.unlink()
            _save_all(kept)
    except Exception:
        pass
