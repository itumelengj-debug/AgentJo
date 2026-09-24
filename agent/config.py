"""Central configuration. Everything is overridable via environment variables."""

import json
import os
from pathlib import Path


def _bool(env_var: str, default: bool) -> bool:
    val = os.environ.get(env_var)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


# Identity
AGENT_NAME = os.environ.get("AGENT_NAME", "Agent Jo")

# Where the agent keeps its brain (database, logs). Survives restarts.
AGENT_HOME = Path(os.environ.get("AGENT_HOME", str(Path.home() / ".local_agent")))
DB_PATH = AGENT_HOME / "agent.db"

# Backend: "anthropic" (Claude API), "ollama" (local model), or "hybrid"
# (local Ollama for simple turns + background work, Claude for hard turns —
# saves API spend). Hybrid uses MODEL for Claude and OLLAMA_MODEL for local.
# "auto" — whichever engine you have configured, resolved when a brain is
# built rather than fixed here. This used to say "anthropic", which made one
# vendor structurally the default: a user with a DeepSeek key still got an
# Anthropic brain constructed first, and four separate fixes in as many
# builds were all downstream of this single line. Anthropic is now one
# option among several, reached when you have a key for it and nothing you
# chose yourself.
BACKEND = os.environ.get("AGENT_BACKEND", "auto")

# Anthropic models — https://platform.claude.com/docs/en/about-claude/models/overview
# MODEL powers conversation; FAST_MODEL handles cheap background memory extraction.
# Only consulted when the Anthropic backend is actually in use. It stays a
# Claude id because it IS the Anthropic model field — naming another
# vendor's model here is the bug `repair_model()` exists to undo.
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")
FAST_MODEL = os.environ.get("AGENT_FAST_MODEL", "claude-haiku-4-5-20251001")

# Ollama backend (https://ollama.com). Pick a tool-capable model.
# After fine-tuning, point AGENT_OLLAMA_MODEL at your custom model (e.g. atlas-tuned).
OLLAMA_HOST = os.environ.get("AGENT_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("AGENT_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_FAST_MODEL = os.environ.get("AGENT_OLLAMA_FAST_MODEL", "")  # default: same model
# Which engine the UI starts on (and remembers). "Auto" routes; or pin a specific
# engine id like "Claude", "Ollama", "DeepSeek Pro", or a custom engine's name.
DEFAULT_ENGINE = os.environ.get("AGENT_DEFAULT_ENGINE", "Auto")
# A second local model offered as its own engine in the UI (alongside the
# primary local model). Must be pulled in Ollama first: `ollama pull qwen3.6`.
OLLAMA_MODEL_2 = os.environ.get("AGENT_OLLAMA_MODEL_2", "qwen3.6")

# DeepSeek V4 (and any OpenAI-compatible endpoint) offered as extra engines.
# The API is OpenAI-format, so set the base URL + key for your provider:
#   - DeepSeek official: base https://api.deepseek.com/v1, models deepseek-v4-pro / deepseek-v4-flash
#   - NVIDIA (default):  base https://integrate.api.nvidia.com/v1, models deepseek-ai/deepseek-v4-pro / -flash
#   - OpenRouter:        base https://openrouter.ai/api/v1, models deepseek/deepseek-v4-pro / -flash
# The model IDs must match the chosen provider. Engines appear in the UI only
# when a key is set. Changing these needs a restart.
DEEPSEEK_API_KEY = os.environ.get("AGENT_DEEPSEEK_KEY",
                                  os.environ.get("DEEPSEEK_API_KEY", ""))
DEEPSEEK_BASE_URL = os.environ.get("AGENT_DEEPSEEK_BASE_URL",
                                   "https://integrate.api.nvidia.com/v1")
DEEPSEEK_MODEL_PRO = os.environ.get("AGENT_DEEPSEEK_MODEL_PRO",
                                    "deepseek-ai/deepseek-v4-pro")
DEEPSEEK_MODEL_FLASH = os.environ.get("AGENT_DEEPSEEK_MODEL_FLASH",
                                      "deepseek-ai/deepseek-v4-flash")
# Optional per-model overrides, for when Pro and Flash use different keys or
# even different providers/endpoints. Each falls back to the shared values
# above when left blank, so a single key still works for both.
DEEPSEEK_API_KEY_PRO = os.environ.get("AGENT_DEEPSEEK_KEY_PRO", "")
DEEPSEEK_API_KEY_FLASH = os.environ.get("AGENT_DEEPSEEK_KEY_FLASH", "")
DEEPSEEK_BASE_URL_PRO = os.environ.get("AGENT_DEEPSEEK_BASE_URL_PRO", "")
DEEPSEEK_BASE_URL_FLASH = os.environ.get("AGENT_DEEPSEEK_BASE_URL_FLASH", "")


def deepseek_creds(model):
    """(api_key, base_url) for a DeepSeek model: per-model override if set,
    otherwise the shared key/url. Unknown models fall back to the shared pair."""
    if model == DEEPSEEK_MODEL_FLASH:
        return (DEEPSEEK_API_KEY_FLASH or DEEPSEEK_API_KEY,
                DEEPSEEK_BASE_URL_FLASH or DEEPSEEK_BASE_URL)
    return (DEEPSEEK_API_KEY_PRO or DEEPSEEK_API_KEY,
            DEEPSEEK_BASE_URL_PRO or DEEPSEEK_BASE_URL)


def deepseek_pro_key():
    return DEEPSEEK_API_KEY_PRO or DEEPSEEK_API_KEY


def deepseek_flash_key():
    return DEEPSEEK_API_KEY_FLASH or DEEPSEEK_API_KEY

# Behaviour
# Stamped when this build was packaged. Surfaced in problem reports so
# "is the fix actually running?" is answerable at a glance — replacing
# files without restarting the app has burned us more than once.
BUILD_ID = "2026-09-24 00:08 UTC"

MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "8192"))
# Ollama context window (prompt + reply budget). Ollama defaults this to only
# 2048 tokens, which silently truncates long replies on local models no matter
# how high MAX_TOKENS is. Set it to something the model actually supports.
OLLAMA_NUM_CTX = int(os.environ.get("AGENT_OLLAMA_NUM_CTX", "8192"))
MAX_HISTORY_TURNS = int(os.environ.get("AGENT_MAX_HISTORY_TURNS", "20"))
AUTO_LEARN = _bool("AGENT_AUTO_LEARN", True)        # extract memories after each turn
MAX_MEMORIES_IN_CONTEXT = int(os.environ.get("AGENT_MAX_MEMORIES", "12"))
MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "15"))

