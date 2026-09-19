"""The agent's reasoning engine — now with two interchangeable brains.

  AnthropicBrain  — Claude API. Strongest reasoning. Weights are fixed;
                    it cannot be fine-tuned by you.
  OllamaBrain     — any open-weights model served locally by Ollama
                    (https://ollama.com). Fully private, and THIS is the
                    brain you can fine-tune: see training/ for the
                    log → distill → LoRA → deploy pipeline.

Both expose the same interface, normalised to Anthropic-style content
blocks (.type / .text / .name / .input / .id) and stop_reason, so
main.py's tool loop doesn't care which brain is thinking.

Switch with:  --backend ollama   or   AGENT_BACKEND=ollama
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

from . import config
from . import crypto


class _PerThreadEngine:
    """Keeps ``last_engine`` / ``last_usage`` per OS-thread.

    One brain instance is shared across concurrent turns (each runs in its own
    worker thread). Storing the "which engine answered / how many tokens" markers
    as plain instance attributes lets one turn overwrite another's just before it
    is read. Backing them with a thread-local makes each turn see only its own.
    """

    @property
    def _engine_tl(self) -> threading.local:
        tl = self.__dict__.get("__engine_tl")
        if tl is None:
            tl = self.__dict__["__engine_tl"] = threading.local()
        return tl

    @property
    def last_engine(self):
        return getattr(self._engine_tl, "engine", None)

    @last_engine.setter
    def last_engine(self, value):
        self._engine_tl.engine = value

    @property
    def last_usage(self):
        return getattr(self._engine_tl, "usage", None)

    @last_usage.setter
    def last_usage(self, value):
        self._engine_tl.usage = value

EXTRACTION_SYSTEM = """You maintain the long-term memory of a personal AI assistant.

You will be shown the latest exchange between the user and the assistant.
Extract any DURABLE information worth remembering across future sessions:
- stable facts about the user (job, projects, environment, people, tools)
- preferences ("user prefers...", "user dislikes...")
- standing instructions or corrections ("always...", "never...", "from now on...")

Rules:
- One memory per line, plain text, no bullets or numbering.
- Write in concise third person, e.g. "User prefers tabs over spaces in Python."
- Only durable info. Ignore one-off task details, pleasantries, and anything
  that will be stale tomorrow.
