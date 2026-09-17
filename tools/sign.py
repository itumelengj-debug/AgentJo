"""Sign and verify Agent Jo releases.

A release is a directory tree plus two artefacts, all relative to the repo
root (the folder that *contains* this ``tools/`` directory):

- ``MANIFEST.sha256``  — one ``<sha256>  <relpath>`` line per tracked file, sorted.
- ``MANIFEST.sig``     — an Ed25519 signature (base64) over the manifest bytes.
- ``signing-key.pub``  — the Ed25519 public key (PEM) anyone can verify against.

The private key (``signing-key.private``, PEM) never leaves the publisher's
machine and is gitignored.

Commands (see SIGNING.md / VERIFYING.md):

    python tools/sign.py --make-key   # once; writes .private (gitignored) + .pub
    python tools/sign.py --sign       # before each release; writes MANIFEST.*
    python tools/sign.py --verify     # what a downloader runs

This is deliberately dependency-light: only ``cryptography`` is required, and
only the ``hashlib`` stdlib is used for the manifest so verification of the
manifest itself never depends on the signing library.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover - reported as a clear message
    _HAS_CRYPTO = False

# Artefact filenames, relative to the repo root.
MANIFEST_NAME = "MANIFEST.sha256"
SIG_NAME = "MANIFEST.sig"
PUB_NAME = "signing-key.pub"
PRIV_NAME = "signing-key.private"

# These are the signing artefacts themselves (and the private key): they are
# never part of the signed content.
_ARTEFACTS = {MANIFEST_NAME, SIG_NAME, PUB_NAME, PRIV_NAME}

# Paths that are never part of the release content.
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
              ".mypy_cache", "node_modules", "dist", "build"}


def _repo_root() -> Path:
    """The directory containing this tools/ folder."""
    here = Path(__file__).resolve()
    return here.parent.parent


def _walk_files(root: Path):
    """Yield every file that is part of the signed release, as (relpath, path).

    Paths are normalised to forward slashes so a manifest is identical whether
    it was produced on Windows, macOS or Linux.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # prune skippable directories in place (tools/ IS part of the release)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in filenames:
            if fn in _ARTEFACTS:
                continue
            rel = (rel_dir / fn).as_posix()
            if rel == ".":
                continue
            yield rel, Path(dirpath) / fn


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path) -> bytes:
    """Return the canonical MANIFEST.sha256 bytes for the tree at ``root``."""
    entries = []
    for rel, path in _walk_files(root):
        entries.append((rel, _file_sha256(path)))
    entries.sort(key=lambda e: e[0])
    lines = [f"{digest}  {rel}" for rel, digest in entries]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _load_pub(root: Path) -> Ed25519PublicKey:
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "cryptography is not installed. Run: pip install cryptography")
    p = root / PUB_NAME
    if not p.exists():
        raise FileNotFoundError(
            f"{PUB_NAME} not found — this release is unsigned (or the public "
            f"key was not committed).")
    return serialization.load_pem_public_key(p.read_bytes())


def _load_priv(root: Path) -> Ed25519PrivateKey:
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "cryptography is not installed. Run: pip install cryptography")
    p = root / PRIV_NAME
    if not p.exists():
        raise FileNotFoundError(
            f"{PRIV_NAME} not found. Run `python tools/sign.py --make-key` "
            f"first (see SIGNING.md).")
    return serialization.load_pem_private_key(p.read_bytes(), password=None)


def cmd_make_key(root: Path) -> int:
    if not _HAS_CRYPTO:
        print("error: cryptography is not installed. Run: pip install cryptography")
        return 1
    priv_path = root / PRIV_NAME
    pub_path = root / PUB_NAME
    if priv_path.exists() or pub_path.exists():
        print(f"error: a key already exists ({PRIV_NAME} / {PUB_NAME}).")
        print("       Refusing to overwrite — a new key invalidates every "
              "published release.")
        return 1

    key = Ed25519PrivateKey.generate()
    priv_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_bytes)
    pub_path.write_bytes(pub_bytes)
    # best-effort: keep the private half unreadable by others where the OS allows
    try:
        os.chmod(priv_path, 0o600)
    except Exception:
        pass

    print(f"wrote {PRIV_NAME} (keep secret, never commit — it is gitignored)")
    print(f"wrote {PUB_NAME}  (commit this)")
    print()
    print("Back up the private half somewhere you won't lose it.")
    return 0


def cmd_sign(root: Path) -> int:
    manifest = build_manifest(root)
    priv = _load_priv(root)
    sig = priv.sign(manifest)
    (root / MANIFEST_NAME).write_bytes(manifest)
    (root / SIG_NAME).write_bytes(base64.b64encode(sig) + b"\n")
    n = manifest.count(b"\n")
    print(f"wrote {MANIFEST_NAME} ({n} files)")
    print(f"wrote {SIG_NAME}")
    print()
    print("Commit these files:")
    print(f"  git add {MANIFEST_NAME} {SIG_NAME} {PUB_NAME}")
    return 0


def cmd_verify(root: Path) -> int:
    manifest_path = root / MANIFEST_NAME
    sig_path = root / SIG_NAME
    pub_path = root / PUB_NAME

    if not pub_path.exists():
        print("ok: False")
        print("verdict: Unsigned — no signing-key.pub present.")
        return 1
    if not manifest_path.exists() or not sig_path.exists():
        print("ok: False")
        print("verdict: Unsigned — MANIFEST.sha256 / MANIFEST.sig missing.")
        return 1

    pub = _load_pub(root)
    expected_manifest = manifest_path.read_bytes()
    try:
        sig = base64.b64decode(sig_path.read_bytes().strip())
    except Exception:
        print("ok: False")
        print("verdict: Corrupt — MANIFEST.sig is not valid base64.")
        return 1

    try:
        pub.verify(sig, expected_manifest)
    except InvalidSignature:
        print("ok: False")
        print("verdict: Invalid signature — the manifest was not signed by "
              "the holder of signing-key.pub.")
        return 1

    # signature is valid; now check the actual files match the manifest
    actual = build_manifest(root)
    expected_map = {}
    for line in expected_manifest.decode("utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        expected_map[rel] = digest
    actual_map = {}
    for line in actual.decode("utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        actual_map[rel] = digest

    missing = sorted(set(expected_map) - set(actual_map))
    added = sorted(set(actual_map) - set(expected_map))
    changed = sorted(
        rel for rel in set(expected_map) & set(actual_map)
        if expected_map[rel] != actual_map[rel]
    )

    if missing or added or changed:
        print("ok: False")
        print("verdict: Altered — this copy does not match the signed manifest.")
        for rel in missing:
            print(f"  removed:  {rel}")
        for rel in added:
            print(f"  added:    {rel}")
        for rel in changed:
            print(f"  changed:  {rel}")
        if any("agent/tools.py" in r for r in changed):
            print()
            print("  NOTE: agent/tools.py was modified — this is a reason to "
                  "stop before running anything.")
        return 1

    n = len(expected_map)
    print("ok: True")
    print(f"verdict: Signed and unaltered — every one of {n} files matches.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sign and verify Agent Jo releases.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--make-key", action="store_true",
                       help="generate signing-key.private + signing-key.pub")
    group.add_argument("--sign", action="store_true",
                       help="write MANIFEST.sha256 + MANIFEST.sig")
    group.add_argument("--verify", action="store_true",
                       help="check this copy against the published signature")
    args = parser.parse_args(argv)

    root = _repo_root()
    if args.make_key:
        return cmd_make_key(root)
    if args.sign:
        return cmd_sign(root)
    return cmd_verify(root)


if __name__ == "__main__":
    sys.exit(main())
