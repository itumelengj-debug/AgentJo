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
# ...and whatever the Jobs app's own files import from agent/, read from the
# files themselves. The list above is kept by hand, and it went stale the
# moment the server imported a new module (localguard): the sync would have
# shipped a server that couldn't start. The imports can't go stale.
APP_FILES = ("jobs/server.py", "run_jobs.py")
ALSO = [("jobs/server.py", "jobs/server.py"),
        # the window is identical in both now, icon included, so it syncs too
        ("web_jobs/index.html", "web_jobs/index.html"),
        ("web_jobs/jobs.css", "web_jobs/jobs.css"),
        ("web_jobs/jobs.js", "web_jobs/jobs.js"),
        ("run_jobs.py", "run_jobs.py"),
        # the helper is where applications happen, so its check goes too
        ("tools/check_helper.py", "tools/check_helper.py"),
        ("tools/check_layout.py", "tools/check_layout.py")]


def _app_imports() -> set:
    """Every agent module the Jobs app's own files import, lazy ones included."""
    names = set()
    for rel in APP_FILES:
        f = ROOT / rel
        if not f.exists():
            continue
        for n in ast.walk(ast.parse(f.read_text("utf-8"))):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[1] for a in n.names
                             if a.name.startswith("agent."))
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                if n.module == "agent":
                    names.update(a.name for a in n.names)
                elif n.module.startswith("agent."):
                    names.add(n.module.split(".")[1])
    return names


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
    for e in sorted(set(ENTRY) | _app_imports()):
        walk(e)
    return sorted(seen)


def _hash(p: Path) -> str:
    """Of the text with line endings normalised: git on Windows may check a
    file out with CRLF, and that is not a change to the code."""
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]


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
        # every file the sync owns, not just the modules: the server, the
        # window and the launcher are copies too, and the Jobs app's own
        # tests check each one against this, so a hand edit over there shows
        "files": {**{f"agent/{m}.py": _hash(ROOT / "agent" / f"{m}.py")
                     for m in closure()},
                  **{dst: _hash(ROOT / src) for src, dst in ALSO}},
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
