"""Neural photo→3D — run a locally-installed image-to-3D model (TripoSR-class)
and post-process the mesh in Blender.

This is the neural sibling of the vision-grounded reconstruction (feature 19):
instead of the agent modelling shapes it sees, a neural network LIFTS the
photo into a mesh — which is what handles organic subjects. The trade, stated
plainly: it requires an ML tool the user installs themselves (TripoSR,
InstantMesh, Hunyuan3D…), typically wanting a CUDA GPU (CPU works but is
slow), plus multi-GB model weights. This module is the HARNESS around that
tool — configured via a command template so any of them plugs in:

    Settings → "Neural 3D command", e.g.
    C:\\tools\\trellis-env\\python.exe C:\\tools\\TripoSR\\run.py {image} --output-dir {out}

{image} and {out} are substituted per job. After the tool produces a mesh,
if Blender is available the harness runs a FIXED, module-owned post-process:
import the mesh, normalise its scale, studio-light it, render turntable
angles, and re-export a clean model.glb — so a raw neural mesh comes back as
something you can immediately see and use.

Harness behaviour mirrors the Blender lab: per-job folders, hard timeouts,
log tails as feedback (never exceptions into the chat), audit entries, and
path-traversal-safe file lookups.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

RUN_TIMEOUT = 1200            # neural inference on CPU can be many minutes
MESH_EXTS = (".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

_POSTPROCESS = """
import bpy, os, math
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
mesh_path = os.environ["AGENTJO_N3D_MESH"]
ext = os.path.splitext(mesh_path)[1].lower()
if ext in (".glb", ".gltf"):
    bpy.ops.import_scene.gltf(filepath=mesh_path)
elif ext == ".obj":
    bpy.ops.wm.obj_import(filepath=mesh_path)
elif ext == ".ply":
    bpy.ops.wm.ply_import(filepath=mesh_path)
elif ext == ".stl":
    bpy.ops.wm.stl_import(filepath=mesh_path)
objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not objs:
    raise RuntimeError("no mesh found after import")
# normalise: join, centre, scale to ~2 units
bpy.ops.object.select_all(action="DESELECT")
for o in objs:
    o.select_set(True)
bpy.context.view_layer.objects.active = objs[0]
if len(objs) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
dims = max(obj.dimensions) or 1.0
obj.scale = (2.0 / dims,) * 3
bpy.ops.object.transform_apply(scale=True)
obj.location = (0, 0, obj.dimensions.z / 2)
# studio: soft key + fill, camera on a slight orbit
bpy.ops.object.light_add(type="AREA", location=(4, -4, 5))
k = bpy.context.object; k.data.energy = 700; k.data.size = 6
k.rotation_euler = (0.9, 0.2, 0.8)
bpy.ops.object.light_add(type="AREA", location=(-5, -2, 3))
f = bpy.context.object; f.data.energy = 200; f.data.size = 5
scene.render.engine = "CYCLES"
scene.cycles.samples = 32
scene.render.resolution_x, scene.render.resolution_y = 640, 480
for i, ang in enumerate((0.6, 2.4)):
    bpy.ops.object.camera_add(
        location=(5 * math.cos(ang), 5 * math.sin(ang), 2.6),
        rotation=(1.18, 0, ang + math.pi / 2))
    scene.camera = bpy.context.object
    scene.render.filepath = os.path.join(OUT_DIR, f"turntable_{i}.png")
    bpy.ops.render.render(write_still=True)
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath=os.path.join(OUT_DIR, "model.glb"),
                          use_selection=True)
