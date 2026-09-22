#!/bin/bash
# Agent Jo Jobs. Double-click on macOS, or run from a terminal.
cd "$(dirname "$0")" || exit 1
PY=""
for v in .venv venv env; do
  if [ -x "$v/bin/python" ]; then PY="$v/bin/python"; break; fi
done
if [ -z "$PY" ]; then
  echo "  No environment found next to this file. Run install.command first."
  echo "  Press Enter to close."
  read -r _
  exit 1
fi
exec "$PY" run_jobs.py "$@"
