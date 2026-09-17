"""Engines — what's local, what's cloud, and how they cover for each other.

Until now "is this local?" was answered by looking at the NAME: a list of
substrings like ollama, qwen, llama, mistral. That is wrong in both directions
and quietly so. A qwen model served from a hosted endpoint is cloud and costs
real money, but reads as free. A local proxy someone named "claude-local" is
free, but gets billed at Anthropic rates. Cost totals, routing and the engine
indicator were all built on that guess.

So classification is done on FACTS, in this order:

  1. the engine's base_url host — loopback, .local, or an RFC-1918 address is
     local; a known provider host is cloud
  2. the built-in engines, whose nature is known outright
  3. only then, a name hint — and when it's only a hint, the answer is
     "unknown" rather than a confident wrong one

"Unknown" matters: an engine we can't classify is *not* assumed free, because
an unpriced cloud engine silently reporting $0 is the worst of the three
outcomes.

The second half is collaboration. With both kinds available the app should use
the cheap one for what it can do and the strong one for what it can't, and
neither should be a dead end: if the cloud engine is out of credit or
unreachable, work continues locally rather than failing, and it says so.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from . import config

LOCAL, CLOUD, UNKNOWN = "local", "cloud", "unknown"

# Hosts that are unambiguously a provider. Matched on the registrable host, so
# a look-alike path or query can't spoof one.
CLOUD_HOSTS = {
    "api.anthropic.com": "Anthropic",
    "api.openai.com": "OpenAI",
    "api.deepseek.com": "DeepSeek",
    "openrouter.ai": "OpenRouter",
    "api.groq.com": "Groq",
    "api.mistral.ai": "Mistral",
    "api.together.xyz": "Together",
    "api.fireworks.ai": "Fireworks",
    "api.cohere.ai": "Cohere",
    "generativelanguage.googleapis.com": "Google",
    "api.x.ai": "xAI",
    "api.perplexity.ai": "Perplexity",
}

# Only used when there is no URL to go on, and only ever to say "probably".
_LOCAL_NAME_HINTS = ("ollama", "lmstudio", "llama.cpp", "llamacpp", "vllm",
                     "localai", "jan", "gpt4all", "koboldcpp", "textgen")


def _host_of(url: str) -> str:
    try:
        u = urlparse(url if "://" in (url or "") else f"http://{url}")
        return (u.hostname or "").lower()
    except Exception:
        return ""


def host_is_local(host: str) -> bool:
    """Loopback, .local, or a private address — i.e. this machine or this
    network. Anything reachable only from here can't be billing you."""
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(
            (".local", ".lan", ".home", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or ip.is_private or ip.is_link_local)
    except ValueError:
        return False


def _custom(name: str) -> dict | None:
    try:
        from . import brain as brainmod
        for e in brainmod.load_custom_engines(refresh=True):
            if str(e.get("name", "")).strip().lower() == name.strip().lower():
                return e
    except Exception:
        pass
    return None


def classify(name: str) -> dict:
    """{kind, why, host, provider, billable} for any engine label."""
    raw = (name or "").strip()
    key = raw.lower()

    if not raw or key in ("auto", "default"):
        return {"kind": UNKNOWN, "why": "Auto picks per turn", "host": "",
                "provider": "", "billable": None, "name": raw or "Auto"}

    # 1. a custom engine's URL is the strongest evidence there is, so it is
    #    consulted FIRST — a local proxy named "claude-local" must not be
    #    classified by its name, which is the whole point of this module
    entry = _custom(raw)
    if entry:
        host = _host_of(str(entry.get("base_url", "")))
        if host_is_local(host):
            return {"kind": LOCAL,
                    "why": f"served from {host} — this machine or network",
                    "host": host, "provider": "", "billable": False,
                    "name": raw}
        for known, provider in CLOUD_HOSTS.items():
            if host == known or host.endswith("." + known):
                return {"kind": CLOUD, "why": f"hosted by {provider}",
                        "host": host, "provider": provider, "billable": True,
                        "name": raw}
        if host:
            return {"kind": CLOUD,
                    "why": f"remote host {host} — treated as billable until "
                           f"you say otherwise",
                    "host": host, "provider": "", "billable": True,
                    "name": raw}

    # 2. built-ins whose nature isn't in doubt
    if key in ("claude", "anthropic") or key.startswith(
            ("claude-", "sonnet", "opus", "haiku")):
        return {"kind": CLOUD, "why": "Anthropic's hosted API",
                "host": "api.anthropic.com", "provider": "Anthropic",
                "billable": True, "name": raw}
    if key in ("ollama", "local"):
        return {"kind": LOCAL, "why": "Ollama on this machine",
                "host": "localhost", "provider": "Ollama",
                "billable": False, "name": raw}

    # An Ollama tag is verifiably local: either it's the configured local
    # model, or the local server reports it as installed. That's a fact, not
    # a name hint.
    if key == str(getattr(config, "OLLAMA_MODEL", "")).strip().lower() and key:
        return {"kind": LOCAL, "why": "the configured local model",
                "host": "localhost", "provider": "Ollama",
                "billable": False, "name": raw}
    if any(key == m.lower() or key == m.lower().split(":")[0]
           for m in _ollama_models()):
        return {"kind": LOCAL, "why": "installed in Ollama on this machine",
                "host": "localhost", "provider": "Ollama",
                "billable": False, "name": raw}

    if "deepseek" in key and "r1:" not in key:
        return {"kind": CLOUD, "why": "DeepSeek's hosted API",
                "host": "api.deepseek.com", "provider": "DeepSeek",
                "billable": True, "name": raw}

    # 3. no URL to go on — a hint at best, and labelled as such
    if any(h in key for h in _LOCAL_NAME_HINTS):
        return {"kind": LOCAL, "why": "name matches a local runtime",
                "host": "", "provider": "", "billable": False, "name": raw}

    return {"kind": UNKNOWN,
            "why": "no endpoint recorded — add its URL under Engines so cost "
                   "and routing are right",
            "host": "", "provider": "", "billable": None, "name": raw}


def is_local(name: str) -> bool:
    return classify(name)["kind"] == LOCAL


def is_cloud(name: str) -> bool:
    return classify(name)["kind"] == CLOUD


def billable(name: str) -> bool | None:
    """True / False / None where None honestly means 'not known'."""
    return classify(name)["billable"]


# --------------------------------------------------------------------------- #
#  inventory
# --------------------------------------------------------------------------- #
def _ollama_models() -> list:
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
        return [m.get("name") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        return []


def inventory() -> dict:
    """Everything available right now, split honestly."""
    names = ["Claude"]
    try:
        from . import brain as brainmod
        names += list(brainmod.custom_engine_names())
    except Exception:
        pass
    models = _ollama_models()
    if models:
        names.append("Ollama")

    out = {"local": [], "cloud": [], "unknown": []}
    for n in names:
        c = classify(n)
        out[c["kind"]].append(c)
    out["ollama_models"] = models
    out["has_local"] = bool(out["local"])
    out["has_cloud"] = bool(out["cloud"])
    return out


# --------------------------------------------------------------------------- #
#  collaboration — neither kind should be a dead end
# --------------------------------------------------------------------------- #
_CLOUD_DEAD = re.compile(
    r"credit balance|insufficient|quota|billing|payment required|"
    r"rate.?limit|401|403|429|overloaded|unauthor|connect|timeout|"
    r"unreachable|refused", re.I)


def cloud_is_unavailable(error: str) -> bool:
    """Is this the kind of failure another engine could get past? A bad
    prompt or an oversized request would fail identically elsewhere, so
    falling back would just spend a second engine to fail twice."""
    return bool(_CLOUD_DEAD.search(str(error or "")))


def fallback_for(engine: str, error: str = "") -> dict | None:
    """The engine to try when `engine` can't do it — and only when the failure
    is one a different engine could actually survive.

    Cloud out of credit -> the local model, which costs nothing and is right
    there. Local unreachable -> the cloud one, if it's configured. Anything
    else -> nothing, because retrying elsewhere would waste tokens failing the
    same way."""
    # a retired model is permanent, and precisely the case another engine can
    # survive — it was previously treated as an ordinary error and never
    # fell back, so the same call failed identically every day
    if error and not (cloud_is_unavailable(error) or is_retired(error)):
        return None
    inv = inventory()
    kind = classify(engine)["kind"]
    if kind in (CLOUD, UNKNOWN) and inv["has_local"]:
        pick = inv["local"][0]
        return {"engine": pick["name"], "kind": LOCAL,
                "why": f"{engine} isn't answering ({_short(error)}), so this "
                       f"ran on {pick['name']} — local and free."}
    if kind == LOCAL:
        try:
            import os
            if os.environ.get("ANTHROPIC_API_KEY"):
                return {"engine": "Claude", "kind": CLOUD,
                        "why": f"the local engine isn't answering "
                               f"({_short(error)}), so this ran on Claude — "
                               f"this one costs money."}
        except Exception:
            pass
    return None


def _short(error: str) -> str:
    e = " ".join(str(error or "").split())
    low = e.lower()
    if "credit" in low or "billing" in low or "insufficient" in low:
        return "no credit"
    if "429" in low or "rate" in low:
        return "rate limited"
    if "401" in low or "403" in low or "unauthor" in low:
        return "key rejected"
    if "connect" in low or "refused" in low or "timeout" in low:
        return "not reachable"
    if is_retired(e):
        return "that model has been retired"
    return e[:60] or "unknown error"


_RETIRED = re.compile(
    r"end of life|end-of-life|no longer available|has been (?:retired|"
    r"deprecated|sunset)|model_not_found|\bdecommission", re.I)


def is_retired(error: str) -> bool:
    """A provider retiring a model is permanent.

    It read as a generic failure, so a scheduled job kept firing daily and
    failing the same way — `deepseek-v4-pro` reached end of life and every run
    since has been a wasted call. Retirement needs naming so the fix (pick
    another engine) is obvious, and so nothing retries it forever."""
    e = str(error or "")
    return bool(_RETIRED.search(e)) or "410" in e[:80]


def retired_model_name(error: str) -> str:
    m = re.search(r"[\"']?model[\"']?\s*[:=]?\s*[\"']([\w.\-/:]+)[\"']",
                  str(error or ""), re.I)
    if m:
        return m.group(1)
    m = re.search(r"[\"']([\w\-]+/[\w.\-]+)[\"']", str(error or ""))
    return m.group(1) if m else ""


def explain(error: str, engine: str = "") -> str:
    """One sentence a person can act on, for any engine failure."""
    e = " ".join(str(error or "").split())
    low = e.lower()
    who = f"'{engine}'" if engine else "that engine"
    if is_retired(e):
        name = retired_model_name(e)
        return (f"{who} points at a model that has been retired"
                + (f" ({name})" if name else "")
                + ". Pick another engine in Settings, or update this one's "
                  "model under Engines — it will keep failing until you do.")
    if "credit" in low or "billing" in low or "insufficient" in low:
        return (f"{who} has no credit. Switch to a local engine and it costs "
                f"nothing.")
    if "429" in low or "rate limit" in low:
        return f"{who} is rate limited; it should work again shortly."
    if "401" in low or "403" in low or "unauthor" in low:
        return f"{who} rejected the API key — check it in Settings."
    if "connect" in low or "refused" in low or "timeout" in low:
        return (f"Could not reach {who}. If it's local, check Ollama is "
                f"running.")
    return e[:200] or "unknown error"
