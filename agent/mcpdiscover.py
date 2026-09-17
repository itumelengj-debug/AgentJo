"""Finding MCP servers, and being careful about wiring them.

An MCP server is a program the agent can call tools on. Adding one is not like
adding a bookmark — it is granting arbitrary code the ability to act on your
behalf, usually with a token you supply. So this module does two things and
keeps them apart:

  It DISCOVERS. The npm registry is the real index for MCP servers, so this
  searches it live rather than shipping a list that goes stale. What comes
  back is the actual current set, with publish dates and download counts, so
  "latest" means latest rather than latest-when-this-was-written.

  It PROPOSES. Nothing is ever wired automatically. A discovered server is
  shown with what it does, who published it, what it will need from you, and
  what it would be able to reach — and then it waits. The one-click install
  writes a DISABLED entry, so even after you accept it, nothing runs until you
  turn it on and supply its credentials.

That gap is deliberate. A registry search returning a package called
`mcp-server-github` proves nothing about who published it, and an agent that
installs and enables tools it found on the internet is a bad idea however
convenient.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import config

NPM_SEARCH = "https://registry.npmjs.org/-/v1/search"
FETCH_TIMEOUT = 20.0

# Searches that actually surface MCP servers. `keywords:mcp` is the convention
# most publishers follow; the scoped name catches the official ones.
QUERIES = (
    "@modelcontextprotocol/server",
    "keywords:mcp-server",
    "mcp server",
)

# Published by the protocol's own maintainers. Worth saying, because "official"
# is the single most useful signal when the rest is a package name.
OFFICIAL_SCOPES = ("@modelcontextprotocol/",)

# What a server typically needs before it can do anything. Guessed from the
# name, and clearly labelled as a guess — being wrong here just means an extra
# question, whereas pretending to know means a broken server and no clue why.
NEEDS_HINTS = (
    (r"github", ["GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN"]),
    (r"gitlab", ["GITLAB_TOKEN"]),
    (r"slack", ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"]),
    (r"postgres|pg\b", ["a connection string"]),
    (r"sqlite", ["a path to the database file"]),
    (r"mysql|maria", ["a connection string"]),
    (r"filesystem|files?\b", ["the directories it may read"]),
    (r"google|gdrive|gmail|calendar", ["OAuth credentials"]),
    (r"notion", ["NOTION_API_KEY"]),
    (r"jira|atlassian|confluence", ["a site URL and API token"]),
    (r"sentry", ["SENTRY_AUTH_TOKEN"]),
    (r"stripe", ["STRIPE_API_KEY"]),
    (r"aws|s3", ["AWS credentials"]),
    (r"brave|search|tavily|exa", ["a search API key"]),
    (r"puppeteer|playwright|browser", ["a browser it can drive"]),
    (r"memory|knowledge", ["a storage path"]),
    (r"fetch|http", ["nothing — it fetches public pages"]),
    (r"time|weather", ["nothing, or a location"]),
)

# What accepting one actually grants. Stated in plain terms because "adds
# tools" undersells it.
REACH_HINTS = (
    (r"filesystem|files?\b", "read and write files on this machine"),
    (r"github|gitlab", "read and change your repositories"),
    (r"slack|discord|mail|gmail", "read and send messages as you"),
    (r"postgres|mysql|sqlite|maria", "query and modify that database"),
    (r"aws|s3|azure|gcp", "act on your cloud account"),
    (r"stripe|payment", "see and move money"),
    (r"browser|puppeteer|playwright", "drive a browser, including logged-in "
                                      "sessions"),
    (r"fetch|http|search|weather|time", "reach public web pages"),
)

# Packages in the MCP ecosystem that are NOT servers: the protocol libraries,
# the client, the debugging inspector. Offering these as things to install
# would be a confident wrong answer.
_LIBRARY = re.compile(
    r"(^|/)(sdk|core|node|client|spec|types?|schema|cli|inspector|proxy|"
    r"registry|template|starter|boilerplate|example)s?$", re.I)


def _is_library(name: str, description: str = "") -> bool:
    tail = (name or "").split("/")[-1]
    if _LIBRARY.search(tail) or _LIBRARY.search(name or ""):
        return True
    d = (description or "").lower()
    return any(p in d for p in ("sdk for", "client library", "typescript sdk",
                                "python sdk", "type definitions",
                                "implementation of the model context protocol"
                                " specification"))


FETCHER = None          # tests substitute this


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get(url: str, params: dict) -> dict:
    if FETCHER is not None:
        return FETCHER(url, params)
    import httpx
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url, params=params,
                  headers={"User-Agent": "AgentJo-MCP-Discovery/1.0"})
        r.raise_for_status()
        return r.json()


def _match(patterns, name: str, default):
    low = (name or "").lower()
    for rx, val in patterns:
        if re.search(rx, low):
            return val
    return default


def _age_days(iso_date: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(iso_date, fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).total_seconds() / 86400
        except Exception:
            continue
    return 1e6


def discover(limit: int = 40, include_stale: bool = False) -> dict:
    """Search the registry for MCP servers available right now."""
    seen, found, errors = {}, [], []
    for q in QUERIES:
        try:
            data = _get(NPM_SEARCH, {"text": q, "size": 60})
        except Exception as exc:
            errors.append(f"{q}: {type(exc).__name__}")
            continue
        for obj in data.get("objects", []):
            pkg = obj.get("package") or {}
            name = pkg.get("name", "")
            if not name or name in seen:
                continue
            blob = f"{name} {pkg.get('description', '')}".lower()
            # a package merely mentioning MCP isn't a server
            if "mcp" not in blob and "model context protocol" not in blob:
                continue
            # The official scope contains the SDK, the core library and the
            # client too. Offering someone `@modelcontextprotocol/sdk` as a
            # server to install is a confident wrong answer.
            if _is_library(name, pkg.get("description", "")):
                continue
            if "server" not in blob and not name.startswith(
                    "@modelcontextprotocol/"):
                continue
            age = _age_days(pkg.get("date", ""))
            if age > 400 and not include_stale:
                continue          # unmaintained is a fact worth acting on
            seen[name] = True
            official = any(name.startswith(s) for s in OFFICIAL_SCOPES)
            found.append({
                "name": name,
                "title": name.split("/")[-1].replace("server-", "")
                             .replace("mcp-", "").replace("-", " ").strip(),
                "description": (pkg.get("description") or "")[:300],
                "version": pkg.get("version", ""),
                "publisher": (pkg.get("publisher") or {}).get("username", ""),
                "official": official,
                "updated": (pkg.get("date") or "")[:10],
                "age_days": round(age),
                "npm": (pkg.get("links") or {}).get("npm", ""),
                "homepage": (pkg.get("links") or {}).get("homepage", ""),
                "popularity": round(
                    float((obj.get("score") or {}).get("detail", {})
                          .get("popularity", 0)), 3),
                "needs": _match(NEEDS_HINTS, name, ["unknown — check its "
                                                    "README"]),
                "grants": _match(REACH_HINTS, name,
                                 "whatever its tools do — read its README "
                                 "before enabling it"),
            })
    # official first, then genuinely popular, then recent
    found.sort(key=lambda s: (not s["official"], -s["popularity"],
                              s["age_days"]))
    return {"ok": True, "at": _iso(), "found": found[:limit],
            "total": len(found), "errors": errors,
            "note": ("Nothing here is installed or enabled. A package name "
                     "proves nothing about who published it — read the README "
                     "of anything you're about to give a token to.")}


def spec_for(pkg: dict) -> dict:
    """The mcp.json entry this package would need.

    Written DISABLED. Even after you accept a server, nothing runs until you
    turn it on and supply its credentials — an agent that installs and enables
    tools it found on the internet is a bad idea however convenient."""
    env = {}
    for need in pkg.get("needs", []):
        m = re.match(r"^([A-Z][A-Z0-9_]{3,})", str(need))
        if m:
            env[m.group(1)] = ""
    return {"transport": "stdio", "command": "npx",
            "args": ["-y", pkg["name"]],
            "env": env, "enabled": False}


def key_for(pkg: dict) -> str:
    base = pkg.get("title") or pkg["name"].split("/")[-1]
    return re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-") or "server"


def propose_install(pkg: dict) -> dict:
    """Add it to the config, switched off, and say what happens next."""
    from . import mcp
    if not pkg or not pkg.get("name"):
        return {"ok": False, "error": "nothing to install"}
    cfg = mcp.load_config()
    servers = cfg.setdefault("servers", {})
    key = key_for(pkg)
    if key in servers:
        return {"ok": False,
                "error": f"'{key}' is already configured — edit it in the "
                         f"MCP panel rather than adding it twice."}
    servers[key] = spec_for(pkg)
    mcp.save_config(cfg)
    _audit("mcp-added", f"{pkg['name']} as '{key}' (disabled)")
    needs = pkg.get("needs") or []
    return {
        "ok": True, "key": key, "package": pkg["name"],
        "enabled": False,
        "needs": needs,
        "grants": pkg.get("grants", ""),
        "next": ([f"Set {n}" for n in needs
                  if not n.lower().startswith("nothing")]
                 + [f"Turn '{key}' on in the MCP panel"]),
        "note": ("Added and left OFF. It can't do anything until you enable "
                 "it — and it will be able to " + pkg.get("grants", "act")
                 + "."),
    }


def _audit(name: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("mcp", name=name, summary=summary[:200])
    except Exception:
        pass


def installed_summary() -> dict:
    """What's configured now, so discovery doesn't offer duplicates."""
    from . import mcp
    servers = (mcp.load_config().get("servers") or {})
    return {"count": len(servers),
            "keys": sorted(servers),
            "enabled": sorted(k for k, v in servers.items()
                              if v.get("enabled")),
            "packages": sorted(
                (v.get("args") or ["", ""])[-1]
                for v in servers.values() if v.get("command") == "npx")}
