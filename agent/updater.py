"""Updates in one click — and only ones you signed.

An updater is the most dangerous thing an agent that runs commands can have:
whoever can push it an "update" can run code on your machine. So this installs
nothing it can't prove came from you:

  1. Check. Reads latest.json from the update address you set (a GitHub
     release, any web folder): {build, url, sha256, notes}. A newer build is
     offered; nothing is downloaded until you say so.
  2. Verify. The zip must match latest.json's SHA-256, and its MANIFEST.sig
     must verify against the public key ALREADY IN THIS COPY — not the one in
     the download, which an attacker would simply replace. Every file must
     match the signed manifest. The release may not change the key, and may
     not be an older build. Anything that fails is refused, with the reason.
  3. Install. Files are copied over the app folder; every file replaced is
     kept first, so Roll back puts it exactly as it was. Your data lives in
     your home folder and is never touched. If requirements.txt changed, the
     new packages are installed. Then restart the app.

Releases are signed by tools/make_release.py when signing-key.private exists
(python tools/sign.py --make-key, once). A copy with no signing-key.pub can't
verify anything, so it refuses every update and says how to fix that.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

from . import config

ROOT = Path(__file__).resolve().parents[1]
MANIFEST, SIGNATURE, PUBKEY = "MANIFEST.sha256", "MANIFEST.sig", "signing-key.pub"
MAX_BYTES = 300 * 1024 * 1024
# never written by an update, whatever a manifest says
PROTECTED = (".venv", "venv", ".git", ".update", "signing-key.private")


def _work() -> Path:
    d = ROOT / ".update"
    d.mkdir(exist_ok=True)
    return d


def _settings_path() -> Path:
    return config.AGENT_HOME / "updates.json"


def settings() -> dict:
    try:
        s = json.loads(_settings_path().read_text("utf-8"))
    except Exception:
        s = {}
    return {"source": "", "last_check": "", **s}


def save_settings(s: dict) -> dict:
    cur = {**settings(), **{k: v for k, v in (s or {}).items() if k in ("source",)}}
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(cur, indent=2), "utf-8")
    return cur


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def _get(url: str, *, limit: int = MAX_BYTES) -> bytes:
    """Fetch a URL (https, or file:// for a folder on your network)."""
    if url.startswith("file://") or (len(url) > 2 and url[1] == ":") or url.startswith("/"):
        p = Path(url[7:] if url.startswith("file://") else url)
        data = p.read_bytes()
    else:
        if not url.startswith("https://"):
            raise ValueError("updates are only fetched over https")
        import httpx
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > limit:
                    raise ValueError("the download is larger than any release should be")
            data = bytes(buf)
    if len(data) > limit:
        raise ValueError("the download is larger than any release should be")
    return data


def status() -> dict:
    last = {}
    try:
        last = json.loads((_work() / "last.json").read_text("utf-8"))
    except Exception:
        pass
    return {"build": config.BUILD_ID, "source": settings()["source"],
            "can_verify": (ROOT / PUBKEY).exists(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "last_install": last,
            "can_roll_back": bool(last) and (_work() / "previous").exists()}


def check() -> dict:
    """Is there a newer build? Downloads latest.json only."""
    src = settings()["source"].strip()
    if not src:
        return {"ok": False, "error": "No update address set. Paste the address of "
                                      "your release's latest.json."}
    try:
        info = json.loads(_get(src, limit=1_000_000).decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't read {src}: {type(exc).__name__}: {exc}"}
    if not all(info.get(k) for k in ("build", "url", "sha256")):
        return {"ok": False, "error": "latest.json is missing build, url or sha256."}
    s = settings(); s["last_check"] = _now()
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(s, indent=2), "utf-8")
    newer = str(info["build"]) > str(config.BUILD_ID)
    return {"ok": True, "current": config.BUILD_ID, "latest": info["build"],
            "newer": newer, "notes": str(info.get("notes", ""))[:2000],
            "size": info.get("size"), "url": urljoin(src, info["url"]),
            "sha256": info["sha256"]}


def verify_release(folder: Path, pubkey_pem: bytes) -> dict:
    """Is this unpacked release signed by the key we trust, and unaltered?"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature
    man, sig = folder / MANIFEST, folder / SIGNATURE
    if not man.exists() or not sig.exists():
        return {"ok": False, "error": "This release isn't signed, so it can't be "
                                      "shown to come from you. Refused."}
    key = serialization.load_pem_public_key(pubkey_pem)
    try:
        key.verify(sig.read_bytes(), man.read_bytes())
    except InvalidSignature:
        return {"ok": False, "error": "The signature doesn't match the key in this "
                                      "copy: this release wasn't signed by you, or "
                                      "was changed after signing. Refused."}
    body = json.loads(man.read_text("utf-8"))
    entries = body.get("entries") or {}
    bad = []
    for rel, want in entries.items():
        p = folder / rel
        if (".." in Path(rel).parts or Path(rel).is_absolute()
                or any(rel == x or rel.startswith(x + "/") for x in PROTECTED)):
            bad.append(f"{rel} (not allowed in a release)")
        elif not p.is_file():
            bad.append(f"{rel} (missing)")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != want:
            bad.append(f"{rel} (changed)")
    if bad:
        return {"ok": False, "error": "Signed, but these files don't match what was "
                                      "signed: " + ", ".join(bad[:6]) + ". Refused."}
    if (folder / PUBKEY).exists() and (folder / PUBKEY).read_bytes().strip() != pubkey_pem.strip():
        return {"ok": False, "error": "This release carries a different signing key. "
                                      "A key change can't arrive by update. Refused."}
    return {"ok": True, "entries": entries, "build": body.get("build", "")}


def _unpack(data: bytes, dest: Path) -> Path:
    import io
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            target = (dest / n).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"unsafe path in the zip: {n}")
        z.extractall(dest)
    # the release may sit in one top folder
    if not (dest / MANIFEST).exists():
        kids = [k for k in dest.iterdir() if k.is_dir()]
        if len(kids) == 1 and (kids[0] / MANIFEST).exists():
            return kids[0]
    return dest


def install(info: dict | None = None) -> dict:
    """Download, verify, back up, and copy in. Restart afterwards."""
    if getattr(sys, "frozen", False):
        return {"ok": False, "error": "This copy runs as a packaged .exe, which can't "
                                      "update its own files. Install the new release."}
    if not (ROOT / PUBKEY).exists():
        return {"ok": False, "error": "This copy has no signing-key.pub, so it can't "
                                      "check an update is yours. Install one signed "
                                      "release by hand (python tools/sign.py --make-key, "
                                      "then tools/make_release.py); updates after that "
                                      "are one click."}
    info = info or check()
    if not info.get("ok"):
        return info
    if not info.get("newer"):
        return {"ok": False, "error": f"Already up to date ({config.BUILD_ID})."}
    try:
        data = _get(info["url"])
    except Exception as exc:
        return {"ok": False, "error": f"The download failed: {type(exc).__name__}: {exc}"}
    if hashlib.sha256(data).hexdigest() != str(info["sha256"]).lower():
        return {"ok": False, "error": "The download doesn't match latest.json's "
                                      "checksum. Refused."}
    try:
        folder = _unpack(data, _work() / "staging")
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't unpack it: {exc}"}
    v = verify_release(folder, (ROOT / PUBKEY).read_bytes())
    if not v.get("ok"):
        return v
    if v.get("build") and str(v["build"]) <= str(config.BUILD_ID):
        return {"ok": False, "error": "That release is not newer than this one. Refused."}
    files = list(v["entries"]) + [MANIFEST, SIGNATURE]
    prev = _work() / "previous"
    if prev.exists():
        shutil.rmtree(prev)
    replaced, added = [], []
    old_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    for rel in files:
        src, dst = folder / rel, ROOT / rel
        if dst.exists():
            (prev / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, prev / rel)
            replaced.append(rel)
        else:
            added.append(rel)
    try:
        for rel in files:
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / rel, ROOT / rel)
    except Exception as exc:
        _restore(replaced, added)
        return {"ok": False, "error": f"Copying the new files failed ({exc}); "
                                      f"everything was put back as it was."}
    record = {"at": _now(), "from": config.BUILD_ID, "to": v.get("build") or info["latest"],
              "replaced": replaced, "added": added}
    (_work() / "last.json").write_text(json.dumps(record, indent=1), "utf-8")
    shutil.rmtree(_work() / "staging", ignore_errors=True)
    pip = ""
    new_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    if new_req != old_req:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            str(ROOT / "requirements.txt"), "--quiet"],
                           capture_output=True, text=True)
        pip = "new packages installed" if p.returncode == 0 else (
            "new packages could not be installed — run install.bat")
    return {"ok": True, "from": record["from"], "to": record["to"],
            "files": len(files), "packages": pip,
            "message": "Updated. Restart the app to run the new version."}


def _restore(replaced: list, added: list) -> None:
    prev = _work() / "previous"
    for rel in replaced:
        if (prev / rel).exists():
            shutil.copy2(prev / rel, ROOT / rel)
    for rel in added:
        try:
            (ROOT / rel).unlink()
        except FileNotFoundError:
            pass


def roll_back() -> dict:
    """Put back exactly what the last update replaced, and remove what it added."""
    try:
        last = json.loads((_work() / "last.json").read_text("utf-8"))
    except Exception:
        return {"ok": False, "error": "There's no update to roll back."}
    if not (_work() / "previous").exists():
        return {"ok": False, "error": "The files from before the update are gone."}
    _restore(last.get("replaced") or [], last.get("added") or [])
    shutil.rmtree(_work() / "previous", ignore_errors=True)
    (_work() / "last.json").unlink()
    return {"ok": True, "back_to": last.get("from"),
            "message": "Rolled back. Restart the app."}
