"""Audit trail — an append-only, tamper-evident record of what the agent did.

Every turn (which engine answered), every tool call (with outcome and timing),
every autonomous action (schedules firing), and every configuration change is
appended to AGENT_HOME/audit.jsonl. Each entry carries the SHA-256 of the
previous line, forming a hash chain: verify() re-walks the file and reports
exactly where any edit, deletion, or insertion broke it. Local only, like
everything else — this is *your* record of your agent.

When the file exceeds MAX_BYTES it rotates to audit-<timestamp>.jsonl and a
fresh chain starts, with the first entry naming the rotated file and its final
hash so the chains stay linkable.

Toggle with the AUDIT setting (on by default). Recording is fail-safe: an audit
hiccup can never break the action it was recording.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import time
from datetime import datetime, timezone

from . import config

MAX_BYTES = 2_000_000
_LOCK = threading.Lock()
_last_hash: str | None = None          # cache of the newest line's hash


def _path():
    return config.AGENT_HOME / "audit.jsonl"


def _sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:32]


def _tail_hash() -> str:
    """Hash of the last line on disk ('' for a fresh file)."""
    global _last_hash
    if _last_hash is not None:
        return _last_hash
    try:
        last = ""
        with open(_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line.rstrip("\n")
        _last_hash = _sha(last) if last else ""
    except Exception:
        _last_hash = ""
    return _last_hash


class _FileLock:
    """A lock the OS enforces across processes.

    The chain broke in real use because `_LOCK` is a THREAD lock: the web
    server, the scheduler and any `python -c` script that imports these
    modules are separate processes, and each computed its `prev` from the
    same tail before either had appended. Two entries then claimed the same
    predecessor, which reads exactly like tampering.

    Reproduced with four concurrent writers: the chain broke at line 72.
    """

    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+b")
            try:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            except ImportError:                      # Windows
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
        except Exception:
            # a lock we cannot take must not stop the app from recording
            self.fh = None
        return self

    def __exit__(self, *exc):
        try:
            if self.fh:
                try:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                self.fh.close()
        except Exception:
            pass
        return False


def _lock_path():
    return _path().with_suffix(".lock")


def _write_locked(entry: dict) -> str:
    """Append one entry. The caller must already hold the file lock."""
    global _last_hash
    _last_hash = None                      # another process may have written
    entry["prev"] = _tail_hash()
    line = json.dumps(entry, sort_keys=True)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            import os as _os
            _os.fsync(fh.fileno())
        except Exception:
            pass
    _last_hash = _sha(line)
    return line


def _rotate_if_needed_locked():
    """Rotate while the lock is already held.

    This used to call record(), which tried to take the same lock again — a
    deadlock, since a file lock is not re-entrant across separate opens. It
    now writes the rotation entry directly.
    """
    global _last_hash
    try:
        p = _path()
        if p.exists() and p.stat().st_size > MAX_BYTES:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            tail = _tail_hash()
            p.rename(p.with_name(f"audit-{stamp}.jsonl"))
            _last_hash = None
            _write_locked({
                "ts": round(time.time(), 3),
                "iso": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"),
                "kind": "rotate",
                "note": f"rotated to audit-{stamp}.jsonl",
                "prev_file_hash": tail})
    except Exception:
        pass


def record(kind: str, **fields) -> None:
    """Append one audit entry. Never raises; no-op when auditing is off."""
    global _last_hash
    if not getattr(config, "AUDIT", True) and kind != "rotate":
        return
    try:
        entry = {"ts": round(time.time(), 3),
                 "iso": datetime.now(timezone.utc).strftime(
                     "%Y-%m-%d %H:%M:%S UTC"),
                 "kind": str(kind)[:20]}
        for k, v in fields.items():
            if v is None:
                continue
            entry[str(k)[:30]] = (" ".join(str(v).split())[:300]
                                  if isinstance(v, str) else v)
        with _LOCK, _FileLock(_lock_path()):
            _write_locked(entry)
            # inside the same lock: rotating between another writer's
            # tail-read and its append would strand that entry
            _rotate_if_needed_locked()
    except Exception:
        pass


def recent(n: int = 50, kind: str = "") -> list:
    try:
        out = []
        for ln in _path().read_text("utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if kind and e.get("kind") != kind:
                continue
            out.append(e)
        return list(reversed(out[-max(1, n) * (3 if kind else 1):]))[:max(1, n)]
    except Exception:
        return []


def count() -> int:
    try:
        return sum(1 for ln in _path().read_text("utf-8").splitlines()
                   if ln.strip())
    except Exception:
        return 0


def _diagnose_break(line_no: int) -> dict:
    """Why did the chain break here?

    A break says "this entry's predecessor isn't the line above it", which
    covers two very different situations:

      TWO ENTRIES CLAIMING THE SAME PREDECESSOR — a race. Two processes read
      the same tail and both appended. Nothing was altered; the log is
      complete, just interleaved. This was the real cause on this machine
      before the cross-process lock went in.

      A LINE THAT SIMPLY DOESN'T FIT — an edit, a truncation, or a file
      restored from elsewhere. That is the one worth worrying about.

    Telling the user "something edited your audit log" when two of their own
    processes wrote at once is a false accusation, and it sends them looking
    for an intruder instead of pressing Reseal.
    """
    try:
        lines = [ln for ln in _path().read_text("utf-8").splitlines()
                 if ln.strip()]
    except Exception:
        return {}
    idx = line_no - 1
    if idx < 1 or idx >= len(lines):
        return {}
    try:
        here = json.loads(lines[idx])
        before = json.loads(lines[idx - 1])
    except Exception:
        return {"cause": "unreadable",
                "cause_detail": "a line here isn't valid JSON — the file was "
                                "truncated or partially written"}
    # the giveaway: this entry and the one before it name the same predecessor
    if here.get("prev") and here.get("prev") == before.get("prev"):
        return {"cause": "concurrent-write",
                "cause_detail": ("two entries claim the same predecessor, "
                                 "which is what happens when two processes "
                                 "write at once — nothing was altered, and "
                                 "the entries themselves are intact"),
                "safe_to_reseal": True}
    # does this entry's prev match ANY earlier line? then it's out of order
    want = here.get("prev", "")
    for j in range(max(0, idx - 40), idx):
        if _sha(lines[j]) == want:
            return {"cause": "out-of-order",
                    "cause_detail": (f"this entry follows line {j + 1} rather "
                                     f"than the line above it — again a "
                                     f"concurrent write, not an edit"),
                    "safe_to_reseal": True}
    return {"cause": "altered",
            "cause_detail": ("no earlier entry matches what this one claims "
                             "as its predecessor — a line was edited, "
                             "removed, or the file came from elsewhere"),
            "safe_to_reseal": False}


def verify() -> dict:
    """Walk the whole chain. {'ok': bool, 'entries': n, 'break_at': line_no}."""
    try:
        prev = ""
        n = 0
        for i, ln in enumerate(_path().read_text("utf-8").splitlines(), 1):
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except Exception:
                return {"ok": False, "entries": n, "break_at": i,
                        "reason": "unreadable line"}
            if e.get("prev", "") != prev:
                return {"ok": False, "entries": n, "break_at": i,
                        "reason": "hash chain broken",
                        **_diagnose_break(i)}
            prev = _sha(ln)
            n += 1
        return {"ok": True, "entries": n, "break_at": None}
    except FileNotFoundError:
        return {"ok": True, "entries": 0, "break_at": None}
    except Exception as exc:
        return {"ok": False, "entries": 0, "break_at": None,
                "reason": str(exc)[:120]}


def reseal() -> dict:
    """Archive a broken trail and start a fresh sealed chain.

    Deliberately NOT a repair. Re-hashing existing lines would make the file
    verify again while proving nothing — the whole point of the chain is that
    it cannot be quietly rewritten, and a tool that quietly rewrites it is the
    attack it defends against.

    So the old file is kept, untouched, under its own name; the new chain
    opens by naming that file, its size, and where it broke. The history is
    still readable, and now honestly labelled as unverified rather than
    presented as sealed.
    """
    global _last_hash
    v = verify()
    if v.get("ok"):
        return {"ok": False, "error": "the chain verifies — nothing to reseal"}
    p = _path()
    if not p.exists():
        return {"ok": False, "error": "no audit trail yet"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = p.with_name(f"audit-unverified-{stamp}.jsonl")
    try:
        size = p.stat().st_size
        with _LOCK, _FileLock(_lock_path()):
            p.rename(dest)
            _last_hash = None
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    record("reseal",
           note=(f"previous trail could not be verified and was kept as "
                 f"{dest.name}"),
           broke_at_line=v.get("break_at"),
           previous_entries=v.get("entries"),
           previous_bytes=size,
           reason=v.get("reason", ""))
    return {"ok": True, "archived": dest.name,
            "entries_kept": v.get("entries"),
            "broke_at": v.get("break_at"),
            "note": ("The old trail is kept and still readable, but it is "
                     "labelled unverified. Nothing was rewritten.")}


def archives() -> list:
    """Rotated and archived trails still on disk."""
    out = []
    try:
        for p in sorted(_path().parent.glob("audit-*.jsonl")):
            try:
                st = p.stat()
                out.append({"name": p.name, "bytes": st.st_size,
                            "modified": datetime.fromtimestamp(
                                st.st_mtime, timezone.utc).strftime(
                                    "%Y-%m-%d %H:%M UTC")})
            except Exception:
                pass
    except Exception:
        pass
    return out


def clear(keep_days: int = 0, archive: bool = True) -> dict:
    """Start a clean trail.

    A log that can be silently emptied is not tamper-evident, so clearing is
    itself recorded: the new chain opens with an entry stating how many
    entries went, over what dates, and whether they were kept. The gap is
    visible rather than invisible — which is the whole difference between
    housekeeping and a cover-up.

    keep_days > 0 keeps recent entries and drops only what is older, which is
    usually what people actually want: the scheduler noise from months ago,
    not this morning's work.
    """
    global _last_hash
    p = _path()
    if not p.exists():
        return {"ok": False, "error": "there is no audit trail yet"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        with _LOCK, _FileLock(_lock_path()):
            lines = [ln for ln in p.read_text("utf-8").splitlines()
                     if ln.strip()]
            total = len(lines)
            kept_lines = []
            if keep_days > 0:
                cutoff = time.time() - keep_days * 86400
                for ln in lines:
                    try:
                        if float(json.loads(ln).get("ts", 0)) >= cutoff:
                            kept_lines.append(ln)
                    except Exception:
                        pass
            removed = total - len(kept_lines)
            if not removed:
                return {"ok": False,
                        "error": (f"nothing is older than {keep_days} day(s) "
                                  f"— nothing to clear")}
            first_iso = last_iso = ""
            try:
                first_iso = json.loads(lines[0]).get("iso", "")
                last_iso = json.loads(lines[-1]).get("iso", "")
            except Exception:
                pass
            dest = ""
            if archive:
                dest = f"audit-cleared-{stamp}.jsonl"
                p.rename(p.with_name(dest))
            else:
                p.unlink()
            _last_hash = None
            # the kept entries cannot carry their old hashes into a new chain
            # without lying about their predecessors, so they are preserved as
            # an archive and the new chain starts empty
            _write_locked({
                "ts": round(time.time(), 3),
                "iso": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"),
                "kind": "cleared",
                "removed_entries": removed,
                "kept_entries": len(kept_lines),
                "covered": f"{first_iso} to {last_iso}",
                "archived_as": dest or "(deleted permanently)",
                "note": ("the trail was cleared on request; this entry marks "
                         "the gap so it cannot pass unnoticed")})
            if kept_lines:
                for ln in kept_lines:
                    try:
                        e = json.loads(ln)
                    except Exception:
                        continue
                    e.pop("prev", None)
                    e["kind"] = str(e.get("kind", ""))[:20]
                    e["carried_over"] = True
                    _write_locked(e)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "removed": removed, "kept": len(kept_lines),
            "archived_as": dest,
            "note": ("The old trail is kept and still readable."
                     if dest else
                     "The old trail was deleted permanently.")}


def delete_archives(names: list = None) -> dict:
    """Remove archived trails. Recorded, like everything else here."""
    gone, freed = [], 0
    try:
        for a in archives():
            if names and a["name"] not in names:
                continue
            f = _path().with_name(a["name"])
            freed += a["bytes"]
            f.unlink()
            gone.append(a["name"])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not gone:
        return {"ok": False, "error": "no archives matched"}
    record("archives-deleted", count=len(gone), bytes_freed=freed,
           note=", ".join(gone)[:280])
    return {"ok": True, "deleted": gone, "bytes_freed": freed}


def export_csv() -> str:
    rows = list(reversed(recent(5000)))
    cols = ["iso", "kind", "engine", "name", "action", "status", "ok", "ms",
            "session", "summary", "detail", "prev"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for e in rows:
        w.writerow([e.get(c, "") for c in cols])
    return buf.getvalue()


def export_text() -> str:
    rows = list(reversed(recent(5000)))
    if not rows:
        return "(audit trail is empty)"
    lines = [f"# Agent Jo audit trail — {len(rows)} entries, chain "
             f"{'OK' if verify()['ok'] else 'BROKEN'}"]
    for e in rows:
        bits = [e.get("iso", ""), e.get("kind", "")]
        for k in ("engine", "name", "action", "status", "summary", "detail"):
            if e.get(k) not in (None, ""):
                bits.append(f"{k}={e[k]}")
        if "ok" in e:
            bits.append("ok" if e["ok"] else "FAILED")
        lines.append(" | ".join(str(b) for b in bits))
    return "\n".join(lines)
