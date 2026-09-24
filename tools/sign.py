"""Signing a release — what it does, and what it does not.

Being clear about this first, because the words invite a misunderstanding
that costs people money:

  A signature does NOT stop anyone copying this. Anything published on
  GitHub can be read, forked and republished. No signature, watermark or
  obfuscation changes that, and anyone selling you one is selling comfort.

  What a signature DOES is prove a copy is yours and unaltered. Someone who
  downloads a zip can check that every byte came from you and nothing was
  added on the way — which matters most for an agent that runs commands on
  the machine it's installed on. A tampered build of this could do real harm,
  and the signature is how a user rules that out.

  What governs whether someone may USE it is the licence, not the signature.

So: Ed25519 over a manifest of SHA-256 file hashes. The private key never
enters the repository; the public key does, so anyone can verify without
trusting a server. Verification needs no network and no account.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "MANIFEST.sha256"
SIGNATURE = "MANIFEST.sig"
PUBKEY = "signing-key.pub"
KEYFILE = "signing-key.private"        # never committed; see .gitignore

SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist",
        "build", ".mypy_cache", ".pytest_cache"}
SKIP_FILES = {MANIFEST, SIGNATURE, KEYFILE}


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _files(root: Path) -> list:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP for part in p.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        out.append(p)
    return out


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: str = ".") -> dict:
    """Every file, with its hash. This is what gets signed."""
    r = Path(root).resolve()
    entries = {}
    for p in _files(r):
        entries[str(p.relative_to(r)).replace("\\", "/")] = _digest(p)
    body = {"created": _iso(), "files": len(entries), "entries": entries}
    text = json.dumps(body, indent=2, sort_keys=True)
    (r / MANIFEST).write_text(text, "utf-8")
    return {"ok": True, "files": len(entries), "path": str(r / MANIFEST)}


def make_key(path: str = KEYFILE) -> dict:
    """Create a signing key. Run once, then keep the private half safe.

    If it leaks, someone can sign a build as you — which is the one thing
    this is meant to prevent."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return {"ok": False,
                "error": "pip install cryptography — signing needs it."}
    p = Path(path)
    if p.exists():
        return {"ok": False,
                "error": f"{p} already exists. Signing again with the same "
                         f"key is fine; making a NEW one invalidates every "
                         f"release you've already published."}
    key = ed25519.Ed25519PrivateKey.generate()
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    try:
        p.chmod(0o600)
    except Exception:
        pass
    pub = Path(PUBKEY)
    pub.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    return {"ok": True, "private": str(p), "public": str(pub),
            "warning": ("Keep the private key out of the repository — it's "
                        "in .gitignore. Anyone holding it can sign a build "
                        "as you, which is the one thing this prevents.")}


def sign(root: str = ".", key_path: str = KEYFILE) -> dict:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return {"ok": False, "error": "pip install cryptography"}
    r = Path(root).resolve()
    k = Path(key_path)
    if not k.exists():
        return {"ok": False,
                "error": f"No signing key at {k}. Run: python tools/sign.py "
                         f"--make-key"}
    build_manifest(root)
    data = (r / MANIFEST).read_bytes()
    key = serialization.load_pem_private_key(k.read_bytes(), password=None)
    (r / SIGNATURE).write_bytes(key.sign(data))
    return {"ok": True, "manifest": MANIFEST, "signature": SIGNATURE,
            "note": ("Commit the manifest, the signature and the public key. "
                     "Never the private key.")}