- Do not repeat anything in the KNOWN MEMORIES list.
- Maximum 4 lines.
- If there is nothing durable, output exactly: NONE"""


def _extraction_prompt(user_text: str, assistant_text: str,
                       known_memories: list[str]) -> str:
    known = "\n".join(f"- {m}" for m in known_memories[:30]) or "(none yet)"
    return (
        f"KNOWN MEMORIES:\n{known}\n\n"
        f"LATEST EXCHANGE:\n"
        f"User: {user_text[:3000]}\n"
        f"Assistant: {assistant_text[:1500]}"
    )


def _parse_facts(text: str) -> list[str]:
    text = (text or "").strip()
    if not text or text.upper().startswith("NONE"):
        return []
    facts = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if len(line) > 8 and not line.upper().startswith("NONE"):
            facts.append(line)
    return facts[:4]


SUMMARY_SYSTEM = (
    "You compress a conversation excerpt for an assistant's memory. Produce "
    "concise notes (plain text, under 200 words) capturing: facts established "
    "about the user or task, decisions made, actions taken and their outcomes, "
    "and any unresolved threads or next steps. Omit pleasantries. Write as terse "
    "factual bullets in prose form, not dialogue.")

ROUTER_SYSTEM = (
    "You route requests for a personal agent between a fast small model and a "
    "strong main model. SIMPLE = greeting, small talk, a single short factual "
    "question, quick recall, a trivial rewrite. COMPLEX = anything involving "
    "files, shell commands, code, multiple steps, planning, analysis, teaching, "
    "long content, or any doubt at all. Reply with exactly one word: "
    "SIMPLE or COMPLEX.")


def _system_texts(system) -> list[str]:
    """Accept a plain string or a [static, dynamic] list of prompt parts."""
    if isinstance(system, str):
        return [system]
    return [t for t in system if t and t.strip()]


def _blk(b, key, default=None):
    """Read a field from a content block that may be a dict OR an SDK object."""
    if isinstance(b, dict):
        return b.get(key, default)
    return getattr(b, key, default)


def _sanitize_tool_pairs(messages: list) -> list:
    """Guarantee Anthropic tool-pairing validity. Anthropic requires every
    assistant `tool_use` to be followed immediately by a user message with a
    matching `tool_result`, and every `tool_result` to follow its `tool_use`.
    Local backends are lenient, so a stored history can pick up orphans (e.g. a
    tool that errored, or a turn interrupted, before its result was recorded),
    which then break the Claude API on later turns or on Retry. This repairs the
    sequence by synthesising any missing tool_result and dropping orphan
    tool_result blocks. Builds new message dicts; never mutates the input."""
    out: list = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        content = m.get("content")
        role = m.get("role")
        if role == "assistant" and isinstance(content, list):
            tool_ids = [_blk(b, "id") for b in content
                        if _blk(b, "type") == "tool_use" and _blk(b, "id")]
            if tool_ids:
                out.append({"role": "assistant", "content": content})
                nxt = messages[i + 1] if i + 1 < n else None
                consumed = False
                provided = []
                if (nxt and nxt.get("role") == "user"
                        and isinstance(nxt.get("content"), list)
                        and any(_blk(b, "type") == "tool_result"
                                for b in nxt["content"])):
                    provided = [b for b in nxt["content"]
                                if _blk(b, "type") == "tool_result"
                                and _blk(b, "tool_use_id") in tool_ids]
                    consumed = True
                have = {_blk(b, "tool_use_id") for b in provided}
                for tid in tool_ids:
                    if tid not in have:
                        provided.append({"type": "tool_result", "tool_use_id": tid,
                                         "content": "(no result recorded)"})
                rank = {tid: k for k, tid in enumerate(tool_ids)}
                provided.sort(key=lambda b: rank.get(_blk(b, "tool_use_id"), 0))
                out.append({"role": "user", "content": provided})
                i += 2 if consumed else 1
                continue
            out.append(m)
            i += 1
            continue
        if role == "user" and isinstance(content, list):
            non_results = [b for b in content
                           if _blk(b, "type") != "tool_result"]
            if len(non_results) != len(content):     # had orphan tool_result(s)
                if non_results:
                    out.append({"role": "user", "content": non_results})
                i += 1
                continue
        out.append(m)
        i += 1
    return out


def _enforce_openai_tool_invariant(seq: list) -> list:
    """OpenAI/DeepSeek require every assistant `tool_calls` message to be followed
    immediately by one `tool` message per tool_call_id, and reject any `tool`
    message that isn't responding to a preceding tool call. After converting from
    our internal format an imperfect history can violate this (an interrupted turn
    leaves a tool call with no result; a fallback id can drift). This final pass
    guarantees the invariant: it pairs each call to its response in order,
    synthesises a placeholder for any missing one, and drops orphan tool messages.
    """
    fixed: list = []
    i, n = 0, len(seq)
    while i < n:
        msg = seq[i]
        role = msg.get("role")
        if role == "tool":                       # orphan (no preceding tool_calls)
            i += 1
            continue
        if role == "assistant" and msg.get("tool_calls"):
            fixed.append(msg)
            need = [c.get("id") for c in msg["tool_calls"]]
            j = i + 1
            provided: dict = {}
            while j < n and seq[j].get("role") == "tool":
                provided.setdefault(seq[j].get("tool_call_id"), seq[j])
                j += 1
            for cid in need:
                fixed.append(provided.get(cid) or
                             {"role": "tool", "tool_call_id": cid,
                              "content": "(no result recorded)"})
            i = j
            continue
        fixed.append(msg)
        i += 1
    return fixed


def _merge_consecutive(messages: list) -> list:
    """Merge neighbouring same-role messages (Anthropic requires alternation).
    Only triggers on repaired histories; normal alternating turns are untouched."""
    out: list = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            prev = out[-1]
            pc, mc = prev["content"], m["content"]
            pb = pc if isinstance(pc, list) else [{"type": "text", "text": pc}]
            mb = mc if isinstance(mc, list) else [{"type": "text", "text": mc}]
            prev["content"] = pb + mb
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def prepare_anthropic_request(messages: list, system, cache: bool = True):
    """Build (system_param, messages_param) for the API call.

    Prompt caching: one breakpoint after the STATIC system block (caches
    tools + identity + skills) and one on the last message block (caches the
    whole growing conversation prefix — the big saver inside tool loops,
    where the same history is re-sent every round). Cached reads bill at
    ~10% of input price; entries live 5 min, refreshed on every hit.

    Never mutates the live history: returns transformed copies, and strips
    stale cache_control keys so breakpoints don't accumulate past the API's
    limit of 4.
    """
    texts = _system_texts(system)
    if cache:
        system_param = [{"type": "text", "text": t} for t in texts]
        system_param[0]["cache_control"] = {"type": "ephemeral"}
    else:
        system_param = "\n\n".join(texts)

    messages = _merge_consecutive(_sanitize_tool_pairs(messages))
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            cleaned = []
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    block = {k: v for k, v in block.items() if k != "cache_control"}
                cleaned.append(block)
            out.append({"role": m["role"], "content": cleaned})
        else:
            out.append({"role": m["role"], "content": content})

    if cache and out:
        last = out[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"],
                                "cache_control": {"type": "ephemeral"}}]
        elif last["content"] and isinstance(last["content"][-1], dict):
            last["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return system_param, out


# ====================================================================== #
# Backend 1: Claude API
# ====================================================================== #
class EngineNotConfigured(RuntimeError):
    """No usable engine — a condition, not a reason to kill the process.

    These used to be `sys.exit(1)`, which is fine in a CLI and catastrophic
    in a web server: it raised SystemExit inside a request, /api/meta
    returned 500, and the front end never finished loading. On a fresh
    install — which by definition has no API key — that left the app dead
    and the Settings page unreachable, so the key could never be entered.
    A first run must always produce a working window.
    """

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.message = message
        self.fix = fix


class AnthropicBrain(_PerThreadEngine):
    backend = "anthropic"

    def __init__(self, model: str | None = None):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise EngineNotConfigured(
                "The 'anthropic' package isn't installed.",
                "Run: pip install -r requirements.txt")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EngineNotConfigured(
                "No Anthropic API key is set.",
                "Add one in Settings, or pick a local engine in the engine "
                "menu and nothing needs a key at all.")
        self.client = Anthropic(api_key=api_key)
        self.model = model or config.MODEL
        self.fast_model = config.FAST_MODEL

    def describe(self) -> str:
        return f"anthropic:{self.model}"

    @staticmethod
    def _usage_of(msg):
        """Token usage of one API response, or None if unavailable."""
        u = getattr(msg, "usage", None)
        if u is None:
            return None
        g = lambda n: int(getattr(u, n, 0) or 0)  # noqa: E731
        return {"in": g("input_tokens"), "out": g("output_tokens"),
                "cache_read": g("cache_read_input_tokens"),
                "cache_write": g("cache_creation_input_tokens")}

    def _create_unstreamed(self, **kwargs):
        """A caller that wants a whole message at once (no delta callback).

        The SDK refuses a NON-streaming request whose max_tokens implies it
        could run past 10 minutes — a client-side guard that fires before any
        network call, so it costs nothing and no tokens are spent. When that
        happens we transparently run the SAME request over the streaming
        endpoint and hand back the assembled final message, which is the
        identical Message object (tool_use blocks included). This kept
        biting long-running callers: the trend digest and run_subagent both
        died on it with a raw ValueError.
        """
        try:
            return self.client.messages.create(**kwargs)
        except ValueError as exc:
            if "streaming" not in str(exc).lower():
                raise
            with self.client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()

    def chat(self, messages: list, system, tools: list | None = None,
             on_text=None, model: str | None = None):
        """on_text: optional callback receiving text deltas as they stream.
        model: per-call override (used by routing)."""
        _ds = _external_response(model, messages, system, tools, on_text,
                                 caller=self)
        if _ds is not None:
            resp, self.last_engine, self.last_usage = _ds
            return resp
        self.last_engine = "claude"
        self.last_usage = None
        system_param, msgs = prepare_anthropic_request(
            messages, system, cache=config.PROMPT_CACHE)
        _m = model or self.model
        if not looks_anthropic(_m):
            # calling Anthropic with someone else's model id gets a 404 that
            # names the string and explains nothing
            raise ValueError(
                f"'{_m}' isn't an Anthropic model, so Claude can't run it. "
                f"Set the model to a claude-… id in Settings (or Engines, if "
                f"you've defined your own Claude). Anthropic model ids look "
                f"like 'claude-sonnet-4-6'.")
        kwargs = dict(model=_m, max_tokens=config.MAX_TOKENS,
                      system=system_param, messages=msgs)
        if tools:
            kwargs["tools"] = tools
        if on_text is None:
            resp = self._create_unstreamed(**kwargs)
            try:
                self.last_usage = self._usage_of(resp)
            except Exception:
                pass
            return resp
        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                on_text(text)
            msg = stream.get_final_message()
            try:
                self.last_usage = self._usage_of(msg)
            except Exception:
                pass
            return msg

    def route(self, user_text: str) -> str:
        """Triage one turn: fast model for SIMPLE, main model otherwise.
        Any failure escalates to the main model."""
        if self.fast_model == self.model:
            return self.model
        try:
            resp = self.client.messages.create(
                model=self.fast_model, max_tokens=4, system=ROUTER_SYSTEM,
                messages=[{"role": "user", "content": user_text[:1500]}])
            word = "".join(b.text for b in resp.content
                           if b.type == "text").strip().upper()
        except Exception:
            return self.model
        return self.fast_model if word.startswith("SIMPLE") else self.model

    def summarize(self, transcript: str) -> str:
        """Compress a transcript for compaction. '' on failure (caller falls back)."""
        try:
            resp = self.client.messages.create(
                model=self.fast_model, max_tokens=400, system=SUMMARY_SYSTEM,
                messages=[{"role": "user", "content": transcript[:14000]}])
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception:
            return ""

    def extract_facts(self, user_text: str, assistant_text: str,
                      known_memories: list[str]) -> list[str]:
        from anthropic import APIError, APIConnectionError
        try:
            response = self.client.messages.create(
                model=config.FAST_MODEL, max_tokens=300,
                system=EXTRACTION_SYSTEM,
                messages=[{"role": "user", "content": _extraction_prompt(
                    user_text, assistant_text, known_memories)}],
            )
        except (APIError, APIConnectionError):
            return []  # learning is best-effort; never break the conversation
        text = "".join(b.text for b in response.content if b.type == "text")
        return _parse_facts(text)


# ====================================================================== #
# Backend 2: local open-weights model via Ollama
# ====================================================================== #
class OllamaUnavailable(Exception):
    """Raised when the local Ollama server can't be reached."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class OllamaBrain(_PerThreadEngine):
    """Talks to a local Ollama server. Translates between the agent's
    Anthropic-style message/tool format and Ollama's /api/chat format,
    so the rest of the codebase is backend-agnostic."""

    backend = "ollama"

    def __init__(self, model: str | None = None):
        self.host = config.OLLAMA_HOST.rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.fast_model = config.OLLAMA_FAST_MODEL or self.model
        self._check_server()

    def describe(self) -> str:
        return f"ollama:{self.model}"

    def _check_server(self) -> None:
        try:
            urllib.request.urlopen(f"{self.host}/api/tags", timeout=3)
        except (urllib.error.URLError, OSError):
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {self.host}.\n"
                "Install it from https://ollama.com, then:\n"
                f"  ollama pull {self.model}\n"
                "  ollama serve   (usually starts automatically)\n"
                "Or set AGENT_OLLAMA_HOST if it runs elsewhere.")

    def classify(self, user_text: str, model: str | None = None) -> str:
        """Return 'SIMPLE' or 'COMPLEX' using the local model. Raises on
        connection failure so callers can decide how to fall back."""
        data = self._post({
            "model": model or self.fast_model,
            "messages": [{"role": "system", "content": ROUTER_SYSTEM},
                         {"role": "user", "content": user_text[:1500]}],
            "stream": False, "options": {"num_predict": 4},
        }, timeout=60)
        word = (data.get("message", {}).get("content", "") or "").strip().upper()
        return "SIMPLE" if word.startswith("SIMPLE") else "COMPLEX"

    # ---- format translation ------------------------------------------ #
    @staticmethod
    def _to_ollama_tools(tools: list | None) -> list | None:
        if not tools:
            return None
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            },
        } for t in tools]

    @staticmethod
    def _to_ollama_messages(messages: list, system) -> list:
        out = [{"role": "system", "content": "\n\n".join(_system_texts(system))}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):                       # plain user text
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "assistant":                       # text + tool calls
                text_parts, tool_calls = [], []
                for b in content:
                    btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                    if btype == "text":
                        text_parts.append(b["text"] if isinstance(b, dict) else b.text)
                    elif btype == "tool_use":
                        name = b["name"] if isinstance(b, dict) else b.name
                        args = b["input"] if isinstance(b, dict) else b.input
                        tool_calls.append({"function": {"name": name,
                                                        "arguments": args or {}}})
                msg = {"role": "assistant", "content": "\n".join(text_parts)}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
            else:                                              # tool results and/or text+images
                tool_results = [b for b in content
                                if (b.get("type") if isinstance(b, dict)
                                    else getattr(b, "type", "")) == "tool_result"]
                for b in tool_results:
                    result = b["content"] if isinstance(b, dict) else b.content
                    out.append({"role": "tool", "content": str(result)})
                texts, imgs = [], []
                for b in content:
                    bt = b.get("type") if isinstance(b, dict) else getattr(b, "type", "")
                    if bt == "text":
                        texts.append(b["text"] if isinstance(b, dict) else getattr(b, "text", ""))
                    elif bt == "image":
                        src = b.get("source", {}) if isinstance(b, dict) else {}
                        if src.get("type") == "base64" and src.get("data"):
                            imgs.append(src["data"])         # Ollama wants raw base64
                if texts or imgs:
                    msg = {"role": "user", "content": "\n".join(texts)}
                    if imgs:
                        msg["images"] = imgs
                    out.append(msg)
        return out

    @staticmethod
    def _response_from(content: str, tool_calls: list | None):
        blocks = []
        if content:
            blocks.append(SimpleNamespace(type="text", text=content))
        for i, call in enumerate(tool_calls or []):
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):       # some models emit JSON strings
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            blocks.append(SimpleNamespace(type="tool_use", name=fn.get("name", ""),
                                          input=args, id=f"call_{i}"))
        if not blocks:
            blocks.append(SimpleNamespace(type="text", text=""))
        has_tools = any(b.type == "tool_use" for b in blocks)
        return SimpleNamespace(content=blocks,
                               stop_reason="tool_use" if has_tools else "end_turn")

    def _post(self, payload: dict, timeout: int = 600) -> dict:
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- interface ----------------------------------------------------- #
    def chat(self, messages: list, system, tools: list | None = None,
             on_text=None, model: str | None = None):
        _ds = _external_response(model, messages, system, tools, on_text,
                                 caller=self)
        if _ds is not None:
            resp, self.last_engine, self.last_usage = _ds
            return resp
        payload = {
            "model": model or self.model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": on_text is not None,
            "options": {"num_predict": config.MAX_TOKENS,
                        "num_ctx": max(config.OLLAMA_NUM_CTX,
                                       config.MAX_TOKENS + 2048)},
        }
        ollama_tools = self._to_ollama_tools(tools)
        if ollama_tools:
            payload["tools"] = ollama_tools

        self.last_engine = "local"
        self.last_usage = None

        if on_text is None:
            data = self._post(payload)
            self.last_usage = self._usage_from(data)
            msg = data.get("message", {}) or {}
            return self._response_from(msg.get("content", ""),
                                       msg.get("tool_calls"))

        # Streaming: NDJSON lines; accumulate text + tool calls as they arrive.
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        content_parts, tool_calls = [], []
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", {}) or {}
                if msg.get("content"):
                    content_parts.append(msg["content"])
                    on_text(msg["content"])
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                if data.get("done"):
                    self.last_usage = self._usage_from(data)
                    break
        return self._response_from("".join(content_parts), tool_calls)

    @staticmethod
    def _usage_from(data: dict):
        """Token counts from an Ollama response, or None if absent."""
        if "prompt_eval_count" not in data and "eval_count" not in data:
            return None
        return {"in": int(data.get("prompt_eval_count") or 0),
                "out": int(data.get("eval_count") or 0),
                "cache_read": 0, "cache_write": 0}

    def route(self, user_text: str) -> str:
        """Triage between OLLAMA_FAST_MODEL and the main local model.
        No-op (returns main) when they're the same."""
        if self.fast_model == self.model:
            return self.model
        try:
            data = self._post({
                "model": self.fast_model,
                "messages": [{"role": "system", "content": ROUTER_SYSTEM},
                             {"role": "user", "content": user_text[:1500]}],
                "stream": False, "options": {"num_predict": 4},
            }, timeout=60)
            word = (data.get("message", {}).get("content", "") or "").strip().upper()
        except Exception:
            return self.model
        return self.fast_model if word.startswith("SIMPLE") else self.model

    def extract_facts(self, user_text: str, assistant_text: str,
                      known_memories: list[str]) -> list[str]:
        try:
            data = self._post({
                "model": self.fast_model,
                "messages": [
                    {"role": "system", "content": EXTRACTION_SYSTEM},
                    {"role": "user", "content": _extraction_prompt(
                        user_text, assistant_text, known_memories)},
                ],
                "stream": False,
                "options": {"num_predict": 300},
            }, timeout=120)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []
        return _parse_facts(data.get("message", {}).get("content", ""))

    def summarize(self, transcript: str) -> str:
        """Compress a transcript for context compaction. '' on failure, so the
        caller (main.py) falls back to a plain trim."""
        try:
            data = self._post({
                "model": self.fast_model,
                "messages": [
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {"role": "user", "content": transcript[:14000]},
                ],
                "stream": False,
                "options": {"num_predict": 400},
            }, timeout=120)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return ""
        return (data.get("message", {}).get("content", "") or "").strip()
