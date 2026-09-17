"""Launch the Agent Jo web app (FastAPI backend + custom frontend).

    python run_web.py                  # serves on http://127.0.0.1:8000
    python run_web.py --port 9000
    python run_web.py --host 0.0.0.0   # expose on the local network

Run this from the project root so `web.server` is importable. On localhost it
falls back to a free port if the chosen one is busy, and opens your browser.
"""
import os
import sys


def _reexec_into_venv() -> None:
    """If this script was launched with the wrong Python (e.g. a plain
    `python run_web.py` from a shell where the project's virtualenv wasn't
    active), relaunch it using the venv's interpreter. This guarantees the
    server always runs from the environment that has the app's dependencies
    (uvicorn, faster-whisper, etc.), so optional features like voice input work
    without the "installed but the app can't see it" trap.

    Opt out with AGENT_NO_REEXEC=1. Safe and idempotent: it only switches once,
    only when a venv Python exists and differs from the current one.
    """
    if os.environ.get("AGENT_NO_REEXEC") or os.environ.get("_AGENT_REEXECED"):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    names = ("Scripts/python.exe", "Scripts/pythonw.exe", "bin/python",
             "bin/python3")
    for venv in (".venv", "venv", "env"):
        for name in names:
            cand = os.path.join(here, venv, *name.split("/"))
            if not os.path.isfile(cand):
                continue
            try:
                same = os.path.samefile(cand, sys.executable)
            except Exception:
                same = (os.path.normcase(os.path.abspath(cand))
                        == os.path.normcase(os.path.abspath(sys.executable)))
            if same:
                return                     # already the right interpreter
            env = dict(os.environ, _AGENT_REEXECED="1")
            print(f"  (relaunching with the project virtualenv: {cand})")
            try:
                os.execve(cand, [cand, os.path.abspath(__file__)] + sys.argv[1:],
                          env)
            except Exception as exc:       # fall through and run as-is
                print(f"  (could not switch interpreter: {exc}; continuing)")
                return
    # no venv found next to this script — run with whatever launched us


_reexec_into_venv()

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.bind(("127.0.0.1", 0))
                return s2.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description="Run the Agent Jo web app.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = ap.parse_args()

    local = args.host in ("127.0.0.1", "localhost")
    port = _free_port(args.port) if local else args.port
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{shown_host}:{port}"

    if not args.no_browser and local:
        threading.Thread(
            target=lambda: (time.sleep(1.3), webbrowser.open(url)),
            daemon=True).start()

    print(f"\n  Agent Jo  ->  {url}\n  (Ctrl+C to stop)\n")
    uvicorn.run("web.server:app", host=args.host, port=port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
