"""Entry point for the packaged Agent Jo web app (.exe).

PyInstaller freezes this into a single executable. It boots the FastAPI app
in-process (no string import, no reload - the most robust mode for a frozen
binary), picks a free local port, opens the browser, and serves until closed.

Build it with build_exe.bat (Windows). For plain Python use, prefer run_web.py.
"""
import multiprocessing
import socket
import threading
import time
import webbrowser

import uvicorn

from web.server import app


def _free_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.bind(("127.0.0.1", 0))
                return s2.getsockname()[1]


def main():
    multiprocessing.freeze_support()      # safe no-op except in frozen children
    port = _free_port(8000)
    url = f"http://localhost:{port}"
    threading.Thread(
        target=lambda: (time.sleep(1.4), webbrowser.open(url)),
        daemon=True).start()
    print("\n  ============================================")
    print(f"   Agent Jo is running at  {url}")
    print("   Keep this window open. Close it to stop.")
    print("  ============================================\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
