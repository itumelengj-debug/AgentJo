#!/bin/bash
# Agent Jo — install on macOS. Double-click this file.
#
# macOS runs a .command file from Finder, which .sh doesn't do — that's the
# only reason this exists separately. It does as little as possible: find a
# Python, then hand over to install_agent_jo.py where failures can explain
# themselves.
cd "$(dirname "$0")" || exit 1

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python)"

if [ -z "$PY" ]; then
  echo
  echo "  Agent Jo needs Python 3.10 or newer, and this Mac doesn't have it."
  echo
  if command -v brew >/dev/null 2>&1; then
    echo "  Homebrew is installed, so I can do this for you."
    printf "  Install Python 3.12 via Homebrew? (y/n) "
    read -r a
    case "$a" in
      y|Y|yes|YES)
        brew install python@3.12 || {
          echo "  Homebrew couldn't install it. See the output above."; exit 1; }
        PY="$(find_python)"
        ;;
      *)
        echo "  Nothing was changed."
        echo "  Install Python from https://www.python.org/downloads/macos/"
        echo "  then run this again."
        exit 1 ;;
    esac
  else
    # Rather than silently installing Homebrew — a large thing to add to
    # someone's machine without asking — point at the official installer.
    echo "  Install Python from https://www.python.org/downloads/macos/"
    echo "  (the universal2 installer covers both Apple Silicon and Intel),"
    echo "  then run this again."
    echo
    echo "  Or, if you'd rather use Homebrew:"
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    brew install python@3.12"
    exit 1
  fi
fi

[ -z "$PY" ] && { echo "  Python still isn't visible. Open a new Terminal and try again."; exit 1; }

"$PY" install_agent_jo.py "$@"
rc=$?

if [ $rc -ne 0 ]; then
  echo
  echo "  Setup didn't finish. install.log has every command and its output."
  echo "  Press Enter to close."
  read -r _
  exit $rc
fi

# Don't offer to start when there's nobody to answer: --check is a survey,
# and a prompt with no terminal attached waits forever. That hung a test run
# for five minutes, which is exactly what it would do in a CI job or an
# automated install.
case " $* " in *" --check "*) exit 0 ;; esac
if [ ! -t 0 ]; then
  echo
  echo "  Run ./start_agent_jo.command when you're ready."
  exit 0
fi

echo
printf "  Start Agent Jo now? (y/n) "
read -r go
case "$go" in y|Y|yes|YES) exec ./start_agent_jo.command ;; esac
