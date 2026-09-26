"""Build a shareable copy of Agent Jo.

The risk in handing this app to someone is not that it won't run — it's what
travels with it. A careless zip of the working folder would carry the API key,
177 memories, the audit trail, the job profile built from a CV, crew notes and
pipeline specs. So this doesn't copy-and-exclude; it copies ONLY what is on an
allowlist, and then re-opens the finished archive and scans it for anything
that looks like a credential or personal data. If the scan finds something the
build fails rather than warns.

    python tools/make_release.py            -> dist/AgentJo-<date>.zip

The recipient unzips it, runs install.bat once, and gets a desktop shortcut.
They supply their own engine — the first-run screen asks for it.
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Allowlist: only these are shipped. Anything new has to be added here on
# purpose, which is the point — a denylist silently ships whatever it forgot.
# The test suite is deliberately NOT shipped: the recipient has no use for it,
# it doubles the download, and its fixtures contain key-shaped strings that
# (correctly) trip the leak scanner. Caught by that scanner on the first real
# build, which is the scanner earning its place.
INCLUDE_DIRS = [
    "agent", "web",
    # Agent Jo Jobs ships with it: one download, two windows
    "jobs", "web_jobs",
]
INCLUDE_FILES = [
    "README.md", "CHANGELOG.md", "UPGRADING.md", "LICENSE", "COMMERCIAL.md",
    "install.bat", "get-python.ps1", "install_agent_jo.py",
    "install.command", "install.sh",
    "run_jobs.py", "start_agent_jo_jobs.bat",
    "start_agent_jo_jobs.command", "start_agent_jo_jobs.sh",
    "start_agent_jo.command", "start_agent_jo.sh",
    "VERIFYING.md", "SIGNING.md",
    "requirements.txt", "requirements-web.txt",
    "requirements-rag.txt", "requirements-voice.txt", "requirements-app.txt",
    "run_web.py", "run.py", "start_agent_jo.bat", "app.py",
    "agent_avatar.png", "atlas.ico",
]
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".zip"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules",
             "dist", "build", ".pytest_cache", "outputs"}

# Anything matching these in the built archive means the build leaked.
LEAK_PATTERNS = [
    (re.compile(rb"sk-ant-[A-Za-z0-9_\-]{10,}"), "an Anthropic API key"),
    (re.compile(rb"sk-proj-[A-Za-z0-9_\-]{10,}"), "an OpenAI key"),
    (re.compile(rb"ghp_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(rb"AKIA[0-9A-Z]{16}"), "an AWS key"),
]
LEAK_FILENAMES = ["secret.key", "credentials.json", "engines.json",
                  "mcp.json", "email.json", "audit.jsonl", "problems.jsonl",
                  "agent.db", "documents.db", "settings.json",
                  "roles.json", "profile.json", "members.json"]


def _skip(rel: Path) -> bool:
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    return rel.suffix.lower() in SKIP_SUFFIX


def collect() -> list:
    out = []
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                rel = f.relative_to(ROOT)
                if not _skip(rel):
                    out.append(rel)
    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.exists() and p.is_file():
            out.append(p.relative_to(ROOT))
    return sorted(set(out))


INSTALL_BAT = r"""@echo off
setlocal
title Agent Jo - install
echo.
echo   Installing Agent Jo
echo   ===================
echo.

REM --- find a usable Python -------------------------------------------------
set "PY="
for %%C in (py python) do (
  if not defined PY (
    %%C -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=%%C"
  )
)
if not defined PY (
  echo   Python 3.10 or newer is required and wasn't found.
  echo.
  echo   Install it from https://www.python.org/downloads/
  echo   IMPORTANT: tick "Add python.exe to PATH" in the installer.
  echo.
  pause
  exit /b 1
)

echo   Using Python: %PY%
echo   Creating a private environment (this takes a minute)...
%PY% -m venv "%~dp0.venv" || goto :failed

echo   Installing components...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" --quiet || goto :failed
if exist "%~dp0requirements-web.txt" "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-web.txt" --quiet

