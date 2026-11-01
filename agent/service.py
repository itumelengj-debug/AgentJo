"""Background service — Agent Jo keeps working when the window is closed.

Until now every schedule, watcher, folder sweep and auto-resume ran as a
thread inside whichever window you happened to have open. Close it, log out
or restart the PC, and all of it stopped, silently, until you next opened the
app. "Works unattended" only held while you were attending to it.

This turns the web server into an operating-system service that starts when
you log in and is restarted if it dies:

    Linux    a systemd *user* unit      ~/.config/systemd/user/agent-jo.service
    macOS    a launchd agent            ~/Library/LaunchAgents/com.symbolicsynapse.agentjo.plist
    Windows  a Task Scheduler task      "Agent Jo" (at log-on, restart on failure)

All three run as YOU, not as root or SYSTEM, so the service sees the same
data folder, the same keys and the same permissions as the window does, and
installing it never needs an administrator.

Two copies must never both run the schedules — a scheduled email would go out
twice. So the scheduler takes a lock (``acquire_scheduler_lock``). Whichever
process gets it first runs the schedules; any other copy serves its UI and
leaves the schedules alone, and says so.

The unit/plist/XML text is produced by pure functions so it can be tested on
any OS; ``install`` and ``uninstall`` are the only parts that touch the system.

    python tools/service.py install     # start now and at every log-in
    python tools/service.py status
    python tools/service.py uninstall
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from . import config

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.symbolicsynapse.agentjo"
UNIT = "agent-jo.service"
TASK = "Agent Jo"
DEFAULT_PORT = 8765


# --------------------------------------------------------------------------- #
# The scheduler lock: one process runs the schedules, however many are open.
# --------------------------------------------------------------------------- #
_LOCK = {"fh": None, "path": None}


def _lock_path() -> Path:
    return Path(config.AGENT_HOME) / "scheduler.lock"


def acquire_scheduler_lock() -> bool:
    """Try to become the one process that runs schedules. Returns True if this
    process holds the lock (now or already). The OS releases it when the
    process exits, however it exits, so a crash never leaves it stuck."""
    path = _lock_path()
    if _LOCK["fh"] is not None and _LOCK["path"] == str(path):
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return True          # can't even make the file: don't block scheduling
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    _LOCK["fh"], _LOCK["path"] = fh, str(path)
    return True


def release_scheduler_lock() -> None:
    fh = _LOCK["fh"]
    _LOCK["fh"] = _LOCK["path"] = None
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


def scheduler_holder() -> str:
    """The pid written by whoever holds the lock, for messages ('' if none)."""
    try:
        return _lock_path().read_text().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# What to run
# --------------------------------------------------------------------------- #
def python_for_service(windowless: bool = False) -> str:
    """The project's own virtualenv if there is one, else this interpreter."""
    names = (["Scripts/pythonw.exe"] if windowless else []) + [
        "Scripts/python.exe", "bin/python", "bin/python3"]
    for venv in (".venv", "venv", "env"):
        for name in names:
            cand = ROOT / venv / name
            if cand.is_file():
                return str(cand)
    exe = Path(sys.executable)
    if windowless and exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.is_file():
            return str(w)
    return str(exe)


def command(python: str, port: int = DEFAULT_PORT) -> list[str]:
    """The service's command line. --strict-port: a service that quietly moved
    to another port would be one nobody can find; failing lets the supervisor
    retry until the port is free."""
    return [python, str(ROOT / "run_web.py"), "--no-browser", "--strict-port",
            "--port", str(int(port))]


def log_dir() -> Path:
    return Path(config.AGENT_HOME) / "logs"


# --------------------------------------------------------------------------- #
# Pure renderers — testable anywhere
# --------------------------------------------------------------------------- #
def _sd_quote(arg: str) -> str:
    if arg and all(c not in arg for c in ' \t"\'\\'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(python: str, port: int = DEFAULT_PORT,
                   home: str | None = None) -> str:
    home = home or str(config.AGENT_HOME)
    exec_line = " ".join(_sd_quote(a) for a in command(python, port))
    return f"""[Unit]
Description=Agent Jo — local AI agent (schedules, watchers, web UI)
After=network-online.target
Wants=network-online.target
# keep retrying for as long as it takes (see Restart= below)
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={_sd_quote(str(ROOT))}
ExecStart={exec_line}
Environment=AGENT_HOME={_sd_quote(home)}
Environment=AGENT_WEB_PORT={int(port)}
Environment=AGENT_NO_REEXEC=1
Environment=PYTHONUNBUFFERED=1
Restart=always
# a busy port (a window still open on it) or a crash: wait and try again,
# for as long as it takes, rather than giving up after a few attempts
RestartSec=30

[Install]
WantedBy=default.target
"""


def render_launchd(python: str, port: int = DEFAULT_PORT,
                   home: str | None = None) -> str:
    home = home or str(config.AGENT_HOME)
    logs = log_dir() if home == str(config.AGENT_HOME) else Path(home) / "logs"
    args = "\n".join(f"        <string>{_xml_escape(a)}</string>"
                     for a in command(python, port))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(ROOT))}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AGENT_HOME</key>
        <string>{_xml_escape(home)}</string>
        <key>AGENT_WEB_PORT</key>
        <string>{int(port)}</string>
        <key>AGENT_NO_REEXEC</key>
        <string>1</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(logs / "service.out.log"))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(logs / "service.err.log"))}</string>
