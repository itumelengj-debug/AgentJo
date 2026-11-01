"""Is this shell command provably read-only?

Kept on its own so that modules which need the answer (tools, and the checks
in agent/checks.py) don't have to import each other to get it.
"""

import shlex
from pathlib import Path

_SAFE_BINS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "which", "whoami",
    "date", "stat", "file", "du", "df", "uname", "ps", "tree", "sort", "uniq",
    "diff", "realpath", "basename", "dirname", "md5sum", "sha256sum",
    "hostname", "id", "uptime", "nproc", "free",
}
_SAFE_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files",
             "blame", "shortlog", "describe"}
_FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete",
                "-fprint", "-fprintf", "-fls"}
_SUBCOMMAND_BINS = {"git", "npm", "pip", "pip3", "cargo", "docker", "kubectl",
                    "poetry", "uv", "apt", "brew", "conda", "yarn", "pnpm", "gh"}


def is_read_only_command(command: str) -> bool:
    """Best-effort: True only when the command is provably read-only."""
    if not command or len(command) > 500:
        return False
    if any(ch in command for ch in ";&|><`$(){}\n"):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or "=" in tokens[0]:
        return False
    head = Path(tokens[0]).name.lower()
    if head == "git":
        return len(tokens) > 1 and tokens[1] in _SAFE_GIT
    if head == "find":
        return not any(t in _FIND_UNSAFE for t in tokens)
    return head in _SAFE_BINS