def verify(root: str = ".") -> dict:
    """Check a copy is unaltered and came from the holder of the key.

    Deliberately reports WHICH files differ. "Verification failed" tells you
    something is wrong; naming the file tells you whether it's tampering or
    an editor that rewrote line endings."""
    r = Path(root).resolve()
    man, sig, pub = r / MANIFEST, r / SIGNATURE, r / PUBKEY
    for f, what in ((man, "manifest"), (sig, "signature"), (pub, "public key")):
        if not f.exists():
            return {"ok": False, "error": f"No {what} here ({f.name}), so "
                                          f"this copy can't be checked."}
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return {"ok": False, "error": "pip install cryptography to verify."}

    key = serialization.load_pem_public_key(pub.read_bytes())
    try:
        key.verify(sig.read_bytes(), man.read_bytes())
    except InvalidSignature:
        return {"ok": False, "signed": False,
                "error": ("The signature doesn't match the manifest. Either "
                          "the manifest was edited, or this build wasn't "
                          "signed by the holder of that key.")}

    body = json.loads(man.read_text("utf-8"))
    changed, missing, extra = [], [], []
    listed = body.get("entries", {})
    for rel, want in listed.items():
        p = r / rel
        if not p.exists():
            missing.append(rel)
        elif _digest(p) != want:
            changed.append(rel)
    on_disk = {str(p.relative_to(r)).replace("\\", "/") for p in _files(r)}
    extra = sorted(on_disk - set(listed) - {PUBKEY})

    ok = not (changed or missing)
    return {"ok": ok, "signed": True, "files": len(listed),
            "changed": changed[:40], "missing": missing[:40],
            "added": extra[:40], "created": body.get("created", ""),
            "verdict": ("Signed and unaltered — every one of "
                        f"{len(listed)} files matches."
                        if ok else
                        f"Signature is valid, but {len(changed)} file(s) "
                        f"differ and {len(missing)} are missing. Someone has "
                        f"changed this copy since it was signed."),
            "note": ("A valid signature proves the copy is unaltered and came "
                     "from the key holder. It does not stop anyone copying "
                     "the code — the licence governs that.")}


HEADER_TEMPLATE = """{c} {product} — {tagline}
{c} Copyright (c) {year} {holder}. All rights reserved.
{c}
{c} Licensed under the PolyForm Noncommercial License 1.0.0.
{c} Free for personal, research and non-commercial use.
{c} Commercial use requires a licence: see COMMERCIAL.md
{c} Provenance: MANIFEST.sha256 + MANIFEST.sig (see VERIFYING.md)
"""


def add_headers(root: str = ".", holder: str = "Itumeleng Nthite",
                product: str = "Agent Jo", tagline: str = "by Symbolic Synapse",
                year: int = None) -> dict:
    """Put attribution in every source file.

    Not protection — anyone can delete a comment. It's so a file that ends up
    somewhere else still says where it came from, which is what makes a claim
    provable rather than assertable."""
    r = Path(root).resolve()
    year = year or datetime.now(timezone.utc).year
    done, skipped = 0, 0
    for p in _files(r):
        if p.suffix not in (".py", ".js", ".css"):
            skipped += 1
            continue
        text = p.read_text("utf-8", "replace")
        if "PolyForm Noncommercial" in text[:1200]:
            skipped += 1
            continue
        comment = "#" if p.suffix == ".py" else "//"
        head = HEADER_TEMPLATE.format(c=comment, product=product,
                                      tagline=tagline, year=year,
                                      holder=holder)
        if p.suffix == ".css":
            head = ("/*\n" + head.replace("// ", " ").replace("//", " ")
                    + "*/\n")
        # a shebang and an encoding line must stay on the first lines
        lines = text.splitlines(keepends=True)
        at = 0
        while at < len(lines) and (lines[at].startswith("#!")
                                   or "coding" in lines[at][:30]):
            at += 1
        p.write_text("".join(lines[:at]) + head + "\n" + "".join(lines[at:]),
                     "utf-8")
        done += 1
    return {"ok": True, "headers_added": done, "skipped": skipped,
            "note": ("A header doesn't stop anyone removing it. It means a "
                     "file found elsewhere still names its origin, which is "
                     "the difference between claiming authorship and showing "
                     "it.")}


def _cli():
    import argparse
    ap = argparse.ArgumentParser(
        description="Sign or verify an Agent Jo release.")
    ap.add_argument("--make-key", action="store_true",
                    help="create a signing key (run once)")
    ap.add_argument("--sign", action="store_true", help="sign this folder")
    ap.add_argument("--verify", action="store_true",
                    help="check this folder is unaltered")
    ap.add_argument("--headers", action="store_true",
                    help="add copyright headers to source files")
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    if a.make_key:
        r = make_key()
    elif a.sign:
        r = sign(a.root)
    elif a.headers:
        r = add_headers(a.root)
    elif a.verify:
        r = verify(a.root)
    else:
        ap.print_help()
        return 0
    for k, v in r.items():
        if isinstance(v, list) and not v:
            continue
        print(f"{k}: {v}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
