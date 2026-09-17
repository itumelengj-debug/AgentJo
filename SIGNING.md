# Signing your releases

The private key belongs on your machine and nowhere else. It was never
committed, and this build ships **unsigned** because the key that would sign
it is yours, not mine.

## Once

```
pip install cryptography
python tools/sign.py --make-key
```

That writes `signing-key.private` (gitignored — check it stays that way) and
`signing-key.pub` (commit this one). Back the private half up somewhere you
would not lose it: making a new key later invalidates every release you have
already published, because the public key people checked against no longer
matches.

## Before each release

```
python tools/sign.py --sign
git add MANIFEST.sha256 MANIFEST.sig signing-key.pub
```

## What anyone downloading it runs

```
python tools/sign.py --verify
```

They should see `Signed and unaltered`. If a file differs it names which —
which matters, because a changed `agent/tools.py` is a reason to stop while a
README with different line endings is not.

## What this proves

That a copy is byte-for-byte what you published. It does **not** stop anyone
copying the code — the [licence](LICENSE) governs that, not the signature.
