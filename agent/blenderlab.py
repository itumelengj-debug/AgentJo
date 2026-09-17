"""Blender lab — the agent drives Blender headless to design and render scenes.

You prompt in chat; the agent (acting as the designer) writes a Blender Python
(bpy) scene script — geometry, PBR materials, studio lighting, camera — and
this module executes it in headless Blender with the Cycles path-tracer,
returning rendered images. Iterate by telling the agent what to change (or
dragging the render back into chat so it can look and self-critique).

Harness responsibilities, deliberately minimal and safe:
  • Find Blender (BLENDER_PATH setting → PATH → common Windows installs).
  • Run each script in its own job folder with --factory-startup (no user
    addons, reproducible), a hard timeout, and OUT_DIR injected so the script
    always knows where to render.
  • Collect the images + a log tail; never raise into the conversation —
    a failed render is feedback for the next iteration, not a crash.

Scripts are arbitrary Python running in Blender — the tool layer gates this
behind Full access, same trust level as run_command.
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

RUN_TIMEOUT = 300
_HEADER = ("import os\n"
           "OUT_DIR = os.environ.get('AGENTJO_BLENDER_OUT', '.')\n")
_WIN_GLOBS = (
    r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
    r"C:\Program Files\Blender*\blender.exe",
)

# macOS buries the binary inside an .app bundle, so `which blender` finds
# nothing even when Blender is sitting in Applications. Linux installs vary
# by package manager. Without these, the 3D features report "not found" on
# a machine that has it.
_MAC_PATHS = (
    "/Applications/Blender.app/Contents/MacOS/Blender",
    os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender"),
    "/opt/homebrew/bin/blender",
    "/usr/local/bin/blender",
)
_LINUX_PATHS = (
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/snap/bin/blender",
    os.path.expanduser("~/.local/bin/blender"),
)


def _root() -> Path:
    d = config.AGENT_HOME / "blender"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_blender() -> str:
    """Configured path, then PATH, then common Windows installs. '' if none."""
    p = (getattr(config, "BLENDER_PATH", "") or "").strip()
    if p and Path(p).expanduser().exists():
        return str(Path(p).expanduser())
    w = shutil.which("blender")
    if w:
        return w
    import glob
    # the usual places on this platform, before any Windows-specific search
    for p in (_MAC_PATHS if sys.platform == "darwin"
              else () if os.name == "nt" else _LINUX_PATHS):
        if Path(p).exists():
            return p
    for pattern in _WIN_GLOBS:
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits:
            return hits[0]
    # A Microsoft Store install lives under WindowsApps, which is
    # permission-locked and so invisible to a plain glob — but Windows puts
    # an execution alias in the user's own WindowsApps folder, and that IS
    # readable. Health reported "Blender: not found" on a machine that had
    # it installed the whole time.
    for extra in (
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"
                               r"\blender.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\WindowsApps"),
    ):
        p2 = Path(extra)
        if p2.is_file():
            return str(p2)
        if p2.is_dir():
            try:
                hits = sorted(p2.glob("BlenderFoundation.Blender*/Blender/"
                                      "blender.exe"), reverse=True)
                if hits:
                    return str(hits[0])
            except (PermissionError, OSError):
                pass          # WindowsApps refuses listing; the alias above
                              # is the supported way in
    return ""


def run_script(script: str, note: str = "", timeout: int = RUN_TIMEOUT) -> dict:
    """Execute a bpy script headless. Returns images + log tail; never raises."""
    exe = find_blender()
    if not exe:
        return {"ok": False, "images": [],
                "error": ("Blender not found. Install it (free, blender.org) "
                          "and either add it to PATH or set its full path in "
                          "Settings → Blender path.")}
    jid = uuid.uuid4().hex[:10]
    jdir = _root() / jid
    jdir.mkdir(parents=True)
    sp = jdir / "script.py"
    sp.write_text(_HEADER + script, "utf-8")
    env = dict(os.environ)
    env["AGENTJO_BLENDER_OUT"] = str(jdir)
    t0 = time.time()

    def _exec():
        proc = subprocess.run(
            [exe, "--background", "--factory-startup", "--python", str(sp)],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(jdir))
        return ((proc.stdout or "") + "\n" + (proc.stderr or ""),
                proc.returncode == 0)
    try:
        out, ok = _exec()
        if not ok and "OpenImageDenoiser" in out:
            # this Blender build ships without the denoiser (common on
            # distro packages); 4.x defaults denoising ON, so even scripts
            # that never mention it crash — inject a disable before every
            # render call and retry once
            import re as _re
            body = sp.read_text("utf-8")
            body = _re.sub(r"use_denoising\s*=\s*True",
                           "use_denoising = False", body)
            body = _re.sub(
                r"(^[ \t]*)(bpy\.ops\.render\.render\()",
                r"\1bpy.context.scene.cycles.use_denoising = False\n\1\2",
                body, flags=_re.MULTILINE)
            sp.write_text(body, "utf-8")
            out2, ok = _exec()
            out = ("(denoiser unavailable in this Blender build — "
                   "auto-retried without it)\n") + out2
    except subprocess.TimeoutExpired:
        out, ok = f"(timed out after {timeout}s)", False
    except Exception as exc:
        out, ok = f"{type(exc).__name__}: {exc}", False
    images = sorted(p.name for p in jdir.iterdir()
                    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".exr",
                                            ".webp"))
    models = sorted(p.name for p in jdir.iterdir()
                    if p.suffix.lower() in (".glb", ".gltf", ".obj", ".fbx",
                                            ".stl", ".blend"))
    if ok and not images:
        ok = False
        out += ("\n(no image produced — the script must render to OUT_DIR, "
                "e.g. scene.render.filepath = os.path.join(OUT_DIR, "
                "'render.png'); bpy.ops.render.render(write_still=True))")
    tail = "\n".join(ln for ln in out.strip().splitlines()
                     if ln.strip())[-3000:]
    meta = {"id": jid, "ts": round(time.time(), 3),
            "iso": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"),
            "note": note[:200], "ok": ok, "images": images,
            "models": models,
            "seconds": round(time.time() - t0, 1)}
    (jdir / "meta.json").write_text(json.dumps(meta), "utf-8")
    try:
        from . import audit
        audit.record("blender", name=jid, ok=ok, detail=note[:160],
                     summary=(", ".join(images) or tail.splitlines()[-1]
                              if tail else "")[:200])
    except Exception:
        pass
    return {"ok": ok, "job": jid, "images": images, "models": models,
            "paths": [str(jdir / i) for i in images + models],
            "seconds": meta["seconds"], "log_tail": tail}


def jobs(n: int = 20) -> list:
    out = []
    for d in sorted(_root().iterdir(), reverse=True):
        m = d / "meta.json"
        if m.exists():
            try:
                out.append(json.loads(m.read_text("utf-8")))
            except Exception:
                pass
        if len(out) >= n:
            break
    return out


def image_path(job: str, name: str) -> Path | None:
    """Path-traversal-safe lookup of a rendered image."""
    return _safe_lookup(job, name, (".png", ".jpg", ".jpeg", ".webp"))


def model_path(job: str, name: str) -> Path | None:
    """Path-traversal-safe lookup of an exported 3D model file."""
    return _safe_lookup(job, name, (".glb", ".gltf", ".obj", ".fbx", ".stl",
                                    ".blend"))


def _safe_lookup(job: str, name: str, exts) -> Path | None:
    if not (job.isalnum() and name == Path(name).name):
        return None
    p = _root() / job / name
    return p if p.exists() and p.suffix.lower() in exts else None