</dict>
</plist>
"""


def _win_quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def render_windows_task(python: str, port: int = DEFAULT_PORT,
                        user: str | None = None) -> str:
    """Task Scheduler XML: at log-on, as you, no time limit, restart on
    failure every minute up to 999 times, and never two copies at once."""
    cmd = command(python, port)
    user = user or os.environ.get("USERNAME", "")
    who = f"\n      <UserId>{_xml_escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Agent Jo keeps its schedules, watchers and web UI running in the background.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>{who}
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{who}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(cmd[0])}</Command>
      <Arguments>{_xml_escape(" ".join(_win_quote(a) for a in cmd[1:]))}</Arguments>
      <WorkingDirectory>{_xml_escape(str(ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# --------------------------------------------------------------------------- #
# Installing — the only part that touches the system
# --------------------------------------------------------------------------- #
def system() -> str:
    s = platform.system().lower()
    return {"darwin": "macos", "windows": "windows"}.get(s, "linux" if s == "linux" else s)


def definition_path(kind: str | None = None) -> Path | None:
    kind = kind or system()
    if kind == "linux":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "systemd" / "user" / UNIT
    if kind == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if kind == "windows":
        return Path(config.AGENT_HOME) / "service" / "agent-jo-task.xml"
    return None


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, out


def _audit(name: str, detail: str) -> None:
    try:
        from . import audit
        audit.record("config", name=name, detail=detail[:200])
    except Exception:
        pass


def install(port: int = DEFAULT_PORT, start: bool = True) -> dict:
    kind = system()
    path = definition_path(kind)
    if path is None:
        return {"ok": False, "error": f"No service support for {platform.system()} yet."}
    log_dir().mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    steps = []
    if kind == "linux":
        if not shutil.which("systemctl"):
            return {"ok": False, "error": "systemd isn't available here (no systemctl)."}
        path.write_text(render_systemd(python_for_service(), port), "utf-8")
        steps.append(_run(["systemctl", "--user", "daemon-reload"]))
        steps.append(_run(["systemctl", "--user", "enable"] + (["--now"] if start else []) + [UNIT]))
        # without lingering, a user unit stops at log-out; ask, don't require
        if shutil.which("loginctl"):
            _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    elif kind == "macos":
        path.write_text(render_launchd(python_for_service(), port), "utf-8")
        _run(["launchctl", "unload", str(path)])
        if start:
            steps.append(_run(["launchctl", "load", "-w", str(path)]))
    elif kind == "windows":
        xml = render_windows_task(python_for_service(windowless=True), port)
        path.write_text(xml, encoding="utf-16")
        steps.append(_run(["schtasks", "/Create", "/TN", TASK, "/XML", str(path), "/F"]))
        if start:
            steps.append(_run(["schtasks", "/Run", "/TN", TASK]))
    failed = [out for ok, out in steps if not ok]
    _audit("service_install", f"{kind} port {port}" + (f" — failed: {failed[0]}" if failed else ""))
    if failed:
        return {"ok": False, "error": failed[0][:400], "definition": str(path), "kind": kind}
    return {"ok": True, "kind": kind, "definition": str(path), "port": port,
            "url": f"http://127.0.0.1:{port}",
            "note": ("Agent Jo now starts when you log in and is restarted if it "
                     "stops. Closing the browser no longer stops your schedules.")}


def uninstall() -> dict:
    kind = system()
    path = definition_path(kind)
    if kind == "linux":
        _run(["systemctl", "--user", "disable", "--now", UNIT])
        if path and path.exists():
            path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
    elif kind == "macos":
        if path and path.exists():
            _run(["launchctl", "unload", "-w", str(path)])
            path.unlink()
    elif kind == "windows":
        _run(["schtasks", "/End", "/TN", TASK])
        _run(["schtasks", "/Delete", "/TN", TASK, "/F"])
        if path and path.exists():
            path.unlink()
    else:
        return {"ok": False, "error": f"No service support for {platform.system()}."}
    _audit("service_uninstall", kind)
    return {"ok": True, "kind": kind,
            "note": "The background service is removed. Schedules run only while a window is open."}


def status() -> dict:
    kind = system()
    path = definition_path(kind)
    installed = bool(path and path.exists())
    running = None
    detail = ""
    if kind == "linux" and shutil.which("systemctl"):
        ok, out = _run(["systemctl", "--user", "is-active", UNIT])
        running, detail = (out.strip() == "active"), out.strip()
    elif kind == "macos" and shutil.which("launchctl"):
        ok, out = _run(["launchctl", "list", LABEL])
        running = ok and '"PID"' in out
        detail = "loaded" if ok else "not loaded"
    elif kind == "windows":
        ok, out = _run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
        installed = installed or ok
        running = ok and "Running" in out
        detail = "registered" if ok else "not registered"
    holder = scheduler_holder()
    return {"kind": kind, "installed": installed, "running": running,
            "detail": detail, "definition": str(path) if path else "",
            "scheduler_pid": holder,
            "this_process_runs_schedules": _LOCK["fh"] is not None}
