# Checking this copy is genuine

Agent Jo runs commands on the machine it's installed on. A tampered build
could do real harm, so every release is signed and you can check it yourself
— offline, without an account, in about ten seconds.

```
pip install cryptography
python tools/sign.py --verify
```

You want to see:

```
ok: True
verdict: Signed and unaltered — every one of N files matches.
```

If a file differs, it names which. That matters: a changed `agent/tools.py`
is a reason to stop, while your editor rewriting line endings in a README is
not.

## What this proves, and what it doesn't

**It proves** the copy is byte-for-byte what the key holder published. Nothing
was added on the way to you.

**It doesn't prevent copying.** Anything published can be read and forked. No
signature changes that, and the [licence](LICENSE) — not the signature — is
what governs whether someone may use it.

The public key (`signing-key.pub`) is in this repository. The private half
never is.
