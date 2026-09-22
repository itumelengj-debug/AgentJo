"""Setting Agent Jo up — and saying exactly what failed when it doesn't.

The previous installer was 192 lines of PowerShell that did one thing (find
Python) and gave up quietly on anything else. It worked on the machine it was
written for, which is the usual way an installer fails: the second machine has
a different Windows build, no winget, a proxy, an antivirus, a locked-down
PowerShell policy, or simply no Ollama — and the script exits with nothing to
go on.

So the shape is different now. PowerShell does only the irreducible part
(getting a Python onto a machine that has none). Everything after that is
here, in Python, where it can be tested and where a failure can explain
itself.

Three principles:

**Check, then act.** Every step reports what it found before it changes
anything, and `--check` does the whole survey while changing nothing at all.

**A failure names its own fix.** "pip install failed" is useless on someone
else's machine. "pip couldn't reach pypi.org — if you're behind a corporate
proxy, set HTTPS_PROXY and run this again" is a thing you can act on.

**Everything is logged.** install.log records every command and its output, so
a failure on a machine I can't see is still diagnosable from the file.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
# The installer used to tell a Mac user to run install.bat. Small, and the
# kind of small that makes someone conclude the thing isn't meant for them.
LAUNCHER = ("install.bat" if IS_WIN
            else "install.command" if IS_MAC else "./install.sh")
STARTER = ("start_agent_jo.bat" if IS_WIN
           else "./start_agent_jo.command" if IS_MAC
           else "./start_agent_jo.sh")
LOG = HERE / "install.log"
VENV = HERE / ".venv"
MIN_PY = (3, 10)

OK, WARN, FAIL = "ok", "warn", "fail"
_results = []


# --------------------------------------------------------------------------- #
#  saying things
# --------------------------------------------------------------------------- #
def _c(text: str, colour: str = "") -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    codes = {"green": "92", "yellow": "93", "red": "91", "dim": "90",
             "bold": "1", "cyan": "96"}
    return f"\033[{codes.get(colour, '0')}m{text}\033[0m"


def log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} "
                     f"{line}\n")
    except Exception:
        pass


def step(name: str, state: str, detail: str = "", fix: str = "") -> dict:
    mark = {OK: _c("  ok  ", "green"), WARN: _c(" warn ", "yellow"),
            FAIL: _c(" fail ", "red")}[state]
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if fix and state != OK:
        print(f"         {_c(fix, 'dim')}")
    log(f"{state.upper()} {name} :: {detail} :: {fix}")
    r = {"name": name, "state": state, "detail": detail, "fix": fix}
    _results.append(r)
    return r


def run(cmd: list, timeout: int = 900) -> tuple:
    """Run a command, capture everything, never raise."""
    log(f"RUN {' '.join(str(c) for c in cmd)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        log(f"  exit={p.returncode} {out[-1500:]}")
        return p.returncode, out
    except FileNotFoundError:
        log("  not found")
        return 127, f"{cmd[0]} isn't on this machine"
    except subprocess.TimeoutExpired:
        log("  timeout")
        return 124, f"timed out after {timeout}s"
    except Exception as exc:
        log(f"  {type(exc).__name__}: {exc}")
        return 1, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
#  what's here already
# --------------------------------------------------------------------------- #
def venv_python() -> Path:
    return (VENV / ("Scripts" if os.name == "nt" else "bin")
            / ("python.exe" if os.name == "nt" else "python"))


def check_python() -> bool:
    v = sys.version_info
    if v[:2] < MIN_PY:
        step("Python", FAIL,
             f"{v.major}.{v.minor} — too old",
             f"Agent Jo needs {MIN_PY[0]}.{MIN_PY[1]} or newer. Install from "
             f"python.org and tick 'Add python.exe to PATH'.")
        return False
    step("Python", OK, f"{v.major}.{v.minor}.{v.micro} at {sys.executable}")
    return True


def check_internet() -> bool:
    """Reachability, before blaming pip for a network problem."""
    try:
        socket.create_connection(("pypi.org", 443), timeout=6).close()
        step("Internet", OK, "pypi.org is reachable")
        return True
    except Exception as exc:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        step("Internet", FAIL, f"can't reach pypi.org ({type(exc).__name__})",
             ("A proxy is set but isn't working — check HTTPS_PROXY."
              if proxy else
              "If you're on a corporate network, set HTTPS_PROXY to your "
              "proxy and run this again. Otherwise check the connection."))
        return False


def check_disk() -> bool:
    try:
        free = shutil.disk_usage(HERE).free / 1e9
    except Exception:
        return True
    if free < 2:
        step("Disk space", FAIL, f"{free:.1f} GB free",
             "The environment needs about 1.5 GB. Free some space first.")
        return False
    step("Disk space", OK, f"{free:.1f} GB free")
    return True


def check_long_paths() -> None:
    """Windows' 260-character limit breaks pip in deep folders — silently."""
    if os.name != "nt":
        return
    depth = len(str(HERE))
    if depth > 80:
        step("Folder depth", WARN,
             f"this folder's path is {depth} characters",
             "Windows caps paths at 260 characters and pip fails oddly near "
             "it. If installing packages misbehaves, move this folder "
             "somewhere shorter like C:\\AgentJo.")
    else:
        step("Folder depth", OK, f"{depth} characters — fine")


