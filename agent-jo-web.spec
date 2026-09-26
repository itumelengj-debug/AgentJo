# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - builds the Agent Jo web app into one Windows .exe.

Build:  .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean agent-jo-web.spec
Output: dist\AgentJo.exe

Notes:
  * One-file build (a single double-clickable .exe). For faster startup or if an
    antivirus flags the one-file stub, set ONEFILE = False below for a one-folder
    build (dist\AgentJo\AgentJo.exe + an _internal folder).
  * anthropic and openai are imported lazily in the engine, so they are declared
    as hidden imports here; PyInstaller's static graph would otherwise miss them.
  * Heavy, unused stacks (torch, gradio, voice/training deps) are excluded to
    keep the binary small - the web server never imports them.
"""
import os
from PyInstaller.utils.hooks import collect_submodules

ONEFILE = True

hiddenimports = []
for _pkg in ("uvicorn", "anthropic", "openai", "fastapi", "starlette",
             "pydantic", "anyio", "sniffio", "httpx", "httpcore", "h11",
             "websockets", "watchfiles", "multipart", "pypdf", "docx",
             "openpyxl", "PIL"):
    hiddenimports += collect_submodules(_pkg)   # empty list if a pkg is absent

# our own packages (some imports are lazy/inside functions, e.g. agent.windpapi;
# collect_submodules makes sure onefile never misses one)
hiddenimports += collect_submodules("agent")
hiddenimports += collect_submodules("web")
hiddenimports += ["web", "web.server", "web.auth", "web.ratelimit", "agent.windpapi"]

# --- optional voice (speech-to-text) --------------------------------------- #
# faster-whisper loads a native CTranslate2 backend that PyInstaller's static
# graph can't see, so collect its submodules + binaries + data explicitly.
# All guarded: if faster-whisper isn't installed in the build venv, the .exe
# simply builds without voice (and says so at runtime) instead of failing.
voice_binaries, voice_datas = [], []
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files
    for _vpkg in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime",
                  "av"):
        hiddenimports += collect_submodules(_vpkg)
        voice_binaries += collect_dynamic_libs(_vpkg)
        voice_datas += collect_data_files(_vpkg)
    print(f"[spec] voice bundled: {len(voice_binaries)} libs, "
          f"{len(voice_datas)} data files")
except Exception as _exc:
    print(f"[spec] voice NOT bundled ({_exc}); build proceeds without it")

datas = [
    ("web/static", "web/static"),     # index.html, styles.css, app.js
    ("agent_avatar.png", "."),        # served at /avatar and used as brand mark
]

excludes = [
    "torch", "transformers", "datasets", "peft", "trl", "accelerate",
    "bitsandbytes", "gradio", "matplotlib", "tkinter",
    "scipy", "IPython", "notebook",
]

icon = "atlas.ico" if os.path.exists("atlas.ico") else None

a = Analysis(
    ["web_exe.py"],
    pathex=["."],
    binaries=voice_binaries,
    datas=datas + voice_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="AgentJo",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
        runtime_tmpdir=None, console=True, icon=icon,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="AgentJo",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
        console=True, icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="AgentJo")