# ====================================================================== #
class OpenAIBrain(_PerThreadEngine):
    """OpenAI-compatible chat backend.

    Talks to any endpoint speaking the OpenAI Chat Completions API (DeepSeek V4,
    NVIDIA, OpenRouter, vLLM, ...). Converts the internal block format to/from
    OpenAI messages and returns the same response shape (SimpleNamespace content
    blocks + stop_reason) as the other brains, so run_turn handles it unchanged.
    """

    backend = "openai"

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, label: str = "deepseek",
                 client=None, supports_tools: bool = True,
                 supports_stream: bool = True, timeout: float | None = None):
        self.model = model or config.DEEPSEEK_MODEL_PRO
        self._label = label
        # An engine with no base URL used to silently become a DeepSeek
        # call. So a misconfigured engine produced "could not reach the
        # DeepSeek endpoint" while the badge said Nemotron — an error about
        # a service the user had never chosen, on an engine they had.
        # Defaulting a destination is never a kindness.
        self.base_url = (base_url or "").strip()
        if not self.base_url:
            raise EngineNotConfigured(
                f"Engine '{label}' has no base URL.",
                "Open Engines and set one — https://api.deepseek.com/v1 for "
                "DeepSeek, http://localhost:11434/v1 for Ollama.")
        self.supports_tools = supports_tools
        self.supports_stream = supports_stream
        self.last_engine = None
        self.last_usage = None
        if client is not None:
            self._client = client            # injected (tests)
        else:
            from openai import OpenAI         # lazy: only when actually used
            # 600s suits a long generation; it is catastrophic for a
            # connection test, where a host that drops packets would hang the
            # button for half an hour across two retries. The probe passes
            # its own.
            self._client = OpenAI(
                api_key=api_key or config.DEEPSEEK_API_KEY or "missing",
                base_url=self.base_url,
                timeout=(timeout if timeout is not None else 600.0),
                max_retries=(0 if timeout is not None else 2))

    def describe(self) -> str:
        return f"{self._label}: {self.model}"

    @staticmethod
    def _to_openai_tools(tools: list | None) -> list | None:
        if not tools:
            return None
        return [{"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"}),
        }} for t in tools]

    @staticmethod
    def _to_openai_messages(messages: list, system) -> list:
        def _b(b, k, default=None):
            return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)
        # Repair the tool-call/result pairing the same way the Anthropic path does.
        # A stored history can carry an orphaned tool_call (a turn that was
        # cancelled or errored before its result was recorded). Anthropic tolerates
        # this because it sanitises first; DeepSeek/OpenAI reject it hard with
        # "tool_calls must be followed by tool messages", so sanitise here too.
        messages = _sanitize_tool_pairs(messages)
        out = [{"role": "system", "content": "\n\n".join(_system_texts(system))}]
        for m in messages:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                texts, calls = [], []
                for b in content:
                    bt = _b(b, "type")
                    if bt == "text":
                        texts.append(_b(b, "text", ""))
                    elif bt == "tool_use":
                        calls.append({
                            "id": _b(b, "id") or f"call_{len(calls)}",
                            "type": "function",
                            "function": {"name": _b(b, "name", ""),
                                         "arguments": json.dumps(_b(b, "input", {}) or {})},
                        })
                msg = {"role": "assistant", "content": ("\n".join(texts) or None)}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:                                   # user: tool results +/or text
                for b in content:
                    if _b(b, "type") == "tool_result":
                        out.append({"role": "tool",
                                    "tool_call_id": _b(b, "tool_use_id") or "call_0",
                                    "content": str(_b(b, "content", ""))})
                # Images were dropped here: only `text` blocks were collected,
                # so an attached photo silently never left the browser on any
                # engine but Claude. OpenAI-compatible APIs want a different
                # shape entirely — a data URL, not Anthropic's base64 source.
                texts = [_b(b, "text", "") for b in content
                         if _b(b, "type") == "text"]
                parts = []
                for b in content:
                    if _b(b, "type") != "image":
                        continue
                    src = _b(b, "source", {}) or {}
                    data = (src.get("data") if isinstance(src, dict)
                            else None) or ""
                    media = (src.get("media_type") if isinstance(src, dict)
                             else None) or "image/png"
                    if data:
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{data}"}})
                joined = "\n".join(t2 for t2 in texts if (t2 or "").strip())
                if parts:
                    # a vision request must carry text too, or some providers
                    # reject the message outright
                    parts.insert(0, {"type": "text",
                                     "text": joined or "What is in this image?"})
                    out.append({"role": "user", "content": parts})
                elif joined:
                    out.append({"role": "user", "content": joined})
        return _enforce_openai_tool_invariant(out)

    @staticmethod
    def _finish_to_stop(finish) -> str:
        return {"tool_calls": "tool_use", "length": "max_tokens",
                "stop": "end_turn"}.get(finish, "end_turn")

    @staticmethod
    def _text_of(content) -> str:
        """OpenAI-compatible content may be a string, None, or a list of content
        parts (some endpoints return the latter). Always return a plain string."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for p in content:
                if isinstance(p, str):
                    out.append(p)
                elif isinstance(p, dict):
                    out.append(p.get("text") or p.get("content") or "")
                else:
                    out.append(getattr(p, "text", "") or "")
            return "".join(out)
        return str(content)

    @staticmethod
    def _blocks(text: str, calls: list | None) -> list:
        blocks = []
        if text:
            blocks.append(SimpleNamespace(type="text", text=text))
        for i, c in enumerate(calls or []):
            args = c.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {"raw": args}
            blocks.append(SimpleNamespace(type="tool_use", name=c.get("name", ""),
                                          input=args or {},
                                          id=c.get("id") or f"call_{i}"))
        if not blocks:
            blocks.append(SimpleNamespace(type="text", text=""))
        return blocks

    def chat(self, messages: list, system, tools: list | None = None,
             on_text=None, model: str | None = None):
        self.last_engine = self._label
        self.last_usage = None
        try:
            return self._do_chat(messages, system, tools, on_text, model)
        except Exception as exc:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text",
                         text=self._friendly_error(exc, model or self.model))],
                stop_reason="end_turn")

    def _do_chat(self, messages, system, tools, on_text, model):
        _m = model or self.model
        if looks_local_tag(_m) and not is_local_endpoint(self.base_url):
            # the mirror image of the Anthropic guard: that one caught an
            # engine NAME going to a cloud model field, this catches a LOCAL
            # model going to a cloud endpoint
            raise ValueError(
                f"'{_m}' is an Ollama model tag, and {self._label} is a cloud "
                f"engine that has never heard of it. Pick the local engine in "
                f"the engine menu to run it, or give this engine a model id "
                f"from its own provider.")
        kwargs = dict(model=_m,
                      messages=self._to_openai_messages(messages, system),
                      max_tokens=config.MAX_TOKENS)
        # Ollama's OpenAI-compatible endpoint honours num_ctx via extra_body;
        # without it Ollama caps the context at 2048 and truncates long replies
        # regardless of max_tokens. Only sent to local Ollama hosts.
        try:
            if "11434" in str(getattr(self, "base_url", "")) or \
                    "ollama" in str(getattr(self, "base_url", "")).lower():
                kwargs["extra_body"] = {"options": {
                    "num_ctx": max(config.OLLAMA_NUM_CTX,
                                   config.MAX_TOKENS + 2048)}}
        except Exception:
            pass
        oai_tools = self._to_openai_tools(tools) if self.supports_tools else None
        if oai_tools:
            kwargs["tools"] = oai_tools

        if on_text is None or not self.supports_stream:   # non-streaming
            resp = self._client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            msg = choice.message
            calls = [{"id": tc.id, "name": tc.function.name,
                      "arguments": tc.function.arguments}
                     for tc in (msg.tool_calls or [])]
            self.last_usage = self._usage_of(getattr(resp, "usage", None))
            text = self._text_of(msg.content)
            if on_text and text:        # engine can't stream: show the text at once
                on_text(text)
            return SimpleNamespace(
                content=self._blocks(text, calls),
                stop_reason=self._finish_to_stop(choice.finish_reason))

        # streaming: accumulate text + tool-call argument deltas
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self._client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("stream_options", None)        # endpoint without it
            stream = self._client.chat.completions.create(**kwargs)
        texts, calls, finish, usage = [], {}, None, None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            ch = chunk.choices[0]
            delta = ch.delta
            piece = self._text_of(getattr(delta, "content", None))
            if piece:
                texts.append(piece)
                on_text(piece)
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = calls.setdefault(tc.index,
                                        {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if fn.name:
                        slot["name"] = fn.name
                    if fn.arguments:
                        slot["arguments"] += fn.arguments
            if ch.finish_reason:
                finish = ch.finish_reason
        self.last_usage = self._usage_of(usage)
        ordered = [calls[i] for i in sorted(calls)]
        return SimpleNamespace(content=self._blocks("".join(texts), ordered),
                               stop_reason=self._finish_to_stop(finish))

    @staticmethod
    def _usage_of(usage):
        if not usage:
            return None
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        return {"in": int(getattr(usage, "prompt_tokens", 0) or 0),
                "out": int(getattr(usage, "completion_tokens", 0) or 0),
                "cache_read": int(cached), "cache_write": 0}

    def _installed_models(self) -> list:
        """Ask a LOCAL runner what it actually has.

        A 404 from Ollama means "I don't have that model", and the useful
        reply is the list of ones it does have — not advice about provider
        naming prefixes, which is what a cloud 404 needs and is worse than
        useless here."""
        base = (self.base_url or "").rstrip("/")
        for suffix in ("/v1", "/api"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        try:
            import httpx
            with httpx.Client(timeout=3.0) as c:
                r = c.get(base + "/api/tags")
                r.raise_for_status()
                return sorted(m.get("name", "")
                              for m in r.json().get("models", [])
                              if m.get("name"))
        except Exception:
            return []

    @staticmethod
    def _closest(wanted: str, have: list) -> str:
        """The one they probably meant. `gemma4:24b` against a machine
        holding `gemma4:12b` should not be a dead end."""
        import difflib
        if not have:
            return ""
        stem = (wanted or "").split(":")[0].lower()
        same_family = [h for h in have if h.split(":")[0].lower() == stem]
        if same_family:
            return same_family[0]
        near = difflib.get_close_matches(wanted, have, n=1, cutoff=0.5)
        return near[0] if near else ""

    def _friendly_error(self, exc, model: str = "") -> str:
        """Turn a raw provider exception into a short, actionable message.

        It used to say "DeepSeek" whatever the engine was, because this class
        started life as the DeepSeek client. Telling someone their Mistral
        engine has a DeepSeek problem sends them to the wrong provider's
        dashboard."""
        who = getattr(self, "_label", "") or "This engine"
        who = who[:1].upper() + who[1:]
        name = type(exc).__name__
        s = str(exc)
        low = s.lower()
        if "429" in s or "ratelimit" in name.lower() or "too many requests" in low:
            return (f"_{who} is being rate-limited by your provider (HTTP 429 - "
                    "Too Many Requests). On NVIDIA's free tier that means either "
                    "more than ~40 requests a minute or your inference credits are "
                    "used up. Wait a minute and try again, check remaining credits "
                    "at build.nvidia.com, or point this engine at DeepSeek's own API "
                    "(higher limits) by setting AGENT_DEEPSEEK_BASE_URL._")
        if ("401" in s or "403" in s or "authentication" in low
                or ("invalid" in low and "key" in low)):
            return (f"_{who} rejected the API key (authentication failed). Check "
                    "the key and that the base URL matches the provider it is from "
                    "- an NVIDIA key only works on NVIDIA's endpoint, and so on._")
        if "404" in s or ("model" in low and "not" in low and "found" in low):
            # A local runner can be ASKED what it has, which turns a dead end
            # into an answer.
            if is_local_endpoint(self.base_url):
                have = self._installed_models()
                if have:
                    near = self._closest(model, have)
                    listed = ", ".join(f"`{h}`" for h in have[:8])
                    return (f"_`{model}` isn't installed. This machine has: "
                            + listed
                            + (f". Did you mean `{near}`? Set it as the "
                               f"model for this engine."
                               if near else
                               f". Set one of those as the model for this "
                               f"engine, or run `ollama pull {model}` to "
                               f"fetch it.")
                            + "_")
                return (f"_`{model}` isn't installed, and I couldn't reach "
                        f"the local runner at {self.base_url} to see what is. "
                        f"Check Ollama is running._")
            return (f"_{who} could not find the model `{model}`. Make sure the "
                    "model id matches your provider's naming (for example, NVIDIA "
                    "uses the `deepseek-ai/` prefix, DeepSeek's own API does not)._")
        if "timeout" in low or "timed out" in low or "connection" in low:
            return (f"_Could not reach {who} at {self.base_url} (network or "
                    f"timeout). "
                    "Check your connection and the base URL, then try again._")
        return f"_{who} request failed \u2014 {name}: {s[:300]}_"

    # secondary interface (only used if DeepSeek is ever the primary brain)
    def route(self, user_text: str) -> str:
        return self.model

    def classify(self, user_text: str, model: str | None = None) -> str:
        return "COMPLEX"

    def summarize(self, transcript: str) -> str:
        return ""        # fall back to a plain trim, no extra cost

    def extract_facts(self, user_text, assistant_text, known_memories):
        return []


# --- DeepSeek dispatch (shared by every brain so the engine works regardless
#     of the configured backend) ------------------------------------------- #
# One brain per model id, so Pro and Flash can carry different keys/endpoints.
_deepseek_brains: dict = {}


def _get_deepseek(model):
    """Lazily build and cache the DeepSeek brain for a specific model, using
    that model's key/base-url (per-model override or the shared default).
    None if that model has no key, or the 'openai' package is missing."""
    if model in _deepseek_brains:
        return _deepseek_brains[model] or None
    key, base_url = config.deepseek_creds(model)
    if not key:
        _deepseek_brains[model] = False
        return None
    try:
        _deepseek_brains[model] = OpenAIBrain(model=model, base_url=base_url,
                                              api_key=key, label="deepseek")
    except Exception as exc:
        print(f"[deepseek] unavailable for {model}: {type(exc).__name__}: {exc}")
        _deepseek_brains[model] = False
    return _deepseek_brains[model] or None


def _is_deepseek_model(model) -> bool:
    return bool(model) and model in (config.DEEPSEEK_MODEL_PRO,
                                     config.DEEPSEEK_MODEL_FLASH)


# --- user-defined engines (added from the Engines tab, saved to disk) ------ #
# Each entry: {"name","base_url","api_key","model"}. Stored at
# AGENT_HOME/engines.json. Routed by engine NAME (the selector token), so two
# engines may share a model id on different providers.
def _engines_file():
    """Resolved when used, not at import.

    It was a module constant, so it pointed at whatever AGENT_HOME happened
    to be when the module first loaded. Anything that changes the home
    afterwards — a test, a second profile, a moved data folder — kept reading
    and writing the old location while appearing to work.
    """
    return config.AGENT_HOME / "engines.json"
_RESERVED_NAMES = None
_custom_engines_cache = None
_custom_brains: dict = {}


def _reserved_names() -> set:
    """Names that mean something to the router itself.

    "Claude" is no longer among them. It was a built-in you could neither
    edit nor remove, which meant a key you couldn't change, a model id you
    couldn't correct, and a hardcoded fall-through that turned any
    unrecognised engine name into a 404 from Anthropic. Define it like every
    other engine and the special case — and its failure mode — go away.
    """
    return {"auto", "ollama"}


def load_custom_engines(refresh: bool = False) -> list:
    """Return the saved custom engines (cached)."""
    global _custom_engines_cache
    if _custom_engines_cache is not None and not refresh:
        return _custom_engines_cache
    data = []
    _dropped_engines.clear()
    try:
        if _engines_file().exists():
            raw = json.loads(_engines_file().read_text(encoding="utf-8"))
            need_migrate = False
            if isinstance(raw, list):
                for e in raw:
                    # An entry missing a name or model is dropped here. It
                    # used to vanish without trace, so an engine you saved
                    # simply wasn't there and nothing said why. Record it.
                    if isinstance(e, dict) and not (e.get("name")
                                                    and e.get("model")):
                        _dropped_engines.append({
                            "name": e.get("name") or "(unnamed)",
                            "why": ("no model id" if e.get("name")
                                    else "no name")})
                    if isinstance(e, dict) and e.get("name") and e.get("model"):
                        stored = str(e.get("api_key", ""))
                        if stored and not crypto.is_encrypted(stored):
                            need_migrate = True       # legacy plaintext key
                        data.append({"name": str(e["name"]).strip(),
                                     "base_url": str(e.get("base_url", "")).strip(),
                                     "api_key": crypto.decrypt_str(stored),
                                     "model": str(e["model"]).strip(),
                                     "tools": bool(e.get("tools", True)),
                                     "stream": bool(e.get("stream", True)),
                                     "price_in": float(e.get("price_in", 0) or 0),
                                     "price_out": float(e.get("price_out", 0) or 0)})
            _custom_engines_cache = data
            if need_migrate and data:
                try:
                    _save_custom_engines(data)        # rewrite with keys encrypted
                except Exception:
                    pass
            return data
    except Exception as exc:
        print(f"[engines] could not read {_engines_file()}: {exc}")
    _custom_engines_cache = data
    return data


def _save_custom_engines(engines: list) -> None:
    global _custom_engines_cache
    _engines_file().parent.mkdir(parents=True, exist_ok=True)
    on_disk = []
    for e in engines:
        d = dict(e)
        key = d.get("api_key", "")
        d["api_key"] = crypto.encrypt_str(key) if key else ""   # sealed at rest
        on_disk.append(d)
    _engines_file().write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
    try:
        os.chmod(_engines_file(), 0o600)
    except Exception:
        pass
    _custom_engines_cache = engines          # cache keeps usable plaintext keys


def custom_engine_names() -> list:
    return [e["name"] for e in load_custom_engines()]


def list_ollama_models(timeout: float = 4.0) -> list:
    """Model ids installed in the local Ollama instance (for the model picker).
    Returns [] if Ollama isn't running or reachable — the caller falls back to a
    free-text field. Never raises."""
    import json as _json
    import urllib.request
    host = config.OLLAMA_HOST.rstrip("/")
    for path in ("/api/tags", "/v1/models"):
        try:
            with urllib.request.urlopen(host + path, timeout=timeout) as r:
                data = _json.loads(r.read().decode("utf-8", "replace"))
            names = []
            for m in (data.get("models") or data.get("data") or []):
                nm = m.get("name") or m.get("model") or m.get("id")
                if nm and nm not in names:
                    names.append(nm)
            if names:
                return sorted(names)
        except Exception:
            continue
    return []


def engine_price(label) -> tuple:
    """(input, output) price in USD per MILLION tokens for any engine label —
    built-in or custom. Local models are free; custom engines carry their own
    prices (0 until you set them); an unknown label costs 0 so the total is never
    wrong, just incomplete. Claude/DeepSeek prices are read live from config so an
    env override or a settings change takes effect immediately."""
    key = (label or "").strip().lower()
    if key in ("claude", "anthropic", "mixed"):
        # "mixed" only appears if per-engine attribution was unavailable; Claude
        # is the costed default in that case.
        return (config.PRICE_IN_PER_M, config.PRICE_OUT_PER_M)
    if "deepseek" in key:
        return (config.PRICE_DEEPSEEK_IN_PER_M, config.PRICE_DEEPSEEK_OUT_PER_M)
    if key in ("local", "ollama") or key == (config.OLLAMA_MODEL_2 or "").lower():
        return (0.0, 0.0)
    for e in load_custom_engines():
        if e["name"].strip().lower() == key:
            return (float(e.get("price_in", 0) or 0),
                    float(e.get("price_out", 0) or 0))
    return (0.0, 0.0)


_dropped_engines = []


def check_engines() -> dict:
    """Stored engines that can't work, named before you send a message.

    A saved engine that had lost its base URL used to be invisible: the call
    fell through to a default endpoint, so the error named a service the user
    had never chosen while the badge named the engine they had. The only way
    out was to delete every engine and add them again, which is a thing
    nobody should have to deduce.
    """
    problems = []
    for e in load_custom_engines(refresh=True) or []:
        name = e.get("name") or "(unnamed)"
        base = (e.get("base_url") or "").strip()
        model = (e.get("model") or "").strip()
        if not base:
            problems.append({
                "engine": name, "what": "has no base URL",
                "fix": ("Open Engines, edit it, and set one — "
                        "http://localhost:11434/v1 for Ollama.")})
        elif not base.startswith(("http://", "https://")):
            problems.append({
                "engine": name, "what": f"has '{base}' as its base URL",
                "fix": "It needs to start with http:// or https://."})
        if not model:
            problems.append({
                "engine": name, "what": "has no model id",
                "fix": "Set the id the provider expects, e.g. qwen3:8b."})
        if base and not is_local_endpoint(base) and not e.get("api_key"):
            problems.append({
                "engine": name,
                "what": "is a cloud engine with no API key",
                "fix": "Add its key, or point it at a local runner."})
    for d in _dropped_engines:
        problems.append({
            "engine": d["name"],
            "what": f"was dropped on load ({d['why']})",
            "fix": ("Re-save it in Engines with every field filled — it "
                    "isn't in the list at all until then.")})
    return {"ok": not problems, "problems": problems,
            "detail": "; ".join(f"'{p['engine']}' {p['what']}"
                                for p in problems[:3]),
            "count": len(problems)}


def repair_engine_models() -> dict:
    """An engine whose MODEL field holds another ENGINE's name.

    Reported: an engine called Nemotron with the model 'DeepSeekReplika'.
    Ollama was then asked to pull a model by that name, and the error read as
    a missing model rather than a mixed-up field. `repair_model()` already
    does this for the global setting; custom engines had no equivalent, so a
    single mis-typed field broke every call through that engine with a
    message pointing at the wrong thing.

    Only fixes the unambiguous case — the model field naming a DIFFERENT
    engine — because an engine legitimately named after its own model is the
    app's own convention for local ones.
    """
    entries = load_custom_engines(refresh=True) or []
    names = {e.get("name", "").lower() for e in entries}
    fixed = []
    for e in entries:
        model = (e.get("model") or "").strip()
        if not model:
            continue
        if (model.lower() in names
                and model.lower() != (e.get("name") or "").lower()):
            fixed.append({"engine": e.get("name"), "was": model})
    return {"ok": True, "confused": fixed,
            "detail": ("" if not fixed else
                       "; ".join(f"'{f['engine']}' has another engine's name "
                                 f"('{f['was']}') in its model field"
                                 for f in fixed)),
            "fix": ("" if not fixed else
                    "Open Engines, edit it, and put the model id the "
                    "provider expects in the Model field — `ollama list` for "
                    "a local one.")}


def custom_engine_by_name(name):
    if not name:
        return None
    for e in load_custom_engines():
        if e["name"] == name:
            return e
    return None


def add_custom_engine(name, base_url, api_key, model, tools=True, stream=True,
                      price_in=0.0, price_out=0.0):
    """Validate and persist a new engine. Returns (ok, message)."""
    name = (name or "").strip()
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if not name or not base_url or not model:
        return False, "Name, base URL and model id are all required."
    # An engine NAME that looks like a model ID is ambiguous: the dispatcher
    # resolves by name first, then falls through to treating the string as a
    # model id — so naming an engine `gemma4:24b` means a failure to resolve
    # silently becomes "model not found", and the two readings are
    # indistinguishable in the error. Found the hard way.
    if (":" in name and "/" not in name
            and name.strip().lower() != (model or "").strip().lower()):
        # Only when the name and the model DISAGREE. "Make local models
        # selectable" names an engine after its tag on purpose, and there it
        # reads fine because the engine genuinely is that model. The trap is
        # a name that looks like a model id pointing at a DIFFERENT one:
        # "not found" then means either, and the error can't say which.
        return False, (
            f"'{name}' looks like a model id, but this engine's model is "
            f"'{model}'. Give the engine a name of its own — 'Gemma local', "
            f"say — so a 'not found' error can tell you which of the two it "
            f"means.")
    if name.lower() in _reserved_names():
        return False, f"'{name}' is a built-in engine name. Pick another."
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return False, "Base URL must start with http:// or https://"

    def _price(v):
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return 0.0
    engines = [e for e in load_custom_engines() if e["name"] != name]
    engines.append({"name": name, "base_url": base_url,
                    "api_key": api_key, "model": model,
                    "tools": bool(tools), "stream": bool(stream),
                    "price_in": _price(price_in), "price_out": _price(price_out)})
    _save_custom_engines(engines)
    _custom_brains.pop(name, None)            # rebuild on next use
    return True, f"Engine '{name}' saved."


def register_ollama_engine(name: str, model: str):
    """Materialise a local Ollama model as a first-class, removable custom
    engine that talks to Ollama's OpenAI-compatible endpoint. This makes the
    engine work under ANY backend (anthropic/hybrid/ollama) and behave exactly
    like a user-added engine — selectable, priced at zero (local = free), and
    removable — instead of the old preloaded entries that only resolved under
    the hybrid backend and silently fell back to Claude otherwise."""
    base = config.OLLAMA_HOST.rstrip("/") + "/v1"
    return add_custom_engine(name=name, base_url=base, api_key="ollama",
                             model=model, tools=True, stream=True,
                             price_in=0.0, price_out=0.0)


def get_custom_engine(name: str) -> dict | None:
    """One engine, with the key masked. Editing needs to show what's there
    without putting a secret back on screen."""
    for e in load_custom_engines():
        if e["name"] == name:
            key = str(e.get("api_key") or "")
            return {**e, "api_key": "",
                    "has_key": bool(key),
                    "key_hint": (key[:3] + "…" + key[-3:]) if len(key) > 8
                                else ("set" if key else "")}
    return None


def update_custom_engine(name, base_url=None, api_key=None, model=None,
                         tools=None, stream=None, price_in=None,
                         price_out=None, new_name=None):
    """Change an engine that already exists.

    Until now the only way to correct a typo in a base URL was to delete the
    engine and add it again, which lost the key and any pinning that pointed
    at it. Fields left as None are kept; an api_key left empty keeps the
    stored one, so editing a price doesn't silently wipe the credential.
    """
    name = (name or "").strip()
    items = load_custom_engines()
    cur = next((e for e in items if e["name"] == name), None)
    if cur is None:
        return False, f"There's no engine called '{name}'."

    target = (new_name or name).strip()
    if target != name:
        if target.lower() in _reserved_names():
            return False, f"'{target}' is a built-in engine name."
        if any(e["name"] == target for e in items):
            return False, f"There's already an engine called '{target}'."

    url = (base_url if base_url is not None else cur["base_url"]).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "Base URL must start with http:// or https://"
    mdl = (model if model is not None else cur["model"]).strip()
    if not mdl:
        return False, "A model id is required."

    def _price(v, fallback):
        if v is None or v == "":
            return float(fallback or 0)
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return float(fallback or 0)

    updated = {
        "name": target, "base_url": url, "model": mdl,
        # an empty key means "leave it alone", not "delete it" — otherwise
        # changing a price would quietly unauthenticate the engine
        "api_key": (api_key.strip() if (api_key or "").strip()
                    else cur.get("api_key", "")),
        "tools": cur.get("tools", True) if tools is None else bool(tools),
        "stream": cur.get("stream", True) if stream is None else bool(stream),
        "price_in": _price(price_in, cur.get("price_in")),
        "price_out": _price(price_out, cur.get("price_out")),
    }
    _save_custom_engines([e for e in items if e["name"] != name] + [updated])
    _custom_brains.pop(name, None)
    _custom_brains.pop(target, None)
    renamed = target != name
    return True, (f"Renamed to '{target}' and saved." if renamed
                  else f"'{target}' updated.")


def remove_custom_engine(name):
    name = (name or "").strip()
    engines = load_custom_engines()
    kept = [e for e in engines if e["name"] != name]
    if len(kept) == len(engines):
        return False, f"No engine named '{name}'."
    _save_custom_engines(kept)
    _custom_brains.pop(name, None)
    return True, f"Engine '{name}' removed."


def _get_custom_brain(entry):
    """Build/cache an OpenAIBrain for a custom engine; rebuild if it changed."""
    name = entry["name"]
    tools = bool(entry.get("tools", True))
    stream = bool(entry.get("stream", True))
    sig = (entry.get("base_url"), entry.get("api_key"), entry.get("model"),
           tools, stream)
    cached = _custom_brains.get(name)
    if cached is not None and getattr(cached, "_sig", None) == sig:
        return cached or None
    if not entry.get("base_url") or not entry.get("model"):
        _custom_brains[name] = False
        return None
    try:
        b = OpenAIBrain(model=entry["model"], base_url=entry["base_url"],
                        api_key=entry.get("api_key") or "none",
                        label=entry.get("name") or "custom",
                        supports_tools=tools, supports_stream=stream)
        b._sig = sig
        _custom_brains[name] = b
    except Exception as exc:
        print(f"[engine:{name}] unavailable: {type(exc).__name__}: {exc}")
        _custom_brains[name] = False
    return _custom_brains[name] or None


ANTHROPIC_PREFIXES = ("claude-", "claude.", "anthropic.")


def looks_local_tag(model: str) -> bool:
    """Is this an Ollama-style model tag rather than a cloud model id?

    `gemma4:24b` is a tag a local runner understands. A cloud API has never
    heard of it, and sending one produces a 404 that reads as "your model id
    is wrong" when the real fault is that a LOCAL model was routed to a cloud
    engine. The colon is the giveaway: cloud providers use slashes and
    hyphens, Ollama uses `name:tag`."""
    m = (model or "").strip()
    return ":" in m and "/" not in m and not m.startswith("http")


def is_local_endpoint(base_url: str) -> bool:
    u = (base_url or "").lower()
    return ("localhost" in u or "127.0.0.1" in u or "0.0.0.0" in u
            or "::1" in u or ".local" in u
            or any(u.startswith(f"http://192.168.") for _ in (0,))
            or "192.168." in u or "10.0." in u or "172.16." in u)


def looks_anthropic(model: str) -> bool:
    """Is this a model the Anthropic API would recognise?

    Every guard so far checked the engine SELECTOR. None checked the model
    behind it — so a Claude engine whose model field said "DeepSeekReplika"
    sailed through and produced a 404 naming that string, which read like an
    engine-routing fault and wasn't.
    """
    m = (model or "").strip().lower()
    return bool(m) and m.startswith(ANTHROPIC_PREFIXES)


def _is_model_id(token: str) -> bool:
    """Does this look like a MODEL a provider would recognise, rather than an
    engine name someone typed?

    Model ids are lowercase, hyphenated and versioned; engine names have
    capitals and spaces. Getting this wrong in the safe direction just means
    a clearer error, so it errs towards "this is a name"."""
    t = (token or "").strip()
    if not t:
        return False
    if t.startswith(("claude-", "gpt-", "o1", "o3", "deepseek-", "mistral-",
                     "gemini-", "llama-", "qwen")):
        return True
    if ":" in t:                      # an ollama tag, e.g. qwen3.6:latest
        return True
    # a name with spaces or capitals is a label, not a model id
    return not (" " in t or any(c.isupper() for c in t))


def _external_response(model, messages, system, tools, on_text,
                       caller=None):
    """Handle DeepSeek (built-in) or any user-defined engine. `model` is the
    selector token: a DeepSeek model id, or a custom engine's NAME. Returns
    (response, engine, usage), or None if it's neither (caller proceeds)."""
    if _is_deepseek_model(model):
        ds = _get_deepseek(model)
        if ds is None:
            which = ("AGENT_DEEPSEEK_KEY_FLASH" if model == config.DEEPSEEK_MODEL_FLASH
                     else "AGENT_DEEPSEEK_KEY_PRO")
            note = (f"DeepSeek is selected but this engine has no key. Set "
                    f"AGENT_DEEPSEEK_KEY (used for both) or {which} (just this one), "
                    "match the base URL / model id to your provider, install the "
                    "'openai' package, then restart.")
            return (SimpleNamespace(content=[SimpleNamespace(type="text", text=note)],
                                    stop_reason="end_turn"), "deepseek", None)
        return (ds.chat(messages, system, tools, on_text=on_text, model=model),
                ds.last_engine, ds.last_usage)

    # An unrecognised selector must NOT fall through to Anthropic. That is
    # what produced "404 - model: DeepSeekReplika": an engine name that no
    # longer resolves was handed to the Claude client as a model id, and the
    # error named the engine, which makes it look like Anthropic's fault.
    # Your own definition wins. If you've added an engine called "Claude"
    # with your key and model, that is the one to use — the built-in exists
    # only so a fresh install works before you've configured anything.
    # "Claude" has to route like any other engine now. It used to mean
    # "whatever this brain defaults to", which was Anthropic while the
    # backend was hardcoded — once the backend became whichever engine the
    # user configured, choosing Claude quietly called DeepSeek, and sending a
    # Claude model id to DeepSeek produced "invalid model name".
    # Only the engine NAME. A bare model id ("claude-sonnet-4-6") means
    # "the calling brain handles this" and always has — intercepting that
    # took the decision away from a caller that was already equipped to
    # make it, including one holding an injected client.
    if model == "Claude":
        # Only take over when the caller genuinely can't reach Anthropic.
        # An Anthropic brain obviously can; so can a hybrid one, through its
        # cloud side. Intercepting either would bypass their client — which
        # in the hybrid case meant a test's injected fake was ignored and a
        # real API call went out.
        if (getattr(caller, "backend", "") == "anthropic"
                or getattr(caller, "cloud", None) is not None):
            return None
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("AGENT_API_KEY")):
            note = ("Claude needs an API key, and none is set. Add one in "
                    "Settings, or pick one of your own engines — a local "
                    "one needs no key.")
            return (SimpleNamespace(content=[SimpleNamespace(type="text",
                                                             text=note)],
                                    stop_reason="end_turn"), "claude", None)
        try:
            ab = AnthropicBrain(config.MODEL)
        except EngineNotConfigured as exc:
            return (SimpleNamespace(content=[SimpleNamespace(
                type="text", text=f"{exc.message} {exc.fix}")],
                stop_reason="end_turn"), "claude", None)
        return (ab.chat(messages, system, tools, on_text=on_text),
                ab.last_engine, ab.last_usage)

    entry = custom_engine_by_name(model)        # custom engines route by name
    if entry is None and model and not _is_model_id(model):
        names = ", ".join(custom_engine_names()) or "none configured"
        note = (f"'{model}' isn't an engine I can reach any more. It may have "
                f"been renamed or removed. Engines available: {names}. "
                f"Pick one in Settings, or set the default to Auto.")
        return (SimpleNamespace(content=[SimpleNamespace(type="text",
                                                         text=note)],
                                stop_reason="end_turn"), "unknown", None)
    if entry is not None:
        cb = _get_custom_brain(entry)
        if cb is None:
            note = (f"Engine '{entry['name']}' isn't usable. Check its base URL "
                    "and model, install the 'openai' package, then try again.")
            return (SimpleNamespace(content=[SimpleNamespace(type="text", text=note)],
                                    stop_reason="end_turn"), "custom", None)
        return (cb.chat(messages, system, tools, on_text=on_text,
                        model=entry["model"]), cb.last_engine, cb.last_usage)
    return None


