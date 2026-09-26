"""Copy the shared modules from Agent Jo into Agent Jo Jobs.

Agent Jo Jobs is a separate project, which means the 17 modules it needs
exist in two places. That is the cost of it being separate, and it is a real
one: fix the fabrication check here and the Jobs app still has the old one
until someone copies it across.

So the copying is one command, and the Jobs repo carries a manifest of what
it was synced from — hashes, not dates — so a stale copy is visible rather
than discovered when two apps disagree about whether a draft is honest.

    python tools/sync_jobs_app.py ../agent-jo-jobs          # copy
    python tools/sync_jobs_app.py ../agent-jo-jobs --check  # report only
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Every module the Jobs app imports directly. "engines" joined when the Jobs
# app grew its own engine management: the closure is computed from these, so a
# module missing here is a module missing from the standalone app — it failed
# to start with ModuleNotFoundError rather than anything subtler.
ENTRY = ("jobscout", "boards", "jobalerts", "cv", "portal", "outcomes",
         "brain", "config", "memory", "scheduler", "engines")
ALSO = [("jobs/server.py", "jobs/server.py"),
        # the window is identical in both now, icon included, so it syncs too
        ("web_jobs/index.html", "web_jobs/index.html"),
        ("web_jobs/jobs.css", "web_jobs/jobs.css"),
        ("web_jobs/jobs.js", "web_jobs/jobs.js"),
        ("run_jobs.py", "run_jobs.py")]


def closure() -> list:
    mods = {p.stem for p in (ROOT / "agent").glob("*.py")}
    seen = set()

    def walk(name):
        if name in seen or name not in mods:
            return
        seen.add(name)
        tree = ast.parse((ROOT / "agent" / f"{name}.py").read_text("utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level:
                if n.module:
                    walk(n.module.split(".")[0])
                for a in n.names:
                    walk(a.name)
    for e in ENTRY:
        walk(e)
    return sorted(seen)


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else ""


def report(dest: Path) -> dict:
    out = {"stale": [], "missing": [], "same": 0}
    for m in closure():
        a, b = ROOT / "agent" / f"{m}.py", dest / "agent" / f"{m}.py"
        if not b.exists():
            out["missing"].append(f"agent/{m}.py")
        elif _hash(a) != _hash(b):
            out["stale"].append(f"agent/{m}.py")
        else:
            out["same"] += 1
    for src, dst in ALSO:
        if dst and _hash(ROOT / src) != _hash(dest / dst):
            out["stale"].append(dst)
    return out


def sync(dest: Path) -> dict:
    (dest / "agent").mkdir(parents=True, exist_ok=True)
    copied = []
    for m in closure():
        a, b = ROOT / "agent" / f"{m}.py", dest / "agent" / f"{m}.py"
        if _hash(a) != _hash(b):
            shutil.copy2(a, b)
            copied.append(f"agent/{m}.py")
    for src, dst in ALSO:
        if dst and _hash(ROOT / src) != _hash(dest / dst):
            (dest / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / src, dest / dst)
            copied.append(dst)
    manifest = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "from_build": _build_id(),
        "files": {f"agent/{m}.py": _hash(ROOT / "agent" / f"{m}.py")
                  for m in closure()},
    }
    (dest / "VENDORED.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    return {"copied": copied, "manifest": "VENDORED.json"}


def _build_id() -> str:
    try:
        t = (ROOT / "agent" / "config.py").read_text("utf-8")
        import re
        m = re.search(r'BUILD_ID = "([^"]*)"', t)
        return m.group(1) if m else ""
    except Exception:
        return ""


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    dest = Path(argv[0]).resolve()
    if "--check" in argv:
        r = report(dest)
        print(f"  in step: {r['same']}")
        for f in r["stale"]:
            print(f"  STALE:   {f}")
        for f in r["missing"]:
            print(f"  MISSING: {f}")
        return 1 if (r["stale"] or r["missing"]) else 0
    r = sync(dest)
    print(f"  copied {len(r['copied'])} file(s) into {dest.name}")
    for f in r["copied"]:
        print(f"    {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
