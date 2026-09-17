"""Back up and restore everything Agent Jo knows — locally, in one file.

A backup is a single .zip holding clean snapshots of both databases (memories,
skills, tasks, schedules, the message log, and the document index), a
human-readable JSON export of the text data, and a small manifest. Restore
copies a backup's databases back into the running app with SQLite's online
backup API, so it works while Agent Jo is open and doesn't fight Windows file
locks. Nothing ever leaves the machine.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import config

FORMAT_VERSION = 2

# Everything below was added after this module was first written, and none of
# it lives in the databases: an archive without it restores your memories but
# loses your settings, engines, crew briefs, pipeline specs and — worst — the
# hash-chained audit trail. Backing up half the app is a false sense of safety.
_EXTRA_FILES = [
    "settings.json", "engines.json", "mcp.json", "folders.json",
    "email.json", "engine_stats.json", "autoresume.json",
    "autoresume_state.json", "watch_state.json",
    "audit.jsonl", "problems.jsonl", "errors.jsonl",
    "autoresume_log.jsonl", "email_log.jsonl", "folderwatch_log.jsonl",
    "watchers_log.jsonl",
]
_EXTRA_DIRS = ["crew", "trendscout", "pipelines"]

# Reproducible bulk — renders, meshes, undo snapshots, the self-improve
# sandbox. Can run to hundreds of MB; excluded so the archive stays small
# enough to actually copy off the machine.
_BULK_DIRS = ["blender", "neural3d", "timemachine", "selfimprove"]

# secret.key unseals stored API keys. This module's sibling (agent/crypto.py)
# states the threat model plainly: sealed values are safe if copied or backed
# up WITHOUT the key. So it is excluded by default — an archive that leaks
# then reveals nothing. Include it only for a move to a new machine, and
# treat that archive as equivalent to your credentials.
_KEY_FILE = "secret.key"

_SENSITIVE = {
    "engines.json": "custom-engine API keys (sealed unless secret.key is "
                    "also in this archive)",
    "mcp.json": "MCP server tokens — stored in PLAINTEXT",
    "email.json": "email account settings",
    _KEY_FILE: "the master key that unseals stored API keys",
}

_AGENT_DB = "agent.db"
_DOCS_DB = "documents.db"
_EXPORT = "export.json"
_MANIFEST = "manifest.json"


def _snapshot(conn, out_path: str) -> None:
    """Write a clean, consistent copy of an open SQLite database to out_path."""
    dst = sqlite3.connect(out_path)
    try:
        conn.backup(dst)
    finally:
        dst.close()


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune(out_dir, keep: int) -> None:
    """Keep the N most recent archives; old ones are silently retired."""
    try:
        files = sorted(Path(str(out_dir)).glob("*-backup-*.zip"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[max(1, int(keep)):]:
            old.unlink()
    except Exception:
        pass


def _audit(name: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("backup", name=name, summary=summary[:250])
    except Exception:
        pass


def _backups_dir():
    d = config.AGENT_HOME / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve(name: str):
    """Look a backup up by NAME inside the backups folder — never a path,
    so a crafted name can't reach elsewhere on disk."""
    if not name or name != Path(name).name or not name.endswith(".zip"):
        return None
    p = _backups_dir() / name
    return p if p.exists() else None


