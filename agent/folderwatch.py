"""Watch folders — documents flow into the knowledge base by themselves.

Register folders; a managed schedule sweeps them and ingests anything new or
changed into the same document store the chat searches. Change detection is
the store's own per-file mtime tracking (unchanged files are skipped, edited
files are re-indexed), so a sweep of a quiet folder costs almost nothing.

Config: AGENT_HOME/folders.json   {"folders": [{"path": ..., "enabled": true}],
                                   "cadence": "hourly", "schedule_id": ...}
Log:    AGENT_HOME/folderwatch_log.jsonl (one line per sweep that did work)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

MAX_LOG_BYTES = 256_000


def _cfg_path():
    return config.AGENT_HOME / "folders.json"


def _log_path():
    return config.AGENT_HOME / "folderwatch_log.jsonl"


def load_config() -> dict:
    try:
        data = json.loads(_cfg_path().read_text("utf-8"))
        if not isinstance(data.get("folders"), list):
            data["folders"] = []
        return data
    except Exception:
        return {"folders": [], "cadence": "hourly", "schedule_id": None}


def save_config(update: dict) -> dict:
    cfg = load_config()
    cfg.update(update or {})
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), "utf-8")
    return cfg


def add_folder(path: str) -> dict:
    cfg = load_config()
    norm = str(Path(path).expanduser())
    if not any(f["path"] == norm for f in cfg["folders"]):
        cfg["folders"].append({"path": norm, "enabled": True})
        save_config(cfg)
    return load_config()


def remove_folder(path: str) -> dict:
    cfg = load_config()
    cfg["folders"] = [f for f in cfg["folders"] if f["path"] != path]
    return save_config(cfg)


def set_enabled(path: str, enabled: bool) -> dict:
    cfg = load_config()
    for f in cfg["folders"]:
        if f["path"] == path:
            f["enabled"] = bool(enabled)
    return save_config(cfg)


def enabled_folders() -> list:
    return [f for f in load_config()["folders"] if f.get("enabled", True)]


def record_run(entry: dict) -> None:
    try:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if p.stat().st_size > MAX_LOG_BYTES:
            lines = p.read_text("utf-8").splitlines()[-300:]
            p.write_text("\n".join(lines) + "\n", "utf-8")
    except Exception:
        pass


def recent_log(n: int = 20) -> list:
    try:
        lines = _log_path().read_text("utf-8").splitlines()[-max(1, n):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return list(reversed(out))
    except Exception:
        return []


def sweep() -> dict:
    """Ingest new/changed files from every enabled folder. Fail-safe per folder;
    logs only sweeps that actually did something (quiet sweeps stay quiet)."""
    from . import rag
    totals = {"folders": 0, "added": 0, "updated": 0, "skipped": 0,
              "errors": []}
    for f in enabled_folders():
        totals["folders"] += 1
        try:
            res = rag.get_store().ingest_path(f["path"]) or {}
            if res.get("error"):
                totals["errors"].append(f"{f['path']}: {res['error'][:120]}")
                continue
            totals["added"] += int(res.get("added", 0))
            totals["updated"] += int(res.get("updated", 0))
            totals["skipped"] += int(res.get("skipped", 0))
        except Exception as exc:
            totals["errors"].append(f"{f['path']}: {type(exc).__name__}: {exc}")
            try:
                from . import issues
                issues.note_error("folderwatch", totals["errors"][-1])
            except Exception:
                pass
    if totals["added"] or totals["updated"] or totals["errors"]:
        record_run({"event": "sweep", **{k: totals[k] for k in
                                         ("folders", "added", "updated")},
                    "errors": totals["errors"][:3],
                    "summary": f"{totals['added']} added, "
                               f"{totals['updated']} updated"})
    return totals


def status() -> dict:
    cfg = load_config()
    return {"folders": cfg["folders"], "cadence": cfg.get("cadence", "hourly"),
            "schedule_id": cfg.get("schedule_id"),
            "log": recent_log(8)}
