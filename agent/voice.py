"""Local voice for Agent Jo — private by design.

Text-to-speech is server-side and uses the operating system's own voice, so it
needs no Python package on Windows or macOS and nothing ever leaves the
machine. Because a local install runs on your own computer, "server-side"
playback means your own speakers.

  Windows -> PowerShell System.Speech (built in)
  macOS   -> `say`
  Linux   -> `espeak-ng`/`espeak` or `spd-say` if present
  any OS  -> pyttsx3, only if it happens to be installed

Speech-to-text uses faster-whisper if installed (`pip install faster-whisper`)
and runs entirely locally; the model downloads once on first use. If it isn't
installed, the mic UI still appears and simply asks you to install it.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading

_proc_lock = threading.Lock()
_current = None                 # current TTS subprocess (for stop())
_whisper_model = None           # lazily-loaded faster-whisper model


# ---------------------------------------------------------------------- #
# Text-to-speech
# ---------------------------------------------------------------------- #
def tts_engine() -> str:
    """Which TTS engine is usable here, or 'none'."""
    sysname = platform.system()
    if sysname == "Windows":
        return "windows-sapi"
    if sysname == "Darwin" and shutil.which("say"):
        return "macos-say"
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    if shutil.which("spd-say"):
        return "spd-say"
    try:
        import pyttsx3  # noqa: F401
        return "pyttsx3"
    except Exception:
        return "none"


def tts_available() -> bool:
    return tts_engine() != "none"


def _strip_markdown(text: str) -> str:
    """Make text pleasant to hear: drop code, markup, and raw URLs."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)   # code fences
    text = re.sub(r"`[^`]*`", " ", text)                        # inline code
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)             # [label](url) -> label
    text = re.sub(r"https?://\S+", "link", text)                # bare URLs
    text = re.sub(r"[*_#>`~|\[\]]", "", text)                   # markup chars
    return " ".join(text.split())


def _speak_windows(text: str) -> None:
    """Speak via Windows System.Speech. Text goes through a UTF-8 temp file to
    avoid any command-line quoting/encoding issues."""
    global _current
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="atlas_tts_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$sp = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$t = [System.IO.File]::ReadAllText('{path}', "
            "[System.Text.Encoding]::UTF8); $sp.Speak($t)"
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        with _proc_lock:
            _current = proc
        proc.wait()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        with _proc_lock:
            _current = None


def _speak_cmd(cmd: list) -> None:
    global _current
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    with _proc_lock:
        _current = proc
    proc.wait()
    with _proc_lock:
        _current = None


def _speak_pyttsx3(text: str) -> None:
    try:
        import pyttsx3
        eng = pyttsx3.init()
        eng.say(text)
        eng.runAndWait()
    except Exception:
        pass


def _run_engine(engine: str, text: str) -> None:
    try:
        if engine == "windows-sapi":
            _speak_windows(text)
        elif engine == "macos-say":
            _speak_cmd(["say", text])
        elif engine == "espeak":
            _speak_cmd([shutil.which("espeak-ng") or "espeak", text])
        elif engine == "spd-say":
            _speak_cmd(["spd-say", "-w", text])
        elif engine == "pyttsx3":
            _speak_pyttsx3(text)
    except Exception:
        pass


def speak(text: str, max_chars: int = 4000) -> bool:
    """Speak text aloud on the local machine, without blocking. Any currently
    playing speech is stopped first. Returns False if no TTS engine is usable
    or there's nothing to say."""
    engine = tts_engine()
    if engine == "none":
        return False
    clean = _strip_markdown(text or "")
    if not clean:
        return False
    stop()
    threading.Thread(target=_run_engine, args=(engine, clean[:max_chars]),
                     daemon=True).start()
    return True


def stop() -> None:
    """Stop any in-progress speech."""
    with _proc_lock:
        proc = _current
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# Speech-to-text (optional, local)
# ---------------------------------------------------------------------- #
_STT_REASON = ""


def stt_available() -> bool:
    global _STT_REASON
    try:
        import faster_whisper  # noqa: F401
        _STT_REASON = ""
        return True
    except Exception as exc:
        # keep the real reason: "not installed" and "installed but broken"
        # need different fixes, and this tells them apart
        _STT_REASON = f"{type(exc).__name__}: {exc}"
        return True if False else False


def stt_reason() -> str:
    return _STT_REASON


def transcribe(audio_path: str, model_size: str = "base") -> str:
    """Transcribe an audio file locally with faster-whisper. The model loads
    once and is cached. Raises if faster-whisper isn't installed."""
    global _whisper_model
    from faster_whisper import WhisperModel
    if _whisper_model is None or getattr(_whisper_model, "_atlas_size", None) != model_size:
        device = os.environ.get("AGENT_STT_DEVICE", "cpu")
        compute = os.environ.get("AGENT_STT_COMPUTE", "int8")
        _whisper_model = WhisperModel(model_size, device=device, compute_type=compute)
        _whisper_model._atlas_size = model_size       # type: ignore[attr-defined]
    segments, _info = _whisper_model.transcribe(audio_path)
    return " ".join(seg.text.strip() for seg in segments).strip()
