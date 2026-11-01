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


def _running_agent_jo(port: int) -> bool:
    """Is Agent Jo already answering on this port (the background service, or
    a window left open)? A second copy would serve a UI but must not run the
    schedules twice, so opening the one that's there is the better answer."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                    timeout=1.5) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return body.get("status") == "ok" and "app" in body
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Run the Agent Jo web app.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AGENT_WEB_PORT") or 8000))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    ap.add_argument("--strict-port", action="store_true",
                    help="fail if the port is busy instead of moving to a "
                         "free one (the background service uses this)")
    ap.add_argument("--new", action="store_true",
                    help="start a fresh copy even if Agent Jo is already "
                         "running in the background")
    args = ap.parse_args()

    local = args.host in ("127.0.0.1", "localhost")

    # the background service (tools/service.py) already serves the app:
    # open that rather than starting a second copy beside it
    if local and not args.strict_port and not args.new:
        for p in dict.fromkeys((args.port, 8765)):
            if _running_agent_jo(p):
                url = f"http://localhost:{p}"
                print(f"\n  Agent Jo is already running  ->  {url}\n"
                      f"  (opening it; use --new to start another copy)\n")
                if not args.no_browser:
                    webbrowser.open(url)
                return

    if args.strict_port:
        port = args.port
    else:
        port = _free_port(args.port) if local else args.port
    # tell the app which port it's on (the phone set-up page reads it)
    os.environ["AGENT_WEB_PORT"] = str(port)
    os.environ.setdefault("AGENT_WEB_HOST", args.host)
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