class HybridBrain(_PerThreadEngine):
    """Cost-saving router across two backends.

    To keep Claude spend down, EVERYTHING cheap runs on the local Ollama
    model (free): the SIMPLE/COMPLEX routing decision, the simple turns
    themselves, background memory extraction, and context-compaction
    summaries. The Claude API is used ONLY for turns the router judges
    complex (and for anything long or mid-task, per main.py's guards).

    Exposes the same interface as the other brains. `self.model` is the
    cloud identity and `self.fast_model` the local identity; main.py's
    router returns the local id for simple turns, and chat() dispatches to
    the matching backend. If Ollama is unreachable at startup, it degrades
    to Claude-only so the app still runs.
    """

    backend = "hybrid"

    def __init__(self, cloud_model: str | None = None,
                 local_model: str | None = None):
        self.cloud = AnthropicBrain(model=cloud_model)   # Claude (paid)
        try:
            self.local = OllamaBrain(model=local_model)  # Ollama (free)
        except OllamaUnavailable as exc:
            self.local = None
            print(f"[hybrid] {exc.message.splitlines()[0]}\n"
                  "[hybrid] Local model unavailable \u2014 running on Claude only. "
                  "Start Ollama to enable local routing and save tokens.")
        self.model = self.cloud.model
        # fast_model must differ from model for routing to engage; if local is
        # down, make them equal so main.py disables routing (all-cloud).
        self.fast_model = self.local.model if self.local else self.model
        if self.local and self.fast_model == self.model:
            self.fast_model = self.local.model + "#local"

    def describe(self) -> str:
        if not self.local:
            return f"hybrid: {self.cloud.describe()} (local offline)"
        return f"hybrid: {self.local.describe()} + {self.cloud.describe()}"

    def chat(self, messages, system, tools=None, on_text=None, model=None):
        """Routing by the requested model id:
          - None  -> Claude (cloud).
          - the cloud id -> Claude.
          - the local 'fast' marker -> the local default Ollama model.
          - any other non-None id -> the local Ollama, run with THAT id
            (this is how a second local model, e.g. qwen3.6, is selected).
        Falls back to Claude if the local backend is unavailable."""
        _ds = _external_response(model, messages, system, tools, on_text,
                                 caller=self)
        if _ds is not None:
            resp, self.last_engine, self.last_usage = _ds
            return resp
        use_local = (self.local is not None and model is not None
                     and model != self.cloud.model)
        if use_local:
            # The fast marker maps back to the local default; any other string
            # is treated as an explicit Ollama model to run.
            local_model = (self.local.model if model == self.fast_model
                           else model)
            target = self.local
            resp = self.local.chat(messages, system, tools,
                                   on_text=on_text, model=local_model)
        else:
            target = self.cloud
            resp = self.cloud.chat(messages, system, tools,
                                   on_text=on_text, model=model)
        self.last_engine = getattr(target, "last_engine", None)
        self.last_usage = getattr(target, "last_usage", None)
        return resp

    def route(self, user_text: str) -> str:
        """Classify locally (free). Returns the local id for SIMPLE turns,
        the cloud id otherwise. Any local failure escalates to Claude."""
        if not self.local:
            return self.model
        try:
            label = self.local.classify(user_text, model=self.local.model)
        except Exception:
            return self.model      # Ollama hiccup -> use Claude (safe)
        return self.fast_model if label == "SIMPLE" else self.model

    def summarize(self, transcript: str) -> str:
        """Compaction summary on the local model (free); '' falls back to a
        plain trim in main.py, so this never costs Claude tokens."""
        if not self.local:
            return ""
        try:
            return self.local.summarize(transcript)
        except Exception:
            return ""

    def extract_facts(self, user_text, assistant_text, known_memories):
        """Background learning. Prefer the local model (free); if no local model
        is available, fall back to the cloud model so learning still happens —
        it's a single short call. Set AGENT_AUTO_LEARN=0 to switch learning off
        entirely if you'd rather not spend anything on it."""
        engine = self.local or self.cloud
        if engine is None:
            return []
        try:
            return engine.extract_facts(user_text, assistant_text,
                                        known_memories)
        except Exception:
            return []