REM --- desktop shortcut -----------------------------------------------------
set "LNK=%USERPROFILE%\Desktop\Agent Jo.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -COM WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%~dp0start_agent_jo.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "if (Test-Path '%~dp0atlas.ico') { $s.IconLocation='%~dp0atlas.ico' };" ^
  "$s.Description='Agent Jo';$s.Save()" >nul 2>&1

echo.
echo   Done. There's an "Agent Jo" shortcut on your desktop.
echo.
echo   The first time it opens it will ask how it should think:
echo     - an Anthropic API key (paid, best quality), or
echo     - Ollama (free and fully private, from ollama.com)
echo.
echo   Starting it now...
timeout /t 2 >nul
start "" "%~dp0start_agent_jo.bat"
exit /b 0

:failed
echo.
echo   Install failed. The messages above say why - usually no internet
echo   connection, or Python missing "Add to PATH".
echo.
pause
exit /b 1
"""

READ_ME_FIRST = """# Agent Jo

A private AI agent that runs entirely on this computer.

## Install (once)

Double-click **install.bat**.

It creates a self-contained environment, puts an **Agent Jo** shortcut on the
desktop, and starts the app. It needs Python 3.10+ — if it's missing, the
installer says so and links to it (tick *Add python.exe to PATH* when
installing Python).

## First run

The app asks how it should think:

* **Anthropic (Claude)** — best quality, paid per use. Get a key at
  platform.claude.com. It's sealed on this machine and never sent anywhere
  except Anthropic.
* **Ollama** — free and fully private, nothing leaves the computer. Install
  from ollama.com, then run `ollama pull qwen3`.

Either can be changed later, and both can be used together.

## Where your data lives

Everything — conversations, memories, documents, settings — stays in
`.local_agent` in your user folder. Nothing is uploaded. To remove the app,
delete this folder and that one.

## This copy carries no personal data

It ships with no API key, no conversations and no memories. Whoever shared it
built it with a tool that removes those and refuses to package if any are
found.
"""


def build() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d")
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    zip_path = out_dir / f"AgentJo-{stamp}.zip"
    if zip_path.exists():
        zip_path.unlink()
    files = collect()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for rel in files:
            z.write(ROOT / rel, str(rel).replace("\\", "/"))
        # The real install.bat is collected from the repo above. This used to
        # write an inline copy over it, which silently shipped a superseded
        # installer — exactly the class of bug that makes an installer "work
        # on my machine" and nowhere else.
        z.writestr("READ ME FIRST.md", READ_ME_FIRST)
    return zip_path


def audit(zip_path: Path) -> list:
    """Re-open the finished archive and look for anything that shouldn't be
    in it. Checking the build inputs isn't enough — what matters is what's
    actually in the file about to be handed over."""
    problems = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name.lower()
            if name in LEAK_FILENAMES:
                problems.append(f"{info.filename} — personal data file")
                continue
            if info.file_size > 2_000_000:
                continue
            try:
                blob = z.read(info.filename)
            except Exception:
                continue
            for pattern, what in LEAK_PATTERNS:
                if pattern.search(blob):
                    problems.append(f"{info.filename} — looks like {what}")
                    break
    return problems


def main() -> int:
    print("Collecting files…")
    zip_path = build()
    size = zip_path.stat().st_size / 1e6
    with zipfile.ZipFile(zip_path) as z:
        count = len(z.namelist())
    print(f"Packaged {count} files -> {zip_path.name} ({size:.1f} MB)")

    print("Scanning the archive for anything personal…")
    problems = audit(zip_path)
    if problems:
        zip_path.unlink(missing_ok=True)
        print("\nBUILD FAILED — the archive contained things that must not be "
              "shared:")
        for p in problems[:20]:
            print("  -", p)
        print("\nNothing was written. Fix the above and run this again.")
        return 1
    print("Clean: no keys, no conversations, no memories.")
    print(f"\nShare this file: {zip_path}")
    print("They unzip it and double-click install.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
