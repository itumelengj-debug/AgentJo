#!/usr/bin/env python3
"""Run Agent Jo in the background — it starts at log-in and restarts if it stops.

    python tools/service.py install [--port 8765]   start now and at every log-in
    python tools/service.py status
    python tools/service.py uninstall
    python tools/service.py print                   show the definition, change nothing

Linux uses a systemd user unit, macOS a launchd agent, Windows a Task
Scheduler task. All run as you; none needs an administrator. Once it's
installed, open http://localhost:8765 — or just use the usual Start Agent Jo
shortcut, which opens the running copy instead of starting a second one.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import service  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["install", "uninstall", "status", "print"])
    ap.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    ap.add_argument("--no-start", action="store_true",
                    help="install, but don't start it until the next log-in")
    args = ap.parse_args(argv)

    if args.action == "print":
        kind = service.system()
        py = service.python_for_service(windowless=(kind == "windows"))
        text = {"linux": service.render_systemd, "macos": service.render_launchd,
                "windows": service.render_windows_task}.get(kind)
        if not text:
            print(f"No service support for {kind}.")
            return 1
        print(f"# would be written to {service.definition_path(kind)}\n")
        print(text(py, args.port))
        return 0
    if args.action == "install":
        r = service.install(port=args.port, start=not args.no_start)
    elif args.action == "uninstall":
        r = service.uninstall()
    else:
        r = service.status()
    print(json.dumps(r, indent=2))
    if args.action == "install" and r.get("ok"):
        print(f"\n  Agent Jo is running in the background  ->  {r['url']}\n")
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
