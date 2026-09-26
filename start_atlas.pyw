"""Windowless launcher for the Agent Jo web UI.

Double-clicking this (or a desktop shortcut to it) starts the app using the
project's virtual environment and opens it in the browser — no terminal
window, no typing. The .pyw extension means Python runs it without a console.

It finds its own folder, prefers .venv\Scripts\pythonw.exe, and launches app.py.
If the venv is missing it falls back to whatever Python is on PATH.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"

# Prefer the project's virtual environment; pythonw.exe runs without a console.
candidates = [
    HERE / ".venv" / "Scripts" / "pythonw.exe",
    HERE / ".venv" / "Scripts" / "python.exe",
    HERE / "venv" / "Scripts" / "pythonw.exe",
]
python = next((str(c) for c in candidates if c.exists()), None)
if python is None:
    # Fall back to a non-venv interpreter (windowless variant if available).
    base = Path(sys.executable)
    pyw = base.with_name("pythonw.exe")
    python = str(pyw if pyw.exists() else base)

# Launch the app detached from this launcher; working dir = project folder.
creationflags = 0
if os.name == "nt":
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

subprocess.Popen([python, str(APP)], cwd=str(HERE), creationflags=creationflags)
