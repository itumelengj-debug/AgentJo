"""A conversation as a Markdown file you can keep, send or paste somewhere.

Chats lived only inside the app: to share an answer you copied it message by
message. This writes the whole conversation as one .md file — title, date,
who said what — and, because the usual reason to export is to show someone
else, it blanks anything that looks like a secret first (API keys, tokens,
passwords written as password=…), and says how many it blanked.
"""
from __future__ import annotations

import re
import time

from . import config

# shapes of real credentials; narrow on purpose so ordinary text survives
_SECRETS = [
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),          # Anthropic / OpenAI
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                # GitHub
    re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"),              # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                        # AWS access key
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),                   # Google API key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
_ASSIGNED = re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_ ]?key|token)(\s*[:=]\s*)(\S+)")


def redact(text: str) -> tuple[str, int]:
    """Text with secrets replaced by [removed], and how many were removed."""
    n = 0
    out = text or ""
    for rx in _SECRETS:
        out, k = rx.subn("[removed]", out)
        n += k

    def _assigned(m):
        nonlocal n
        if m.group(3) == "[removed]":
            return m.group(0)
        n += 1
        return m.group(1) + m.group(2) + "[removed]"
    out = _ASSIGNED.sub(_assigned, out)
    return out, n


def filename(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")[:60] or "conversation"
    return f"{slug}.md"


def to_markdown(title: str, messages: list, *, keep_secrets: bool = False) -> dict:
    """{'text', 'removed', 'filename'} for a conversation's messages
    ([{role, text}]); tool chatter is already left out by the caller."""
    title = (title or "Conversation").strip()
    lines = [f"# {title}", "",
             f"*Exported from Agent Jo · {time.strftime('%Y-%m-%d %H:%M')} · "
             f"build {getattr(config, 'BUILD_ID', '')}*", ""]
    removed = 0
    for m in messages or []:
        who = "You" if m.get("role") == "user" else "Agent Jo"
        body = str(m.get("text") or "").strip()
        if not body:
            continue
        if not keep_secrets:
            body, k = redact(body)
            removed += k
        lines += [f"**{who}:**", "", body, "", "---", ""]
    if removed:
        lines += [f"*{removed} secret(s) — keys, tokens or passwords — were "
                  f"replaced with [removed] before export.*", ""]
    return {"text": "\n".join(lines).rstrip() + "\n", "removed": removed,
            "filename": filename(title)}