# --------------------------------------------------------------------------- #
#  the environment
# --------------------------------------------------------------------------- #
def make_venv(check_only: bool = False) -> bool:
    if venv_python().exists():
        step("Environment", OK, "already set up (.venv)")
        return True
    if check_only:
        step("Environment", WARN, "not created yet",
             f"Run {LAUNCHER} without --check to create it.")
        return False
    code, out = run([sys.executable, "-m", "venv", str(VENV)])
    if code or not venv_python().exists():
        step("Environment", FAIL, "couldn't create .venv",
             ("On Debian/Ubuntu: sudo apt install python3-venv. "
              "On Windows this usually means the Python install is damaged — "
              "reinstall from python.org.")
             + f"\n         {out.strip()[:200]}")
        return False
    step("Environment", OK, "created .venv — nothing installed system-wide")
    return True


def install_packages(check_only: bool = False) -> bool:
    py = venv_python()
    if not py.exists():
        return False
    req = HERE / "requirements.txt"
    if check_only:
        code, out = run([str(py), "-c",
                         "import fastapi, anthropic, httpx; print('present')"])
        if code == 0:
            step("Packages", OK, "the core ones are installed")
            return True
        step("Packages", WARN, "not installed yet",
             f"Run {LAUNCHER} without --check.")
        return False

    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    args = ([str(py), "-m", "pip", "install", "-r", str(req)]
            if req.exists() else
            [str(py), "-m", "pip", "install",
             "anthropic", "openai", "httpx", "fastapi", "uvicorn[standard]",
             "rich", "python-multipart", "pypdf", "python-docx", "openpyxl",
             "python-pptx", "beautifulsoup4", "cryptography"])
    code, out = run(args, timeout=1800)
    if code == 0:
        step("Packages", OK, "installed")
        return True

    # name the actual cause rather than dumping pip's output
    low = out.lower()
    if "proxy" in low or "timed out" in low or "retries exceeded" in low:
        fix = ("pip couldn't reach the internet. Behind a corporate proxy, "
               "set HTTPS_PROXY and run this again.")
    elif "microsoft visual c++" in low or "build wheel" in low:
        fix = ("A package needs a compiler. Install the Microsoft C++ Build "
               "Tools, or tell me which package failed and I'll find a "
               "wheel-only alternative.")
    elif "no space" in low:
        fix = "The disk filled up."
    elif "permission" in low or "access is denied" in low:
        fix = ("Windows blocked a file — usually antivirus. Allow the folder "
               "in your antivirus and run this again.")
    else:
        fix = f"See install.log. Last of the output: {out.strip()[-300:]}"
    step("Packages", FAIL, "pip failed", fix)
    return False


def check_app() -> bool:
    """Does it actually start? An install that finishes but can't run is
    worse than one that fails, because you find out later."""
    py = venv_python()
    if not py.exists():
        return False
    # both apps, because a release that installs cleanly and then can't open
    # the job search has only half worked
    code, out = run([str(py), "-c",
                     "import sys; sys.path.insert(0, r'%s'); "
                     "import agent.config, web.server, jobs.server; "
                     "print('ok')" % HERE],
                    timeout=180)
    if code == 0 and "ok" in out:
        step("Agent Jo", OK, "loads cleanly")
        return True
    step("Agent Jo", FAIL, "the app doesn't load",
         f"See install.log. {out.strip()[-300:]}")
    return False


# --------------------------------------------------------------------------- #
#  optional, and asked for rather than assumed
# --------------------------------------------------------------------------- #
def check_ollama(offer: bool = True) -> None:
    """Local models. Optional — but it's the difference between free and not."""
    exe = shutil.which("ollama")
    if exe:
        code, out = run([exe, "list"], timeout=30)
        if code == 0:
            models = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
            step("Ollama", OK,
                 f"installed with {len(models)} model(s)" if models
                 else "installed, no models pulled yet",
                 "" if models else
                 "Run: ollama pull qwen3:8b  — then scoring and drafting "
                 "cost nothing.")
            return
    step("Ollama", WARN, "not installed",
         "Optional. Without it every call goes to a paid API. "
         "Get it from ollama.com, then: ollama pull qwen3:8b")