def _first_usable_custom(build: bool = True):
    """An engine the user configured that can actually run.

    Prefers local — no key to be wrong — then any cloud engine carrying its
    own key. Returns None when there's nothing, so the original error stands
    rather than being replaced by a vaguer one.
    """
    try:
        entries = load_custom_engines(refresh=True) or []
    except Exception:
        return None
    ranked = sorted(
        (e for e in entries
         if e.get("base_url") and (e.get("api_key")
                                   or is_local_endpoint(e.get("base_url")))),
        key=lambda e: 0 if is_local_endpoint(e.get("base_url", "")) else 1)
    if not build:
        return ranked[0] if ranked else None
    for e in ranked:
        try:
            return OpenAIBrain(model=e.get("model"),
                               base_url=e.get("base_url"),
                               api_key=e.get("api_key") or "none",
                               label=e.get("name") or "custom")
        except Exception:
            continue
    return None


def probe_engine(base_url: str, api_key: str, model: str,
                 timeout: float = 12.0) -> dict:
    """Actually talk to an engine and report what happened.

    The Test button used to check that two fields weren't empty and say
    "Looks valid" — a test that cannot fail is not a test, and it sent people
    away confident about an endpoint nobody had contacted.

    This sends one real (tiny) message and classifies the result, because
    "it didn't work" is not a diagnosis: a wrong key, a wrong model id, a
    server that isn't running and a firewall all need different actions.
    """
    import time as _t
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "stage": "setup",
                "error": "No base URL.",
                "fix": "Something like https://api.deepseek.com/v1, or "
                       "http://localhost:11434/v1 for Ollama."}
    if not (model or "").strip():
        return {"ok": False, "stage": "setup", "error": "No model id.",
                "fix": "The exact id the provider expects, e.g. "
                       "deepseek-chat or qwen3:8b."}
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "stage": "setup",
                "error": f"'{base}' isn't a URL.",
                "fix": "It needs to start with http:// or https://."}

    t0 = _t.time()
    try:
        brain = OpenAIBrain(model=model, base_url=base,
                            api_key=(api_key or "none"), label="test",
                            timeout=timeout)
    except Exception as exc:
        return {"ok": False, "stage": "client",
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "fix": "The OpenAI client couldn't be built for that URL."}

    try:
        resp = brain.chat([{"role": "user", "content": "Reply with: ok"}],
                          ["Reply with exactly: ok"], None)
        text = "".join(b.text for b in getattr(resp, "content", [])
                       if getattr(b, "type", "") == "text").strip()
    except Exception as exc:
        return _probe_failure(exc, base, model, round(_t.time() - t0, 1))

    took = round(_t.time() - t0, 1)
    # a friendly error message is still a failure — the brain catches
    # provider errors and returns them as text, so check for that shape
    if text.startswith("_") and text.endswith("_"):
        # OpenAIBrain catches provider errors and returns them as text rather
        # than raising, so the classifier below never saw them — an
        # unreachable host was reported as a vague "provider" problem. Run
        # the message through the same classification.
        return _probe_failure(RuntimeError(text.strip("_")), base, model,
                              took)
    if not text:
        return {"ok": False, "stage": "empty",
                "error": "Connected, but the reply was empty.",
                "seconds": took,
                "fix": "Often a model id the server accepts but can't run."}
    return {"ok": True, "seconds": took, "reply": text[:120],
            "detail": f"Connected and answered in {took}s.",
            "note": ("A working connection isn't a working model — check the "
                     "reply looks sane before trusting it.")}