# Per-turn wall-clock timeout (seconds). If a turn runs longer (a stuck model
# call, a runaway tool loop, an unreachable local model), it is stopped with a
# clear message instead of spinning forever. 0 disables.
TURN_TIMEOUT = int(os.environ.get("AGENT_TURN_TIMEOUT", "300"))

# Smart self-recovery: under Auto, when a turn keeps hitting tool errors or
# repeating the same call (i.e. it's stuck), nudge the model to change approach
# and, if it keeps struggling, escalate to a smarter engine by capability score.
AUTO_ESCALATE = _bool("AGENT_AUTO_ESCALATE", True)

# Optional capability-score overrides by engine label, e.g.
# AGENT_ENGINE_SCORES='{"Ollama": 50, "my-engine": 90}'. Higher = smarter.
try:
    ENGINE_SCORES = json.loads(os.environ.get("AGENT_ENGINE_SCORES", "") or "{}")
    if not isinstance(ENGINE_SCORES, dict):
        ENGINE_SCORES = {}
except Exception:
    ENGINE_SCORES = {}

# Sub-agents: spawn an isolated worker with its own clean context for a
# focused objective; only its conclusion returns to the parent. Keeps long
# tasks coherent by not flooding the main context with intermediate churn.
SUBAGENTS = _bool("AGENT_SUBAGENTS", True)
# Cost-tiered teamwork: when on (and a local model exists), the cloud engine is
# coached to hand bulk mechanical work to the FREE local model, and sub-agents
# run on the local tier by default, escalating back to the primary engine only
# if the local worker struggles. Off = behaviour unchanged.
TEAMWORK = _bool("AGENT_TEAMWORK", False)
# Privacy shield: "off" | "mask" (placeholder-mask sensitive data before it
# reaches any engine, restore in replies/tools) | "local" (route turns that
# contain sensitive data entirely to the local model; masks if none running).
PRIVACY_MODE = os.environ.get("AGENT_PRIVACY", "off").strip().lower()
# Audit trail: append-only, hash-chained record of turns, tool calls,
# autonomous actions and config changes (see agent/audit.py). On by default.
AUDIT = _bool("AGENT_AUDIT", True)
# Time Machine: snapshot files before the agent overwrites them (⟲ Undo).
TIMEMACHINE = _bool("AGENT_TIMEMACHINE", True)
# Self-improvement pipeline (sandbox -> tests -> human-approved apply).
SELFIMPROVE = _bool("AGENT_SELFIMPROVE", True)
# Turbo: draft locally, escalate to the cloud engine only when the local
# answer fails a deterministic quality gate.
TURBO = _bool("AGENT_TURBO", False)
# Blender lab: path to the Blender executable ("" = auto-detect).
# --- branding -------------------------------------------------------------- #
# Whose product this is. The footer said "backend: anthropic", which named a
# supplier where the product's own name belongs — and if this is shipped to a
# client, the supplier is an implementation detail, not the masthead.
# Hold externally-mutating tool calls for review under full access and
# unattended runs. Reads always run — asking about those teaches you to click
# through prompts without reading them.
INTERCEPTS = os.environ.get("AGENT_INTERCEPTS", "1") not in ("0", "false")

