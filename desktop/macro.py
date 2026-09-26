#!/usr/bin/env python3
"""Agent Jo — desktop macro recorder & player (Windows / cross-platform).

A *scoped* automation tool: you explicitly start a recording, perform a workflow
once, stop, and replay that exact sequence later — by hand or on a schedule. It
is NOT an always-on logger: it only captures input during a recording session
you start yourself, and it tells you clearly when it is recording.

    python desktop/macro.py record  daily-pnl
    python desktop/macro.py list
    python desktop/macro.py play    daily-pnl
    python desktop/macro.py play    daily-pnl --speed 1.5 --repeat 3
    python desktop/macro.py delete  daily-pnl

============================ READ THIS FIRST ============================
• Recording captures the keys you press, so DO NOT type passwords, PINs, OTPs,
  card numbers, or other secrets while recording. Pause the workflow, enter the
  secret by hand at replay time, or design the macro to stop before the login.
• Recordings are stored ENCRYPTED at rest in your Agent Jo data folder, local
  only — nothing is ever sent anywhere. Still, protect that folder.
• On a work/managed machine, recording and replaying input may be against your
  employer's security policy and may be flagged by endpoint monitoring. Check
  before you use it on a corporate device.
• Replay drives the real mouse and keyboard. It only works on an UNLOCKED,
  logged-in desktop (a locked screen or background session can't be driven).
  FAILSAFE: slam the mouse into the TOP-LEFT corner of the screen, or press
  Esc, to abort a replay immediately.
========================================================================

Requires `pynput`  ->  pip install -r requirements-macro.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# --- storage location + encryption (reuse Agent Jo's, if importable) -------- #
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent import config as _config
    from agent import crypto as _crypto
    _HOME = _config.AGENT_HOME
    _HAVE_CRYPTO = True
except Exception:                       # run standalone without the package
    _HOME = Path.home() / ".local_agent"
    _crypto = None
    _HAVE_CRYPTO = False

_MACRO_DIR = _HOME / "macros"
_MOVE_SAMPLE_S = 0.03                    # downsample mouse-move events (~33/s)
_CORNER_PX = 3                           # failsafe: mouse within this of (0,0)
_STOP_KEY_NAME = "f9"                    # press to end a recording


def _dir() -> Path:
    _MACRO_DIR.mkdir(parents=True, exist_ok=True)
    return _MACRO_DIR


def _path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "macro"
    return _dir() / f"{safe}.macro"


def _save(name: str, data: dict) -> Path:
    blob = json.dumps(data)
    if _HAVE_CRYPTO:
        blob = _crypto.encrypt_str(blob)
    p = _path(name)
    p.write_text(blob, encoding="utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


def _load(name: str) -> dict:
    raw = _path(name).read_text(encoding="utf-8")
    if _HAVE_CRYPTO and _crypto.is_encrypted(raw):
        raw = _crypto.decrypt_str(raw)
    return json.loads(raw)


# --------------------------------------------------------------------------- #
#  key (de)serialisation
# --------------------------------------------------------------------------- #
def _key_to_str(key) -> str:
    from pynput import keyboard
    if isinstance(key, keyboard.KeyCode) and key.char is not None:
        return "c:" + key.char
    if isinstance(key, keyboard.Key):
        return "k:" + key.name
    # KeyCode with a virtual-key but no char (rare)
    return "v:" + str(getattr(key, "vk", ""))


def _str_to_key(s: str):
    from pynput import keyboard
    kind, _, val = s.partition(":")
    if kind == "c":
        return keyboard.KeyCode.from_char(val)
    if kind == "k":
        return getattr(keyboard.Key, val, None)
    if kind == "v" and val:
        try:
            return keyboard.KeyCode.from_vk(int(val))
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
#  RECORD
# --------------------------------------------------------------------------- #
def cmd_record(name: str) -> int:
    try:
        from pynput import mouse, keyboard
    except Exception:
        print("pynput is not installed.  pip install -r requirements-macro.txt")
        return 2

    print("=" * 64)
    print(f"  RECORDING macro '{name}'")
    print("  • Do NOT type any passwords, PINs, OTPs or secrets.")
    print(f"  • Press {_STOP_KEY_NAME.upper()} to stop and save.")
    print(f"  • Recording saved {'ENCRYPTED ' if _HAVE_CRYPTO else ''}to {_path(name)}")
    print("=" * 64)
    for n in (3, 2, 1):
        print(f"  starting in {n}…", end="\r", flush=True)
        time.sleep(1)
    print("  ● recording — go.            ")

    events: list = []
    t0 = time.monotonic()
    last_move = [0.0]
    stop_key = getattr(keyboard.Key, _STOP_KEY_NAME, None)
    stopped = {"v": False}

    def now() -> float:
        return round(time.monotonic() - t0, 4)

    def on_move(x, y):
        t = time.monotonic()
        if t - last_move[0] >= _MOVE_SAMPLE_S:
            last_move[0] = t
            events.append({"t": now(), "e": "move", "x": x, "y": y})

    def on_click(x, y, button, pressed):
        events.append({"t": now(), "e": "click", "x": x, "y": y,
                       "b": button.name, "p": pressed})

    def on_scroll(x, y, dx, dy):
        events.append({"t": now(), "e": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

    def on_press(key):
        if stop_key is not None and key == stop_key:
            stopped["v"] = True
            return False                 # stop the keyboard listener
        events.append({"t": now(), "e": "kd", "k": _key_to_str(key)})

    def on_release(key):
        if stop_key is not None and key == stop_key:
            return
        events.append({"t": now(), "e": "ku", "k": _key_to_str(key)})

    m_listener = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
    k_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    m_listener.start()
    k_listener.start()
    try:
        k_listener.join()                # blocks until F9 (on_press returns False)
    except KeyboardInterrupt:
        stopped["v"] = True
    finally:
        m_listener.stop()

    # drop trailing mouse-moves so replay ends on the last meaningful action
    while events and events[-1]["e"] == "move":
        events.pop()

    _save(name, {"version": 1, "created": time.time(), "events": events})
    keys = sum(1 for e in events if e["e"] == "kd")
    clicks = sum(1 for e in events if e["e"] == "click" and e["p"])
    dur = events[-1]["t"] if events else 0
    print(f"\n  ✓ saved '{name}': {len(events)} events "
          f"({clicks} clicks, {keys} keypresses, {dur:.1f}s).")
    if keys:
        print("  ! This macro contains keystrokes — make sure none were secrets.")
    return 0


# --------------------------------------------------------------------------- #
#  PLAY
# --------------------------------------------------------------------------- #
def cmd_play(name: str, speed: float, repeat: int, yes: bool) -> int:
    try:
        from pynput import mouse, keyboard
    except Exception:
        print("pynput is not installed.  pip install -r requirements-macro.txt")
        return 2
    try:
        data = _load(name)
    except FileNotFoundError:
        print(f"No macro named '{name}'. Try: python desktop/macro.py list")
        return 1
    except Exception as exc:
        print(f"Could not read '{name}': {type(exc).__name__}: {exc}")
        return 1

    events = data.get("events", [])
    if not events:
        print("That macro is empty.")
        return 1
    speed = max(0.1, min(10.0, float(speed)))
    repeat = max(1, int(repeat))

    print("=" * 64)
    print(f"  REPLAY '{name}'  —  {len(events)} events, "
          f"{repeat}x, speed {speed}x")
    print("  This will move your mouse and type on your behalf.")
    print("  ABORT any time: mouse to the TOP-LEFT corner, or press Esc.")
    print("=" * 64)
    if not yes:
        try:
            if input("  Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  cancelled.")
                return 0
        except EOFError:
            print("  no TTY to confirm; pass --yes to run unattended.")
            return 1
    for n in (3, 2, 1):
        print(f"  starting in {n}…  (move to top-left to abort)", end="\r", flush=True)
        time.sleep(1)
    print("  ▶ replaying…                                   ")

    abort = {"v": False}

    def watch_move(x, y):
        if x <= _CORNER_PX and y <= _CORNER_PX:
            abort["v"] = True
            return False

    def watch_key(key):
        if key == keyboard.Key.esc:
            abort["v"] = True
            return False

    watcher_m = mouse.Listener(on_move=watch_move)
    watcher_k = keyboard.Listener(on_press=watch_key)
    watcher_m.start()
    watcher_k.start()

    m_ctl = mouse.Controller()
    k_ctl = keyboard.Controller()
    btn = {"left": mouse.Button.left, "right": mouse.Button.right,
           "middle": mouse.Button.middle}

    def run_once() -> bool:
        clock = time.monotonic()
        base = events[0]["t"]
        for ev in events:
            if abort["v"]:
                return False
            target = clock + (ev["t"] - base) / speed
            while True:
                if abort["v"]:
                    return False
                dt = target - time.monotonic()
                if dt <= 0:
                    break
                time.sleep(min(dt, 0.02))
            try:
                k = ev["e"]
                if k == "move":
                    m_ctl.position = (ev["x"], ev["y"])
                elif k == "click":
                    m_ctl.position = (ev["x"], ev["y"])
                    b = btn.get(ev["b"], mouse.Button.left)
                    m_ctl.press(b) if ev["p"] else m_ctl.release(b)
                elif k == "scroll":
                    m_ctl.scroll(ev.get("dx", 0), ev.get("dy", 0))
                elif k == "kd":
                    key = _str_to_key(ev["k"])
                    if key is not None:
                        k_ctl.press(key)
                elif k == "ku":
                    key = _str_to_key(ev["k"])
                    if key is not None:
                        k_ctl.release(key)
            except Exception:
                pass                      # one bad event shouldn't kill the run
        return True

    completed = 0
    try:
        for i in range(repeat):
            if abort["v"]:
                break
            if repeat > 1:
                print(f"  pass {i + 1}/{repeat}…")
            if not run_once():
                break
            completed += 1
    finally:
        watcher_m.stop()
        watcher_k.stop()

    if abort["v"]:
        print("  ■ aborted by failsafe.")
        return 1
    print(f"  ✓ done ({completed}/{repeat} passes).")
    return 0


# --------------------------------------------------------------------------- #
#  LIST / DELETE
# --------------------------------------------------------------------------- #
def cmd_list() -> int:
    items = sorted(_dir().glob("*.macro"))
    if not items:
        print("No macros yet. Record one:  python desktop/macro.py record <name>")
        return 0
    print(f"Saved macros ({len(items)}) in {_dir()}:")
    for p in items:
        info = ""
        try:
            d = _load(p.stem)
            evs = d.get("events", [])
            dur = evs[-1]["t"] if evs else 0
            keys = sum(1 for e in evs if e["e"] == "kd")
            info = f"  — {len(evs)} events, {dur:.1f}s" + (", has keystrokes" if keys else "")
        except Exception:
            info = "  — (unreadable / wrong key)"
        print(f"  • {p.stem}{info}")
    return 0


def cmd_delete(name: str) -> int:
    p = _path(name)
    if not p.exists():
        print(f"No macro named '{name}'.")
        return 1
    p.unlink()
    print(f"Deleted '{name}'.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="macro", description="Agent Jo desktop macro recorder & player.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a new macro")
    r.add_argument("name")
    p = sub.add_parser("play", help="replay a macro")
    p.add_argument("name")
    p.add_argument("--speed", type=float, default=1.0, help="0.1–10x (default 1)")
    p.add_argument("--repeat", type=int, default=1, help="number of passes")
    p.add_argument("--yes", action="store_true", help="skip the confirm prompt (for schedules)")
    sub.add_parser("list", help="list saved macros")
    d = sub.add_parser("delete", help="delete a macro")
    d.add_argument("name")

    args = ap.parse_args(argv)
    if args.cmd == "record":
        return cmd_record(args.name)
    if args.cmd == "play":
        return cmd_play(args.name, args.speed, args.repeat, args.yes)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "delete":
        return cmd_delete(args.name)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
