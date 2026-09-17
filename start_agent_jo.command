#!/bin/bash
# Start Agent Jo. Double-click on macOS, or run from a terminal.
cd "$(dirname "$0")" || exit 1

PY=""
for v in .venv venv env; do
  if [ -x "$v/bin/python" ]; then PY="$v/bin/python"; break; fi
done

if [ -z "$PY" ]; then
  echo
  echo "  No environment found next to this file."
  echo "  Expected: .venv/bin/python in"
  echo "    $(pwd)"
  echo
  echo "  Run install.command first."
  echo "  Press Enter to close."
  read -r _
  exit 1
fi

"$PY" -c "import faster_whisper" >/dev/null 2>&1 || {
  echo "  Note: voice input (faster-whisper) isn't installed. Everything"
  echo "  else works. To enable it:  $PY -m pip install faster-whisper"
  echo
}

# run_web.py picks a free port and opens the browser itself, so the two
# launchers behave the same way rather than each doing it differently
exec "$PY" run_web.py "$@"