"""



# --------------------------------------------------------------------------- #
#  setup help — find an installed tool and check a command before you rely on it
# --------------------------------------------------------------------------- #
#  Each entry: folder name, the script to run, and the extra flags that tool
#  needs. Ordered by how well the harness supports them.
_KNOWN_TOOLS = [
    ("TripoSR", "run.py", "--output-dir {out} --bake-texture"),
    ("InstantMesh", "run.py", "--output_path {out}"),
    ("stable-fast-3d", "run.py", "--output-dir {out}"),
    ("Hunyuan3D", "main.py", "--output {out}"),
]


def _python_for(root: Path) -> str:
    """Prefer the tool's OWN virtualenv — these tools pin torch versions that
    would fight whatever else is on the system python."""
    for rel in ("venv/Scripts/python.exe", ".venv/Scripts/python.exe",
                "venv/bin/python", ".venv/bin/python"):
        p = root / rel
        if p.exists():
            return str(p)
    return "python"


def detect() -> list:
    """Look for an installed image-to-3D tool and build a ready command."""
    home = Path.home()
    roots = [home, home / "Documents", home / "source", home / "repos",
             Path("C:/"), Path("D:/"), Path("/opt")]
    found, seen = [], set()
    for base in roots:
        try:
            if not base.exists():
                continue
        except Exception:
            continue
        for name, script, flags in _KNOWN_TOOLS:
            root = base / name
            try:
                if not (root / script).exists() or str(root) in seen:
                    continue
            except Exception:
                continue
            seen.add(str(root))
            py = _python_for(root)
            found.append({
                "tool": name,
                "path": str(root),
                "venv": py != "python",
                "command": f'"{py}" "{root / script}" {{image}} {flags}',
            })
    return found


def validate(cmd_tpl: str) -> dict:
    """Check a command template without running an inference — placeholders,
    interpreter, script. Cheap, and it turns a mystery failure into a
    sentence you can act on."""
    cmd_tpl = (cmd_tpl or "").strip()
    if not cmd_tpl:
        return {"ok": False,
                "detail": "No command set. Click Detect, or paste the command "
                          "that runs your image-to-3D tool."}
    missing = [p for p in ("{image}", "{out}") if p not in cmd_tpl]
    if missing:
        return {"ok": False,
                "detail": f"Missing placeholder(s): {', '.join(missing)}. "
                          f"{{image}} is where the photo path goes, {{out}} is "
                          f"the folder the tool should write the mesh into."}
    try:
        parts = shlex.split(cmd_tpl, posix=(os.name != "nt"))
    except ValueError as exc:
        return {"ok": False, "detail": f"Command can't be parsed ({exc}). "
                                       f"Quote any path containing spaces."}
    if not parts:
        return {"ok": False, "detail": "Command is empty."}
    exe = Path(parts[0].strip('"'))
    if not exe.exists() and not shutil.which(parts[0].strip('"')):
        return {"ok": False,
                "detail": f"Interpreter not found: {parts[0]}. Point this at "
                          f"the python.exe inside the tool's own venv."}
    script = next((p.strip('"') for p in parts[1:]
                   if p.strip('"').lower().endswith(".py")), None)
    if script and not Path(script).exists():
        return {"ok": False, "detail": f"Script not found: {script}"}
    return {"ok": True,
            "detail": f"Looks good — {exe.name}"
                      + (f" running {Path(script).name}" if script else "")
                      + ". Attach a photo path and run photo_to_3d_neural to "
                        "confirm end to end."}


def _root() -> Path:
    d = config.AGENT_HOME / "neural3d"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run(image_path: str, note: str = "") -> dict:
    """Run the configured neural tool on one image, then post-process in
    Blender when available. Never raises; failures return log tails."""
    cmd_tpl = (getattr(config, "NEURAL3D_CMD", "") or "").strip()
    if not cmd_tpl or "{image}" not in cmd_tpl or "{out}" not in cmd_tpl:
        return {"ok": False, "error": (
            "No neural image-to-3D tool is configured. Install one "
            "(TripoSR-class — see the README recipe), then set Settings → "
            "'Neural 3D command' to its command line with {image} and {out} "
            "placeholders.")}
    img = Path(image_path).expanduser()
    if not img.exists() or img.suffix.lower() not in IMG_EXTS:
        return {"ok": False,
                "error": f"Image not found (or not an image): {image_path}"}
    jid = uuid.uuid4().hex[:10]
    jdir = _root() / jid
    jdir.mkdir(parents=True)
    cmd = [a.replace("{image}", str(img)).replace("{out}", str(jdir))
           for a in shlex.split(cmd_tpl, posix=(os.name != "nt"))]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT, cwd=str(jdir))
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = f"(neural tool timed out after {RUN_TIMEOUT}s)", False
    except FileNotFoundError:
        out, ok = (f"(command not found: {cmd[0]} — check the Neural 3D "
                   f"command in Settings)", False)
    except Exception as exc:
        out, ok = f"{type(exc).__name__}: {exc}", False
    meshes = sorted(p for p in jdir.rglob("*")
                    if p.suffix.lower() in MESH_EXTS)
    if ok and not meshes:
        ok = False
        out += "\n(the tool exited cleanly but produced no mesh in {out})"
    post = {"ran": False}
    if ok and meshes:
        post = _postprocess(jdir, meshes[0])
    images = sorted(p.name for p in jdir.iterdir()
                    if p.suffix.lower() in IMG_EXTS)
    models = sorted(p.relative_to(jdir).as_posix() for p in meshes
                    if p.parent == jdir) or [m.relative_to(jdir).as_posix()
                                             for m in meshes[:3]]
    if (jdir / "model.glb").exists() and "model.glb" not in models:
        models.insert(0, "model.glb")
    meta = {"id": jid, "ts": round(time.time(), 3), "iso": _iso(),
            "note": note[:200], "ok": ok, "images": images, "models": models,
            "seconds": round(time.time() - t0, 1),
            "postprocess": post.get("ran", False)}
    (jdir / "meta.json").write_text(json.dumps(meta), "utf-8")
    try:
        from . import audit
        audit.record("neural3d", name=jid, ok=ok, detail=note[:160],
                     summary=(", ".join(models) or "no mesh")[:200])
    except Exception:
        pass
    tail = "\n".join(ln for ln in out.strip().splitlines()
                     if ln.strip())[-2500:]
    return {"ok": ok, "job": jid, "images": images, "models": models,
            "seconds": meta["seconds"], "log_tail": tail,
            "post_log": post.get("log", "")[-1200:]}


def _postprocess(jdir: Path, mesh: Path) -> dict:
    """Fixed Blender pass: normalise, light, turntable renders, clean glb."""
    try:
        from . import blenderlab
        if not blenderlab.find_blender():
            return {"ran": False, "log": "blender not installed — raw mesh "
                                         "only"}
        os.environ["AGENTJO_N3D_MESH"] = str(mesh)
        r = blenderlab.run_script(_POSTPROCESS,
                                  note=f"neural3d post {jdir.name}")
        # pull the post-process outputs into the neural job folder
        for name in r.get("images", []) + r.get("models", []):
            src = Path(config.AGENT_HOME) / "blender" / r["job"] / name
            if src.exists():
                (jdir / name).write_bytes(src.read_bytes())
        return {"ran": r.get("ok", False), "log": r.get("log_tail", "")}
    except Exception as exc:
        return {"ran": False, "log": f"{type(exc).__name__}: {exc}"}
    finally:
        os.environ.pop("AGENTJO_N3D_MESH", None)


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


def file_path(job: str, name: str) -> Path | None:
    if not (job.isalnum() and name == Path(name).name):
        return None
    p = _root() / job / name
    return p if p.exists() and p.suffix.lower() in (MESH_EXTS + IMG_EXTS) \
        else None