def list_backups() -> list:
    out = []
    for p in sorted(_backups_dir().glob("*-backup-*.zip"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        m = read_manifest(str(p))
        out.append({
            "name": p.name, "bytes": p.stat().st_size,
            "created": m.get("created_at", ""),
            "counts": m.get("counts", {}),
            "includes_documents": m.get("includes_documents", False),
            "includes_bulk": m.get("includes_bulk", False),
            "includes_secret_key": m.get("includes_secret_key", False),
            "note": m.get("note", ""),
            "format": m.get("format_version", 1),
            "valid": bool(m),
        })
    return out


def verify_backup(name: str) -> dict:
    """Check every file against its recorded checksum before trusting it.
    Format-1 archives predate checksums; they verify structurally instead."""
    p = _resolve(name)
    if p is None:
        return {"ok": False, "error": "no such backup"}
    try:
        with zipfile.ZipFile(str(p)) as zf:
            names = set(zf.namelist())
            if _AGENT_DB not in names:
                return {"ok": False,
                        "error": "not an Agent Jo backup (agent.db missing)"}
            m = read_manifest(str(p))
            sums = m.get("checksums") or {}
            if not sums:
                return {"ok": True, "legacy": True, "files": len(names),
                        "detail": "older backup without checksums — "
                                  "structure looks valid"}
            bad = []
            for arc, want in sums.items():
                if arc not in names:
                    bad.append(f"{arc} (missing)")
                    continue
                if hashlib.sha256(zf.read(arc)).hexdigest() != want:
                    bad.append(f"{arc} (checksum mismatch)")
            if bad:
                return {"ok": False, "error": "archive is damaged",
                        "problems": bad[:10]}
            return {"ok": True, "files": len(sums),
                    "detail": f"all {len(sums)} file(s) verified"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def delete_backup(name: str) -> dict:
    p = _resolve(name)
    if p is None:
        return {"ok": False, "error": "no such backup"}
    p.unlink()
    _audit("delete", p.name)
    return {"ok": True}


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _export_json(memory) -> dict:
    """Human-readable export of the text data (embeddings are left to the db)."""
    data = {
        "memories": _safe(memory.all_memories, []),
        "skills": _safe(memory.get_skills, []),
        "tasks": _safe(lambda: memory.list_tasks("all", 100000), []),
        "permissions": _safe(memory.list_permissions, []),
        "schedules": _safe(memory.list_schedules, []),
        "routing_feedback": _safe(lambda: memory.get_routing_escalations(100000), []),
        "conversations": [],
    }
    sessions = _safe(lambda: memory.list_sessions(100000), [])
    convos = []
    for s in sessions:
        sid = s.get("session_id") or s.get("id")
        convos.append({"session": s,
                       "messages": _safe(lambda: memory.get_transcript(sid), [])})
    data["conversations"] = convos
    return data


def create_backup(memory, rag_store=None, out_dir=None,
                  include_bulk: bool = False, include_key: bool = False,
                  note: str = "", keep: int = 7) -> str:
    """Create a backup zip and return its path. Writes to <data>/backups by
    default. `rag_store` is optional (the document index)."""
    out_dir = out_dir or (config.AGENT_HOME / "backups")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Second-resolution stamps collide when two backups land in the same
    # second — and the rollback taken during a restore is exactly that case,
    # which could overwrite the archive being restored FROM. Never collide.
    zip_path = os.path.join(str(out_dir), f"atlas-backup-{stamp}.zip")
    _n = 1
    while os.path.exists(zip_path):
        zip_path = os.path.join(str(out_dir),
                                f"atlas-backup-{stamp}-{_n}.zip")
        _n += 1

    export = _export_json(memory)
    manifest = {
        "format_version": FORMAT_VERSION,
        "app": getattr(config, "AGENT_NAME", "Agent Jo"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "memories": len(export["memories"]),
            "skills": len(export["skills"]),
            "tasks": len(export["tasks"]),
            "conversations": len(export["conversations"]),
            "documents": _safe(lambda: rag_store.doc_count() if rag_store else 0, 0),
        },
        "includes_documents": rag_store is not None,
        "includes_bulk": bool(include_bulk),
        "includes_secret_key": False,     # set truthfully below
        "note": (note or "")[:300],
        "app_build": getattr(config, "BUILD_ID", "unknown"),
        "checksums": {},
        "skipped": [],
    }

    with tempfile.TemporaryDirectory() as tmp:
        agent_snap = os.path.join(tmp, _AGENT_DB)
        _snapshot(memory.conn, agent_snap)
        files = [(agent_snap, _AGENT_DB)]
        if rag_store is not None:
            docs_snap = os.path.join(tmp, _DOCS_DB)
            _snapshot(rag_store.conn, docs_snap)
            files.append((docs_snap, _DOCS_DB))
        export_path = os.path.join(tmp, _EXPORT)
        with open(export_path, "w", encoding="utf-8") as fh:
            json.dump(export, fh, ensure_ascii=False, indent=2, default=str)
        manifest_path = os.path.join(tmp, _MANIFEST)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        files += [(export_path, _EXPORT), (manifest_path, _MANIFEST)]

        # --- everything outside the databases ---------------------------
        home = config.AGENT_HOME
        for name in _EXTRA_FILES:
            p = home / name
            if p.exists() and p.is_file():
                files.append((str(p), name))
        for d in _EXTRA_DIRS:
            root = home / d
            if root.exists():
                for f in root.rglob("*"):
                    if f.is_file():
                        files.append((str(f), f.relative_to(home).as_posix()))
        if include_bulk:
            for d in _BULK_DIRS:
                root = home / d
                if root.exists():
                    for f in root.rglob("*"):
                        if f.is_file():
                            files.append(
                                (str(f), f.relative_to(home).as_posix()))
        else:
            manifest["skipped"].append(
                "blender/, neural3d/, timemachine/, selfimprove/ "
                "(reproducible bulk — tick 'include renders & snapshots' "
                "to keep them)")
        keyp = home / _KEY_FILE
        if include_key and keyp.exists():
            files.append((str(keyp), _KEY_FILE))
            manifest["includes_secret_key"] = True
        elif keyp.exists():
            manifest["skipped"].append(
                "secret.key (excluded by default — sealed API keys in this "
                "archive cannot be opened without it)")

        # checksums make a damaged archive detectable BEFORE it is restored
        for src, arc in files:
            if arc != _MANIFEST:
                manifest["checksums"][arc] = _sha256(src)
        manifest["sensitive"] = {n: why for n, why in _SENSITIVE.items()
                                 if any(a == n for _, a in files)}
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, arc in files:
                zf.write(src, arc)
    _prune(out_dir, keep)
    _audit("create", f"{len(files)} file(s)"
                     + (" incl. secret.key" if manifest["includes_secret_key"]
                        else ""))
    return zip_path


def read_manifest(zip_path: str) -> dict:
    """Return the manifest from a backup zip, or {} if absent/unreadable."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if _MANIFEST in zf.namelist():
                return json.loads(zf.read(_MANIFEST).decode("utf-8"))
    except Exception:
        pass
    return {}


def restore_backup(zip_path: str, memory, rag_store=None,
                   make_rollback: bool = True) -> dict:
    """Restore a backup into the running stores using the SQLite backup API.
    Replaces all current data. Returns a summary dict. Raises ValueError if the
    file isn't a usable Agent Jo backup.

    Order matters and is deliberate: VERIFY the archive, then snapshot the
    CURRENT state to a rollback archive, and only then overwrite. Restoring
    the wrong file should never be the end of the story."""
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("That file isn't a backup zip.")
    # Verify BEFORE touching anything. A damaged archive must never become
    # the thing that replaces your working data.
    _name = os.path.basename(zip_path)
    if _resolve(_name) is not None:
        _v = verify_backup(_name)
        if not _v.get("ok"):
            raise ValueError(
                "Refusing to restore: " + _v.get("error", "archive invalid")
                + (" — " + "; ".join(_v.get("problems", [])[:3])
                   if _v.get("problems") else ""))

    rollback = None
    if make_rollback:
        try:
            rollback = os.path.basename(create_backup(
                memory, rag_store, include_key=True,
                note=f"rollback point taken before restoring "
                     f"{os.path.basename(zip_path)}"))
        except Exception as exc:
            raise ValueError(
                "Could not create a rollback point first, so nothing was "
                f"restored: {type(exc).__name__}: {exc}")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if _AGENT_DB not in names:
            raise ValueError("This zip doesn't contain an Agent Jo backup "
                             f"({_AGENT_DB} is missing).")
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            summary = {"restored": [], "documents": False}

            agent_path = os.path.join(tmp, _AGENT_DB)
            src = sqlite3.connect(agent_path)
            try:
                src.backup(memory.conn)            # uploaded -> live connection
            finally:
                src.close()
            memory.ensure_schema()                 # forward-compat for old backups
            summary["restored"] = [
                f"{memory.memory_count()} memories",
                f"{len(memory.get_skills())} skills",
            ]

            # everything outside the databases: settings, engines, crew,
            # trends, pipelines, the audit trail…
            home = config.AGENT_HOME
            extras = 0
            for arc in names:
                if arc in (_AGENT_DB, _DOCS_DB, _EXPORT, _MANIFEST):
                    continue
                if arc.startswith("/") or ".." in arc:      # traversal guard
                    continue
                src_path = os.path.join(tmp, arc.replace("/", os.sep))
                if not os.path.isfile(src_path):
                    continue
                dst = home / arc
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_path, dst)
                extras += 1
            if extras:
                summary["restored"].append(f"{extras} config/state file(s)")
            summary["rollback"] = rollback

            if _DOCS_DB in names and rag_store is not None:
                docs_path = os.path.join(tmp, _DOCS_DB)
                src = sqlite3.connect(docs_path)
                try:
                    src.backup(rag_store.conn)
                finally:
                    src.close()
                rag_store.ensure_schema()
                summary["documents"] = True
                summary["restored"].append(
                    f"{_safe(rag_store.doc_count, 0)} documents")
    summary.setdefault("rollback", rollback)
    summary["note"] = ("Restart Agent Jo so it reloads restored settings, "
                       "engines and crew state.")
    _audit("restore", f"from {os.path.basename(zip_path)}: "
                      + ", ".join(summary.get("restored", []))[:180])
    return summary


# --------------------------------------------------------------------------- #
#  nightly autopilot (opt-in) — a backup you have to remember isn't a backup
# --------------------------------------------------------------------------- #
def _cfg_path():
    return _backups_dir() / "config.json"


def load_config() -> dict:
    try:
        return json.loads(_cfg_path().read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    _cfg_path().write_text(json.dumps(cfg), "utf-8")


def schedule_enabled(memory) -> bool:
    sid = load_config().get("schedule_id")
    if not sid:
        return False
    try:
        return memory.get_schedule(int(sid)) is not None
    except Exception:
        return False


def set_schedule(memory, scheduler, enabled: bool) -> bool:
    cfg = load_config()
    sid = cfg.get("schedule_id")
    if enabled and not schedule_enabled(memory):
        spec = scheduler.make_spec("daily", time_str="23:30", n=30)
        nxt = scheduler.next_run(scheduler.parse_spec(spec))
        new_sid = memory.create_schedule(
            "Backup — nightly",
            "Archive memories, skills, audit trail, crew and settings",
            spec, "Auto", False, nxt, action="backup", payload="{}")
        save_config({**cfg, "schedule_id": new_sid})
        return True
    if not enabled and sid:
        try:
            memory.delete_schedule(int(sid))
        except Exception:
            pass
        cfg.pop("schedule_id", None)
        save_config(cfg)
    return schedule_enabled(memory)