# Notifications. Three levels rather than a single switch, because "off" for
# a routine confirmation and "off" for a failure are different requests — and
# silencing the second is how you find out a week later that nothing ran.
#   all       every confirmation
#   important errors, things held for review, anything waiting on you
#   off       nothing at all
NOTIFY_LEVEL = os.environ.get("AGENT_NOTIFY_LEVEL", "important").strip().lower()

# Desktop notifications when the window isn't focused. Off by default: an app
# that asks for notification permission the moment it loads is an app people
# refuse out of reflex.
NOTIFY_DESKTOP = os.environ.get("AGENT_NOTIFY_DESKTOP", "off").strip().lower() \
    in ("1", "on", "true", "yes")

BRAND_NAME = os.environ.get("AGENT_BRAND", "Symbolic Synapse")
BRAND_TAGLINE = os.environ.get("AGENT_TAGLINE", "AI, BI & data")
BRAND_URL = os.environ.get("AGENT_BRAND_URL", "")

BLENDER_PATH = os.environ.get("AGENT_BLENDER_PATH", "")
# Neural photo-to-3D: command template for a locally-installed
# TripoSR-class tool, with {image} and {out} placeholders.
NEURAL3D_CMD = os.environ.get("AGENT_NEURAL3D_CMD", "")
SUBAGENT_MAX_ROUNDS = int(os.environ.get("AGENT_SUBAGENT_MAX_ROUNDS", "8"))

# Context compaction: when the conversation grows past TRIGGER turns, fold
# the oldest turns into a fast-model summary and keep the most recent KEEP
# turns verbatim — so the agent stops "forgetting" what happened earlier.
COMPACT_TRIGGER_TURNS = int(os.environ.get("AGENT_COMPACT_TRIGGER", "20"))
COMPACT_KEEP_TURNS = int(os.environ.get("AGENT_COMPACT_KEEP", "10"))

# Documents / RAG: index your own files locally and let the agent search them.
# Embeddings run on a LOCAL Ollama model (nothing leaves the machine); if it's
# unavailable, retrieval falls back to keyword search.
RAG_DB_PATH = AGENT_HOME / "documents.db"
EMBED_MODEL = os.environ.get("AGENT_EMBED_MODEL", "nomic-embed-text")
RAG_CHUNK_CHARS = int(os.environ.get("AGENT_RAG_CHUNK_CHARS", "900"))
RAG_CHUNK_OVERLAP = int(os.environ.get("AGENT_RAG_OVERLAP", "120"))
RAG_TOP_K = int(os.environ.get("AGENT_RAG_TOP_K", "5"))
RAG_MAX_FILES = int(os.environ.get("AGENT_RAG_MAX_FILES", "500"))
RAG_MAX_FILE_MB = float(os.environ.get("AGENT_RAG_MAX_FILE_MB", "10"))
# Auto-watch: how often (seconds) to re-scan watched folders for new/changed/
# deleted files. Re-scans are cheap (unchanged files are skipped). 0 disables
# the background watcher (manual "Rescan now" still works).
RAG_WATCH_INTERVAL = int(os.environ.get("AGENT_WATCH_INTERVAL", "300"))

# Voice (local, optional, private). TTS uses the OS voice (no pip install on
# Windows/macOS) and plays on the machine running Agent Jo. STT uses faster-whisper
# if installed (pip install faster-whisper) and runs locally. TTS is opt-in.
VOICE_TTS_DEFAULT = os.environ.get("AGENT_TTS", "off").strip().lower() in (
    "1", "true", "yes", "on")
VOICE_STT_MODEL = os.environ.get("AGENT_STT_MODEL", "base")

# Chat file uploads: cap extracted text per file so a large document can't
# overflow the context window (Claude has lots of room; local models less so).
ATTACH_MAX_CHARS = int(os.environ.get("AGENT_ATTACH_MAX_CHARS", "60000"))