def _probe_failure(exc, base: str, model: str, took: float) -> dict:
    """Name the cause. Each of these needs a different thing done about it."""
    s = f"{type(exc).__name__}: {exc}"
    low = s.lower()
    local = is_local_endpoint(base)
    if "401" in s or "authentication" in low or "unauthorized" in low:
        return {"ok": False, "stage": "auth", "seconds": took,
                "error": "The provider rejected the API key.",
                "fix": "Check for a copied space, or a key from a different "
                       "product of theirs."}
    if "403" in s or "permission" in low or "forbidden" in low:
        return {"ok": False, "stage": "auth", "seconds": took,
                "error": "The key was accepted but isn't allowed to use this.",
                "fix": "Usually a plan or region restriction on that model."}
    if "404" in s or ("model" in low and "not" in low and "found" in low):
        return {"ok": False, "stage": "model", "seconds": took,
                "error": f"'{model}' isn't a model this endpoint knows.",
                "fix": ("Run `ollama list` and use one of those exactly."
                        if local else
                        "Check the id against the provider's documentation — "
                        "they differ between vendors for the same model.")}
    if "429" in s or "rate" in low:
        return {"ok": False, "stage": "limit", "seconds": took,
                "error": "Rate-limited, which means it connected.",
                "fix": "The engine works. Try again shortly."}
    if ("insufficient" in low or "credit" in low or "quota" in low
            or "billing" in low):
        return {"ok": False, "stage": "credit", "seconds": took,
                "error": "Connected, but the account has no credit.",
                "fix": "Top up, or use a local engine — those cost nothing."}
    if ("connection" in low or "refused" in low or "timed out" in low
            or "timeout" in low or "unreachable" in low
            or "name or service" in low or "getaddrinfo" in low):
        return {"ok": False, "stage": "network", "seconds": took,
                "error": f"Couldn't reach {base}.",
                "fix": ("Is Ollama running? `ollama serve`, and check the "
                        "port matches." if local else
                        "Check the URL, and whether a proxy or firewall is "
                        "in the way.")}
    if "ssl" in low or "certificate" in low:
        return {"ok": False, "stage": "network", "seconds": took,
                "error": "The TLS certificate wasn't accepted.",
                "fix": "Common behind a corporate proxy that re-signs traffic."}
    return {"ok": False, "stage": "unknown", "seconds": took,
            "error": s[:220],
            "fix": "Unrecognised — the message above is the provider's own."}


