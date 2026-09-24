"""Start Agent Jo Jobs.

Its own port (8766) so it can run alongside the main app rather than instead
of it — they share the data, the engines and the audit trail, so having both
open is the normal case, not a conflict.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8766


def _free_port(start: int, tries: int = 12) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent Jo Jobs")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn isn't installed. Run install.bat (or ./install.sh).")
        return 1

    from jobs.server import app, get_memory, start_scheduler
    get_memory()                       # same store the main app uses
    start_scheduler()                  # its own daily run, claimed so it
                                       # never doubles with the main app

    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"  Agent Jo Jobs -> {url}")
    print("  (leave this window open; Ctrl+C to stop)")

    if not args.no_browser:
        def _open():
            # wait for the server rather than opening into a refused
            # connection, which looks like the app is broken
            for _ in range(40):
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        webbrowser.open(url)
                        return
                time.sleep(0.25)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