# Memory dedup: cosine threshold for treating two memories as the same fact
# when a local embedding model is available (paraphrases share few words).
MEMORY_DEDUP_JACCARD = float(os.environ.get("AGENT_DEDUP_JACCARD", "0.5"))
MEMORY_DEDUP_SIM = float(os.environ.get("AGENT_DEDUP_SIM", "0.80"))

# Estimated Claude pricing (USD per MILLION tokens) for the cost view. These
# are estimates only — adjust to your actual plan/model pricing if it differs.
PRICE_IN_PER_M = float(os.environ.get("AGENT_PRICE_IN", "3.0"))
PRICE_OUT_PER_M = float(os.environ.get("AGENT_PRICE_OUT", "15.0"))
# DeepSeek pricing (USD per MILLION tokens). Defaults track DeepSeek's published
# rates; override if yours differ. Custom engines carry their own prices.
PRICE_DEEPSEEK_IN_PER_M = float(os.environ.get("AGENT_PRICE_DEEPSEEK_IN", "0.28"))
PRICE_DEEPSEEK_OUT_PER_M = float(os.environ.get("AGENT_PRICE_DEEPSEEK_OUT", "1.10"))
# Session spend cap (USD). 0 = no cap. When set, autonomous actions (auto-pilot,
# watchers, auto-resume) stop once the running estimate reaches it; interactive
# chat is only warned, never blocked.
BUDGET_USD = float(os.environ.get("AGENT_BUDGET_USD", "0"))

# Web access: live search (DuckDuckGo, no API key) and page fetching.
# Privacy note: with web access on, search queries and page requests leave
# this machine. The Chat tab has an on/off toggle; AGENT_WEB sets the default.
WEB_SEARCH_RESULTS = int(os.environ.get("AGENT_WEB_RESULTS", "5"))
WEB_FETCH_MAX_CHARS = int(os.environ.get("AGENT_WEB_FETCH_CHARS", "6000"))
# Refuse fetching private/loopback addresses (SSRF guard). Override only if
# you deliberately want the agent reading e.g. a local dev server.
ALLOW_LOCAL_FETCH = os.environ.get("AGENT_ALLOW_LOCAL_FETCH", "").strip().lower() in (
    "1", "true", "yes", "on")

# Routing feedback: when you click "Retry on Claude" on a weak local answer,
# Agent Jo logs that prompt and afterwards routes similar prompts to Claude in
# Auto mode. Similarity uses local embeddings (cosine >= ESCALATE_SIM); if no
# embedding model is available it falls back to token overlap (>= ESCALATE_TOKEN).
ROUTE_FEEDBACK = os.environ.get("AGENT_ROUTE_FEEDBACK", "on").strip().lower() not in (
    "0", "off", "false", "no")
ROUTE_ESCALATE_SIM = float(os.environ.get("AGENT_ROUTE_ESCALATE_SIM", "0.82"))
ROUTE_ESCALATE_TOKEN = float(os.environ.get("AGENT_ROUTE_ESCALATE_TOKEN", "0.6"))

# Prompt caching (Anthropic backend): cuts repeated-context cost ~90%.
PROMPT_CACHE = _bool("AGENT_PROMPT_CACHE", True)

# Stream replies token-by-token instead of waiting for the full answer.
STREAM = _bool("AGENT_STREAM", True)

# Model routing: triage each turn with the fast model and send obviously
# simple ones to it; anything stateful, long, or doubtful stays on the main
# model. Saves money/latency; disable with AGENT_ROUTING=0 or --no-route.
ROUTING = _bool("AGENT_ROUTING", True)

# Safety: commands/file-writes require a y/n confirmation unless auto-approve is on
AUTO_APPROVE = _bool("AGENT_AUTO_APPROVE", False)
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("AGENT_COMMAND_TIMEOUT", "120"))

# Output truncation limits for tool results fed back to the model
MAX_FILE_READ_CHARS = 60_000
MAX_COMMAND_OUTPUT_CHARS = 12_000