_ollama_probe = None          # (checked_at, was_running)


def resolve_backend() -> str:
    """Which backend to build, given what's actually set up.

    Order: an engine you configured yourself (local before paid), then a
    running Ollama, then Anthropic if a key exists. No vendor is privileged
    by being written into a default — the first thing that works wins, and
    yours comes first.
    """
    if _first_usable_custom(build=False) is not None:
        return "custom"
    # Constructing OllamaBrain probes the network for up to 3 seconds. This
    # runs whenever a brain is built, and the UI rebuilds one right after
    # saving an engine — so saving appeared to hang for three seconds on any
    # machine without Ollama, every time. Cache the answer briefly: whether
    # Ollama is running doesn't change between two clicks.
    global _ollama_probe
    import time as _t
    now = _t.time()
    if _ollama_probe and now - _ollama_probe[0] < 30:
        if _ollama_probe[1]:
            return "ollama"
    else:
        try:
            OllamaBrain()
            _ollama_probe = (now, True)
            return "ollama"
        except Exception:
            _ollama_probe = (now, False)
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AGENT_API_KEY"):
        return "anthropic"
    return "none"


def make_brain(backend: str | None = None, model: str | None = None):
    backend = (backend or config.BACKEND).lower()
    if backend == "auto":
        backend = resolve_backend()
    if backend == "custom":
        b = _first_usable_custom()
        if b is not None:
            return b
        backend = "anthropic"
    if backend == "none":
        raise EngineNotConfigured(
            "No engine is set up yet.",
            "Add one in Engines — a local Ollama model needs no key at all — "
            "or set an API key in Settings.")
    if backend == "ollama":
        try:
            return OllamaBrain(model)
        except OllamaUnavailable as exc:
            raise EngineNotConfigured(
                exc.message,
                "Start Ollama, or switch to a cloud engine in Settings.")
    if backend == "anthropic":
        try:
            return AnthropicBrain(model)
        except EngineNotConfigured:
            # BACKEND is "anthropic" out of the box, so someone who added a
            # DeepSeek (or any other) engine and no Claude key still got an
            # Anthropic brain built first — and a 500 on their first message.
            # Their own engine is right there; use it.
            fallback = _first_usable_custom()
            if fallback is None:
                raise
            return fallback
    if backend == "hybrid":
        return HybridBrain(cloud_model=model)
    raise EngineNotConfigured(
        f"Unknown backend '{backend}'.",
        "Use 'anthropic', 'ollama' or 'hybrid' in Settings.")