def check_playwright(check_only: bool = False) -> None:
    py = venv_python()
    if not py.exists():
        return
    code, _ = run([str(py), "-c", "import playwright; print('y')"],
                  timeout=60)
    if code != 0:
        step("Browser control", WARN, "Playwright not installed",
             f"Only needed for portal applications and JavaScript-heavy "
             f"pages. To add it:  {venv_python()} -m pip install playwright "
             f"&& {venv_python()} -m playwright install chromium")
        return
    code2, out2 = run([str(py), "-m", "playwright", "install", "--dry-run",
                       "chromium"], timeout=60)
    if "is already installed" in out2 or code2 == 0:
        step("Browser control", OK, "Playwright ready")
    else:
        step("Browser control", WARN, "Playwright installed, no browser",
             f"{venv_python()} -m playwright install chromium")


def check_port(port: int = 8765) -> None:
    try:
        s = socket.socket()
        s.settimeout(1)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
    except Exception:
        return
    if busy:
        step("Port 8765", WARN, "something is already listening",
             "Agent Jo may already be running — check your browser at "
             "http://127.0.0.1:8765 before starting another copy.")
    else:
        step("Port 8765", OK, "free")


def make_shortcut() -> None:
    if os.name != "nt":
        return
    try:
        import winreg  # noqa: F401
        desktop = Path(os.path.expanduser("~")) / "Desktop"
        if not desktop.exists():
            return
        target = HERE / "start_agent_jo.bat"
        vbs = HERE / "_mkshortcut.vbs"
        vbs.write_text(
            'Set s = CreateObject("WScript.Shell")\n'
            f'Set l = s.CreateShortcut("{desktop / "Agent Jo.lnk"}")\n'
            f'l.TargetPath = "{target}"\n'
            f'l.WorkingDirectory = "{HERE}"\n'
            'l.Description = "Agent Jo - by Symbolic Synapse"\n'
            'l.Save\n', "utf-8")
        run(["cscript", "//nologo", str(vbs)], timeout=30)
        vbs.unlink(missing_ok=True)
        step("Desktop shortcut", OK, "created")
    except Exception as exc:
        step("Desktop shortcut", WARN, f"couldn't create one ({exc})",
             "Run start_agent_jo.bat from this folder instead.")


# --------------------------------------------------------------------------- #
#  the whole thing
# --------------------------------------------------------------------------- #
def main(argv: list = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    check_only = "--check" in argv
    quiet = "--quiet" in argv
    _results.clear()

    log("=" * 60)
    log(f"setup start · {platform.platform()} · python {sys.version}")

    if not quiet:
        print()
        print(_c("  Agent Jo", "bold"))
        print(_c("  by Symbolic Synapse", "dim"))
        print()
        print(_c("  Checking this machine…" if check_only
                 else "  Setting up…", "cyan"))
        print()

    # things that stop everything
    hard = check_python() and check_disk()
    check_long_paths()
    online = check_internet()

    if hard and (online or venv_python().exists()):
        if make_venv(check_only):
            install_packages(check_only)
            check_app()
            check_playwright(check_only)
    check_ollama()
    check_port()
    if not check_only and venv_python().exists():
        make_shortcut()

    fails = [r for r in _results if r["state"] == FAIL]
    warns = [r for r in _results if r["state"] == WARN]

    print()
    print(_c("  " + "-" * 56, "dim"))
    if fails:
        print(_c(f"  Not ready — {len(fails)} thing(s) need fixing:", "red"))
        for r in fails:
            print(f"    • {r['name']}: {r['detail']}")
        print()
        print(_c(f"  Every command and its output is in {LOG.name}. If none "
                 f"of\n  the fixes above work, that file says exactly what "
                 f"happened.", "dim"))
    else:
        print(_c("  Ready." if not check_only else "  Everything checks out.",
                 "green"))
        if warns:
            print(_c(f"  {len(warns)} optional thing(s) not set up — the app "
                     f"runs without them.", "dim"))
        if not check_only:
            print()
            if IS_WIN:
                print("  Start it:  double-click 'Agent Jo' on your desktop,")
                print(f"             or run {STARTER} in this folder.")
            elif IS_MAC:
                print(f"  Start it:  double-click {STARTER.lstrip('./')} "
                      f"in this folder.")
            else:
                print(f"  Start it:  {STARTER}")
            print(_c("  It opens at http://127.0.0.1:8765", "dim"))
            print()
            print("  The job search is its own app:")
            print(f"    {'start_agent_jo_jobs.bat' if IS_WIN else './start_agent_jo_jobs.sh'}"
                  f"  \u2192 http://127.0.0.1:8766")

    # a machine-readable copy, so a failure elsewhere can be sent to me
    try:
        (HERE / "install-report.json").write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "python": sys.version,
            "results": _results,
        }, indent=2), "utf-8")
    except Exception:
        pass

    print()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