# --------------------------------------------------------------------------- #
# User-adjustable settings (the Settings tab)
#
# These are read fresh on every turn, so changing them takes effect on the next
# message without a restart. They are persisted to a small JSON file in
# AGENT_HOME and re-applied at startup, overriding the env defaults above. Model
# names, the backend, and the Ollama host are NOT here: they are baked into the
# engine at launch and need a restart (set the AGENT_* env vars) to change.
# --------------------------------------------------------------------------- #
SETTINGS_FILE = AGENT_HOME / "settings.json"
_USER_KEYS = [
    "AUTO_LEARN", "ROUTING", "PROMPT_CACHE", "SUBAGENTS", "AUTO_ESCALATE",
    "TURN_TIMEOUT", "MAX_TOOL_ROUNDS", "MAX_MEMORIES_IN_CONTEXT",
    "MAX_TOKENS", "MAX_HISTORY_TURNS", "OLLAMA_NUM_CTX",
    "COMPACT_TRIGGER_TURNS", "COMPACT_KEEP_TURNS", "BUDGET_USD",
    "MODEL", "FAST_MODEL", "OLLAMA_MODEL", "OLLAMA_FAST_MODEL",
    "DEFAULT_ENGINE", "TEAMWORK", "PRIVACY_MODE", "AUDIT", "TIMEMACHINE",
    "SELFIMPROVE", "TURBO", "BLENDER_PATH", "NEURAL3D_CMD",
    "BRAND_NAME", "BRAND_TAGLINE", "BRAND_URL", "INTERCEPTS",
    "NOTIFY_LEVEL", "NOTIFY_DESKTOP",
    "RAG_MAX_FILES", "RAG_MAX_FILE_MB",
]
_DEFAULTS: dict = {}


def _coerce(key: str, value):
    """Coerce a stored/incoming value to the type of the baseline default."""
    default = _DEFAULTS.get(key)
    if isinstance(default, bool):          # bool first (it subclasses int)
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def current_settings() -> dict:
    g = globals()
    return {k: g[k] for k in _USER_KEYS}


def load_settings() -> dict:
    """Apply persisted settings over the env/baseline defaults. Safe if the
    file is missing or malformed."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return current_settings()
    if isinstance(data, dict):
        g = globals()
        for k, v in data.items():
            if k in _USER_KEYS:
                try:
                    g[k] = _coerce(k, v)
                except Exception:
                    pass
    return current_settings()


def repair_model() -> dict:
    """Fix a MODEL that was saved as something Anthropic can't run.

    This was saved before it could be refused, so it's sitting in
    settings.json failing every turn. Correcting it on load beats making
    someone find the setting from a 404 that names a string."""
    global MODEL
    bad = str(MODEL or "").strip()
    if not bad or bad.lower().startswith(("claude-", "claude.", "anthropic.")):
        return {"ok": True, "changed": False, "model": MODEL}
    was, MODEL = bad, "claude-sonnet-4-6"
    try:
        save_settings({"MODEL": MODEL})
    except Exception:
        pass
    return {"ok": True, "changed": True, "was": was, "model": MODEL,
            "note": (f"The Claude model was set to '{was}', which Anthropic "
                     f"doesn't have — every turn was failing with a 404. "
                     f"Reset to {MODEL}. If you meant to use a different "
                     f"provider, add it under Engines.")}


def save_settings(updates: dict) -> dict:
    """Apply updates in-process and persist the full managed set to disk."""
    g = globals()
    rejected = []
    for k, v in (updates or {}).items():
        if k not in _USER_KEYS:
            continue
        # An unrecognised level matches none of the three, so it would
        # silently behave as "off": the setting would look saved and
        # notifications would simply stop.
        if k == "NOTIFY_LEVEL":
            v = str(v).strip().lower()
            if v not in ("all", "important", "off"):
                rejected.append(
                    f"'{v}' isn't a notification level. Use all, important "
                    f"or off.")
                continue
        # MODEL is the id sent to Anthropic. Saving an engine NAME here — as
        # happened with "DeepSeekReplika" — produces a 404 that names the
        # string and explains nothing, hours later and somewhere else.
        if k == "MODEL":
            m = str(v or "").strip().lower()
            if m and not m.startswith(("claude-", "claude.", "anthropic.")):
                rejected.append(
                    f"'{v}' isn't an Anthropic model id, so it was not saved. "
                    f"They look like 'claude-sonnet-4-6'. To use a different "
                    f"provider, add it under Engines instead.")
                continue
        try:
            g[k] = _coerce(k, v)
        except Exception:
            pass
    current = current_settings()
    if rejected:
        current["_rejected"] = rejected
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass
    return current


def reset_settings() -> dict:
    """Restore the env/baseline defaults and forget the saved overrides."""
    g = globals()
    for k in _USER_KEYS:
        g[k] = _DEFAULTS[k]
    try:
        if SETTINGS_FILE.exists():
            SETTINGS_FILE.unlink()
    except Exception:
        pass
    return current_settings()


_DEFAULTS = {k: globals()[k] for k in _USER_KEYS}   # baseline (env) defaults
load_settings()                                      # apply saved overrides
