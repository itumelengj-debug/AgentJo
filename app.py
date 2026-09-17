"""Gradio web interface for the local agent.

A tabbed front-end over the same engine as the CLI — Chat, Memory, Skills,
Tasks, and a capabilities guide. Run with:  python app.py
then open the printed http://127.0.0.1:7860 in your browser.

Two safety modes (toggle on the Chat tab):
  - Safe (default): converse, remember, plan, read files, run read-only
    commands. It will NOT write files or run anything that changes your machine.
  - Full access: runs everything without asking, like the CLI's --yolo.

Conversations in separate browser tabs run in parallel. For per-command
approval, use the terminal (python run.py).
"""

import base64
import copy
import io
import itertools
import json
import os
import queue
import socket
import sys
import threading
import time
import uuid
import warnings
from datetime import datetime
from types import SimpleNamespace

from rich.console import Console

import agent.config as config
import agent.main as main
import agent.tools as tools
from agent.brain import make_brain
import agent.brain as brainmod
from agent.memory import MemoryStore, format_task
from agent import rag
from agent import scheduler
from agent import voice
from agent import files as agent_files
from agent import backup as agent_backup

# Silence the engine's terminal-oriented output into a throwaway buffer; the
# model's text reaches the UI through the streaming callback instead.
main.console = Console(file=io.StringIO(), force_terminal=False)

# Browser approvals: read-only commands auto-run; full-access bypasses prompts;
# so this stub is only reached for a mutating action in SAFE mode -> decline.
tools.Prompt = SimpleNamespace(ask=lambda *a, **k: "n")

# One shared brain + memory; cross-thread DB so worker threads can use it.
memory = MemoryStore(check_same_thread=False)
try:
    brain = make_brain()  # honours AGENT_BACKEND / AGENT_MODEL / API key
except SystemExit:
    raise
except Exception as exc:  # pragma: no cover
    print(f"Could not start the model backend: {exc}")
    sys.exit(1)

try:
    import gradio as gr
except ImportError:
    print("Gradio isn't installed. Run:  pip install -r requirements-app.txt")
    sys.exit(1)


# --- live job tracking across all browser tabs ----------------------------- #
class _Cancelled(Exception):
    """Raised inside the stream callback to abort a cancelled turn."""


_jobs_lock = threading.Lock()
_jobs: dict = {}          # id -> running job dict
_job_history: list = []   # finished jobs, most-recent-first, bounded
_job_ids = itertools.count(1)


def _start_job(session_id: str, message: str) -> dict:
    job = {"id": next(_job_ids), "session": session_id,
           "message": " ".join(message.split())[:100],
           "started": time.time(), "status": "running",
           "cancel": threading.Event()}
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def _finish_job(job: dict, status: str) -> None:
    job["status"] = status
    job["ended"] = time.time()
    with _jobs_lock:
        _jobs.pop(job["id"], None)
        _job_history.insert(0, job)
        del _job_history[20:]


def _cancel_job(job_id: int) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        job["cancel"].set()
        return True
    return False


# --- engine usage + estimated Claude cost (since app start) ----------------- #
_usage_lock = threading.Lock()
_usage = {"local_turns": 0, "claude_turns": 0, "mixed_turns": 0,
          "local_in": 0, "local_out": 0,
          "claude_in": 0, "claude_out": 0,
          "deepseek_turns": 0, "deepseek_in": 0, "deepseek_out": 0,
          "custom_turns": 0, "custom_in": 0, "custom_out": 0,
          "cache_read": 0, "cache_write": 0, "estimated": False}


def _add_usage(info: dict) -> None:
    """Fold one finished turn's turn_info into the running totals."""
    if not info or not info.get("engine"):
        return
    u = info.get("usage", {}) or {}
    with _usage_lock:
        eng = info["engine"]
        if eng == "local":
            _usage["local_turns"] += 1
            _usage["local_in"] += int(u.get("in", 0))
            _usage["local_out"] += int(u.get("out", 0))
        elif eng == "deepseek":
            _usage["deepseek_turns"] += 1
            _usage["deepseek_in"] += int(u.get("in", 0))
            _usage["deepseek_out"] += int(u.get("out", 0))
        elif eng == "custom":
            _usage["custom_turns"] += 1
            _usage["custom_in"] += int(u.get("in", 0))
            _usage["custom_out"] += int(u.get("out", 0))
        else:                  # claude (mixed turns count as Claude spend)
            _usage["claude_turns" if eng == "claude" else "mixed_turns"] += 1
            _usage["claude_in"] += int(u.get("in", 0))
            _usage["claude_out"] += int(u.get("out", 0))
            _usage["cache_read"] += int(u.get("cache_read", 0))
            _usage["cache_write"] += int(u.get("cache_write", 0))
        if info.get("estimated"):
            _usage["estimated"] = True


def _est_cost() -> tuple[float, dict]:
    """Estimated Claude spend in USD (cache writes 1.25x input rate,
    cache reads 0.10x), plus a snapshot of the totals."""
    with _usage_lock:
        u = dict(_usage)
    cost = (u["claude_in"] * config.PRICE_IN_PER_M
            + u["cache_write"] * config.PRICE_IN_PER_M * 1.25
            + u["cache_read"] * config.PRICE_IN_PER_M * 0.10
            + u["claude_out"] * config.PRICE_OUT_PER_M) / 1_000_000
    return cost, u


def _engine_tag(info: dict) -> str:
    """Small per-reply marker: which engine produced it."""
    eng = (info or {}).get("engine")
    if not eng:
        return ""
    model_name = info.get("model") or ""
    label = f"{eng} \u00b7 {model_name}" if model_name else eng
    return f"\n\n*[{label}]*"


def _routing_md() -> str:
    try:
        n = memory.count_routing_escalations()
        rows = memory.get_routing_escalations(limit=8)
    except Exception:
        return "_Routing feedback unavailable._"
    if not n:
        return ("ROUTING FEEDBACK\n\nNo escalations learned yet. When a local "
                "answer is weak, click \"Retry on Claude\" under the chat: Agent Jo "
                "reruns it on Claude and learns to route similar prompts there in "
                "Auto mode.")
    lines = [f"ROUTING FEEDBACK ({n} learned) \u2014 prompts like these now go to Claude:", ""]
    lines += [f"  \u2022 {' '.join(r['text'].split())[:80]}" for r in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def do_clear_routing():
    try:
        n = memory.clear_routing_escalations()
    except Exception as exc:
        return _routing_md(), f"Could not clear: {exc}"
    return _routing_md(), f"Cleared {n} learned escalation(s)."


def _usage_md() -> str:
    cost, u = _est_cost()
    a = "~" if u["estimated"] else ""
    claude_turns = u["claude_turns"] + u["mixed_turns"]
    lines = [
        "ENGINE USAGE (since app start; chat + scheduled turns)", "",
        f"  local    turns {u['local_turns']:>4}   "
        f"tokens {a}{u['local_in'] + u['local_out']:,}",
        f"  claude   turns {claude_turns:>4}   in {a}{u['claude_in']:,} "
        f"(cache r {u['cache_read']:,} / w {u['cache_write']:,})   "
        f"out {a}{u['claude_out']:,}",
    ]
    if u.get("deepseek_turns"):
        lines.append(
            f"  deepseek turns {u['deepseek_turns']:>4}   "
            f"tokens {a}{u['deepseek_in'] + u['deepseek_out']:,}   "
            "(billed by your DeepSeek provider, not in the Claude cost below)")
    if u.get("custom_turns"):
        lines.append(
            f"  engines  turns {u['custom_turns']:>4}   "
            f"tokens {a}{u['custom_in'] + u['custom_out']:,}   "
            "(your added engines; billed by their providers, not below)")
    lines += [
        "",
        f"  estimated Claude cost   ${cost:,.4f}",
        f"  (rates {config.PRICE_IN_PER_M:g} in / {config.PRICE_OUT_PER_M:g} out, "
        f"USD per million tokens; override with AGENT_PRICE_IN / AGENT_PRICE_OUT)",
    ]
    return "```\n" + "\n".join(lines) + "\n```"


WELCOME = (
    f"### Hi, I'm {config.AGENT_NAME}\n"
    "How can I help you today? I remember what matters across our conversations, "
    "I can plan and carry out multi-step work, and \u2014 with your go-ahead \u2014 "
    "work with files on your computer. Pick a suggestion below or just start typing."
)
SAFE_NOTE = ("MODE: SAFE \u2014 read files and run read-only commands only. "
             "Will not write files or run state-changing commands.")
FULL_NOTE = ("MODE: FULL ACCESS \u2014 will create or modify files and run "
             "commands without confirmation. Use only while supervising, and "
             "never on data that cannot leave this machine.")
STARTERS = [
    "Remember that I work on the FX desk at Standard Bank.",
    "What can you do?",
    "Help me plan a multi-step task.",
    "Read and summarise a file on my computer.",
]


# ---------------------------------------------------------------------- #
# Display helpers (all plain Markdown — maximally version-stable)
# ---------------------------------------------------------------------- #
def _stats_html() -> str:
    with _jobs_lock:
        nrun = len(_jobs)

    def card(label, value, cls=""):
        return (f'<div class="kpi{cls}"><div class="kpi-val">{value}</div>'
                f'<div class="kpi-lbl">{label}</div></div>')

    web_on = tools.WEB_ENABLED
    return ('<div class="kpi-row">'
            + card("Memories", memory.memory_count())
            + card("Skills", memory.skill_count())
            + card("Tasks", len(memory.active_tasks()))
            + card("Active jobs", nrun, " good" if nrun else "")
            + card("Documents", rag.get_store().doc_count())
            + card("Est. cost", f"${_est_cost()[0]:.2f}")
            + card("Web", "On" if web_on else "Off", " good" if web_on else " off")
            + '</div>')


def _memories_md(query: str = "") -> str:
    q = (query or "").strip()
    items = memory.search_memories(q, 100) if q else memory.all_memories()
    if not items:
        return "_No matches._" if q else "_Nothing learned yet. Tell me to remember something._"
    head = f"**{len(items)} " + ("match" if q else "memor") + \
           ("" if (q and len(items) == 1) else ("es" if q else ("y" if len(items) == 1 else "ies"))) + "**\n"
    lines = [head]
    for m in items:
        src = m.get("source", "")
        tag = " · auto" if src in ("auto", "learned") else ""
        lines.append(f"- **#{m['id']}** · _{m['category']}_{tag} · {m['created_at'][:10]}  \n  {m['content']}")
    return "\n".join(lines)


def _skills_md() -> str:
    skills = memory.get_skills()
    if not skills:
        return "_No skills taught yet — use the form below to teach one._"
    out = [f"**{len(skills)} skill(s)**\n"]
    for s in skills:
        out.append(f"### {s['name']}\n*When:* {s['description']}\n\n*Does:* {s['instructions']}")
    return "\n".join(out)


def _tasks_md(status: str = "all") -> str:
    tasks = memory.list_tasks(status if status in ("active", "completed", "abandoned") else "all", 30)
    if not tasks:
        return "_No tasks yet. They appear when you give me multi-step work._"
    return "\n\n".join("```\n" + format_task(t) + "\n```" for t in tasks)


CAPABILITIES = f"""
## {config.AGENT_NAME} \u2014 capabilities

**Persistent memory**
State facts, preferences, or rules ("remember that...", "from now on always...") and I retain them across every session. I also capture durable details as we talk. Review, search, or delete everything in the Memory tab, and use "Review duplicates" to find and remove reworded repeats.

**Taught routines**
In the Skills tab, define a named procedure once (for example, a morning briefing) and I will run it on request \u2014 this session and every future one.

**Planning and verification**
For multi-step work I write a plan, execute it in order, and verify each step before marking it done. Track progress in the Tasks tab.

**Filesystem and shell**  (Full access mode)
With Full access enabled I can read and write files and run commands \u2014 summarise a document, fix a script, reorganise a directory. In Safe mode I can read files and run read-only commands, but will not modify anything.

**Delegation**
For large self-contained units of work \u2014 research, multi-file exploration \u2014 I can dispatch an isolated worker that reports back, keeping long jobs coherent.

**Scheduled tasks**
In the Schedules tab, give me a standing instruction — a morning briefing, a weekly summary — and I run it on my own while Agent Jo is open. Results land in History; live runs show in Jobs.

**Conversation history**
Conversations are saved automatically. Browse them in the History tab and load one to continue exactly where it left off.

**Backup & restore**
Save everything Agent Jo knows to a single zip (memories, skills, tasks, schedules, conversations, documents) from the Backup tab, and restore it later. Backups stay on your machine.

**File uploads in chat**
Attach PDFs, Word, Excel, text, or code files in the chat and ask about them. Their text is extracted locally and given to the model, so it works on Claude or a local model.

**Image understanding (vision)**
Attach an image in the chat and ask about it — a chart, a screenshot, a photo. Under Auto, images are sent to Claude; you can also choose a local vision model in the Engine selector.

**Voice (local and private)**
Open the Voice panel under the chat to speak instead of type (transcribed locally) and to have replies read aloud in your computer's own voice. No audio ever leaves this machine.

**Smarter routing (learns from you)**
In Auto mode I send simple turns to the local model to save tokens. If a local answer falls short, click "Retry on Claude" under the chat: I redo that turn on Claude and remember the prompt, so similar prompts route to Claude automatically afterwards. Manage what I've learned in the Jobs tab.

**DeepSeek and other engines**
Alongside Claude and the local models, I can use DeepSeek V4 (Pro and Flash) or any OpenAI-compatible endpoint. Set a key and base URL for your provider (DeepSeek's own API, NVIDIA, or OpenRouter) and the two DeepSeek options appear in the Engine selector. You can also add your own engines on the Engines tab - give a name, model id, base URL and key (OpenAI, Groq, Mistral, OpenRouter, a local vLLM server, ...), test it, and it shows up in the selector right away with no restart. Load any saved engine back into the form to edit or rename it, and toggle per-engine whether it supports tools and streaming. Engine tokens are tracked separately and are not counted in the Claude cost estimate.

**Web access**
I can search the web and read pages for current information — news, prices, recent releases — always citing my sources. The Web access toggle on the Chat tab turns this off entirely for sessions where nothing should leave this machine.

**Local and private**
Everything I learn is stored in a local database on this machine. With the cloud backend, messages are sent to the model for reasoning; with the offline backend and Web access off, nothing leaves the machine at all.

**Operation**
- Open a second browser tab to run another session in parallel.
- Use the terminal (python run.py) to approve commands individually.
- Monitor and cancel running work in the Jobs tab.
- While a reply is being prepared, the bubble shows a live status naming the current phase and the seconds elapsed (for example, "working - running web_search (12s)"), and the terminal prints a matching per-phase timing trace, so you can always see what is being processed; if a turn runs too long it stops on its own with a message rather than hanging (set AGENT_TURN_TIMEOUT, default 300s).
- Learning durable facts happens in the background after each reply, so it never holds up the conversation.
- The Settings tab adjusts behaviour at runtime (fact-learning, routing, timeout, tool-call and token limits, memory and history sizes) without a restart; changes persist across restarts.
- If a long reply is cut off by the length limit, it continues automatically and stitches the parts into one complete answer, so you do not have to keep typing "continue".
- If a turn uses up its tool-call budget before writing its answer, it makes a final pass with tools off and delivers the result, instead of dead-ending on "round limit reached".
- Each reply is tagged with the engine that produced it; the Jobs tab totals usage and estimated Claude cost.
"""


def _new_session() -> dict:
    return {"id": uuid.uuid4().hex[:12], "messages": []}


# Chat-history format the installed Gradio supports. Defaults to the modern
# messages format; flips to legacy tuples at build time if this Gradio rejects
# it. Lets the app run across essentially any Gradio version.
CHAT_FORMAT = "messages"


def _append_pair(history, user_msg):
    if CHAT_FORMAT == "messages":
        return history + [{"role": "user", "content": user_msg},
                          {"role": "assistant", "content": ""}]
    return history + [[user_msg, ""]]


def _set_assistant(history, text):
    if CHAT_FORMAT == "messages":
        history[-1]["content"] = text
    else:
        history[-1][1] = text


def _build_mic():
    """Microphone input across Gradio versions; None if unsupported."""
    try:
        return gr.Audio(sources=["microphone"], type="filepath",
                        label="Hold to record, release to transcribe",
                        elem_id="mic")
    except Exception:
        pass
    try:
        return gr.Audio(source="microphone", type="filepath", label="Record")
    except Exception:
        return None


_IMG_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}


def _image_block(path):
    """Turn an uploaded image into an Anthropic image content block (base64),
    downscaling oversized images so the request stays within vision limits.
    Returns the block, or None."""
    if not path:
        return None
    try:
        raw = open(path, "rb").read()
    except Exception:
        return None
    media = _IMG_MEDIA.get(os.path.splitext(path)[1].lower())
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        fmt = (im.format or "").upper()
        media = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif",
                 "WEBP": "image/webp"}.get(fmt, media)
        long_edge = max(im.size)
        if long_edge > 1568 or len(raw) > 4_500_000:          # vision-friendly size
            scale = min(1.0, 1568 / long_edge)
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)),
                                max(1, int(im.height * scale))))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            raw = buf.getvalue()
            media = "image/jpeg"
    except Exception:
        pass
    if not media:
        media = "image/png"
    b64 = base64.standard_b64encode(raw).decode()
    return {"type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64}}


def _build_image_input():
    """Image upload across Gradio versions; None if unsupported."""
    try:
        return gr.Image(sources=["upload", "clipboard"], type="filepath",
                        label="Attach an image to ask about it", height=200,
                        elem_id="imgin")
    except Exception:
        pass
    try:
        return gr.Image(type="filepath", label="Attach an image")
    except Exception:
        return None


def _read_attachments(paths):
    """Extract text from uploaded files into a single delimited block ready to
    prepend to the user's message. Returns (block_text, names, notes)."""
    if not paths:
        return "", [], []
    if isinstance(paths, (str, bytes)):
        paths = [paths]
    chunks, names, notes = [], [], []
    for item in paths:
        path = item.get("path") if isinstance(item, dict) else (
            item.name if hasattr(item, "name") else item)
        if not path:
            continue
        text, note = agent_files.extract_text(path)
        name = os.path.basename(path)
        names.append(name)
        notes.append(f"{name}: {note}")
        if text:
            chunks.append(f"===== FILE: {name} =====\n{text}\n===== END FILE =====")
    if not chunks:
        return "", names, notes
    header = ("The user attached the following file(s). Use their contents to "
              "answer.\n\n")
    return header + "\n\n".join(chunks) + "\n\n", names, notes


def _build_file_input():
    """File upload across Gradio versions; None if unsupported."""
    try:
        return gr.File(file_count="multiple", type="filepath",
                       label="Attach files (PDF, Word, Excel, text, code, ...)",
                       elem_id="filein")
    except Exception:
        pass
    try:
        return gr.File(file_count="multiple", label="Attach files")
    except Exception:
        return None


def _build_chatbot():
    """Create the Chatbot across Gradio versions; return (component, format)."""
    msg_value = [{"role": "assistant", "content": WELCOME}]
    avatars = (None, _AVATAR_PATH) if globals().get("_HAS_AVATAR") else None
    try:                       # current 6.x: messages format, no `type` arg
        return gr.Chatbot(height=680, value=msg_value, elem_id="terminal",
                          avatar_images=avatars), "messages"
    except Exception:
        pass
    try:                       # 5.x / late 4.x: explicit type selector
        return gr.Chatbot(type="messages", height=680, value=msg_value,
                          elem_id="terminal", avatar_images=avatars), "messages"
    except Exception:
        pass
    return gr.Chatbot(height=680, value=[(None, WELCOME)], elem_id="terminal"), "tuples"  # very old


# ---------------------------------------------------------------------- #
# Chat turn (streamed; one at a time per conversation)
# ---------------------------------------------------------------------- #
def _seal_history(msgs):
    """Keep the conversation valid if a turn ended with no assistant reply
    (e.g. cancelled), so the next turn's role alternation stays correct."""
    if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "user":
        msgs.append({"role": "assistant", "content": "(stopped)"})


def _engine_choices() -> list:
    """Engine selector options. Each DeepSeek engine appears only when it has a
    key (its own per-model key, or the shared key). User-added engines follow."""
    choices = ["Auto", "Claude", "Ollama", config.OLLAMA_MODEL_2]
    if config.deepseek_pro_key():
        choices.append("DeepSeek Pro")
    if config.deepseek_flash_key():
        choices.append("DeepSeek Flash")
    choices += brainmod.custom_engine_names()
    return choices


def _force_model_for(choice):
    """Translate the Engine selector into a run_turn override.
    'Auto' -> automatic routing; 'Claude' -> cloud; 'Ollama' -> local default;
    the second local model name -> that local model; DeepSeek -> its model id."""
    backend = getattr(brain, "backend", "")
    local = getattr(brain, "local", None)
    has_local = backend == "ollama" or (backend == "hybrid" and local is not None)
    if choice == "Claude":
        return None                       # None == the cloud/default engine
    if choice == "DeepSeek Pro":
        return config.DEEPSEEK_MODEL_PRO
    if choice == "DeepSeek Flash":
        return config.DEEPSEEK_MODEL_FLASH
    if choice in brainmod.custom_engine_names():
        return choice                     # routed by engine name in the dispatch
    if choice == "Ollama":
        if backend == "hybrid" and local is not None:
            return brain.fast_model       # the local-engine marker
        return None                       # pure-ollama default, or no local
    if choice == config.OLLAMA_MODEL_2:
        return config.OLLAMA_MODEL_2 if has_local else None
    return main._AUTO                     # Auto (current hybrid routing)


def _engines_table_md() -> str:
    """All engines in one place: built-in (read-only) plus user-added."""
    rows = ["**Built-in**",
            f"- Claude &middot; `{config.MODEL}`",
            f"- Ollama &middot; `{config.OLLAMA_MODEL}` (+ `{config.OLLAMA_MODEL_2}`)"]
    if config.deepseek_pro_key():
        rows.append(f"- DeepSeek Pro &middot; `{config.DEEPSEEK_MODEL_PRO}`")
    if config.deepseek_flash_key():
        rows.append(f"- DeepSeek Flash &middot; `{config.DEEPSEEK_MODEL_FLASH}`")
    rows += ["", "**Your engines**"]
    customs = brainmod.load_custom_engines(refresh=True)
    if customs:
        for e in customs:
            keyed = "key set" if e.get("api_key") else "no key"
            caps = []
            caps.append("tools" if e.get("tools", True) else "no tools")
            caps.append("streaming" if e.get("stream", True) else "no streaming")
            rows.append(f"- {e['name']} &middot; `{e['model']}` via "
                        f"`{e['base_url']}` ({keyed}; {', '.join(caps)})")
    else:
        rows.append("_None yet - add one below._")
    return "\n".join(rows)


def _engine_updates():
    """gr.update payloads to refresh the two engine selectors and remove-list."""
    choices = _engine_choices()
    names = brainmod.custom_engine_names()
    radio = gr.update(choices=choices)
    dd = gr.update(choices=names, value=(names[0] if names else None))
    return radio, dd


def do_add_engine(name, base_url, model, api_key, tools, stream, editing):
    ok, msg = brainmod.add_custom_engine(name, base_url, api_key, model,
                                         tools=bool(tools), stream=bool(stream))
    if ok:
        old = (editing or "").strip()
        if old and old != (name or "").strip():
            if brainmod.remove_custom_engine(old)[0]:
                msg = f"Saved '{(name or '').strip()}' (renamed from '{old}')."
    radio, dd = _engine_updates()
    blank = gr.update(value="")
    on = gr.update(value=True)
    keep = gr.update()
    if ok:
        return (msg, _engines_table_md(), radio, radio, dd,
                blank, blank, blank, blank, on, on, "")
    return (msg, _engines_table_md(), radio, radio, dd,
            keep, keep, keep, keep, keep, keep, editing)


def do_load_engine(name):
    """Populate the form from a saved engine for editing."""
    e = brainmod.custom_engine_by_name(name)
    if e is None:
        return ("", "", "", "", True, True, "",
                "Pick an engine from the list first.")
    return (e["name"], e["base_url"], e["model"], e.get("api_key", ""),
            bool(e.get("tools", True)), bool(e.get("stream", True)), e["name"],
            f"Loaded '{e['name']}' - edit the fields above, then click Save engine.")


def do_clear_engine_form():
    return ("", "", "", "", True, True, "", "Form cleared.")


def do_remove_engine(name):
    ok, msg = brainmod.remove_custom_engine(name)
    radio, dd = _engine_updates()
    return (msg, _engines_table_md(), radio, radio, dd)


def do_test_engine(name, base_url, model, api_key):
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    if not base_url or not model:
        return "Enter at least a base URL and a model id to test."
    try:
        b = brainmod.OpenAIBrain(model=model, base_url=base_url,
                              api_key=(api_key or "").strip() or "none",
                              label="custom")
        # _do_chat raises on failure (unlike chat, which returns a friendly note),
        # so we can report a genuine pass/fail here.
        resp = b._do_chat([{"role": "user", "content": "ping"}],
                          "Reply with the single word: ok", None, None, model)
        txt = "".join(x.text for x in resp.content
                      if getattr(x, "type", None) == "text").strip()
        return (f"Connection OK - reply: {txt[:160]}" if txt
                else "Connected, but the reply was empty.")
    except Exception as exc:
        return f"Test failed - {type(exc).__name__}: {str(exc)[:200]}"


# Speak replies aloud (opt-in; toggled by the Voice panel checkbox).
_speak_enabled = config.VOICE_TTS_DEFAULT


def _set_speak(enabled):
    global _speak_enabled
    _speak_enabled = bool(enabled)


def do_transcribe(audio_path):
    """Transcribe a recorded clip locally and put the text in the message box."""
    if not audio_path:
        return gr.update()
    if not voice.stt_available():
        try:
            gr.Info("Speech-to-text needs faster-whisper. Install it with:  "
                    "pip install faster-whisper")
        except Exception:
            pass
        return gr.update()
    try:
        text = voice.transcribe(audio_path, config.VOICE_STT_MODEL)
    except Exception as exc:
        try:
            gr.Info(f"Transcription failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return gr.update()
    if not text:
        try:
            gr.Info("Didn't catch any speech in that clip.")
        except Exception:
            pass
        return gr.update()
    return text


def do_stop_speaking():
    voice.stop()


def respond(message, history, full_access, engine_choice, sess, image=None, files=None):
    """Stream one turn in a worker thread, tracked as a cancellable job.
    Separate tabs run in parallel; within one conversation a second send
    while busy is deferred. An optional image is sent to the vision model;
    optional files are read locally and their text is given to the model."""
    message = (message or "").strip()
    img_block = _image_block(image) if image else None
    attach_text, attach_names, attach_notes = _read_attachments(files)
    if not message and not img_block and not attach_text and not attach_names:
        yield history, sess, ""
        return
    if attach_notes:
        try:
            gr.Info(" | ".join(attach_notes))
        except Exception:
            pass
    if not message and (img_block or attach_text):
        message = ("What's in this image?" if img_block and not attach_text
                   else "Summarize the attached file(s).")
    if img_block and engine_choice in ("Ollama", "DeepSeek Pro", "DeepSeek Flash"):
        try:
            gr.Info("This engine can't see images here. Use Auto, Claude, or "
                    + config.OLLAMA_MODEL_2 + " for pictures.")
        except Exception:
            pass
    if not sess or not sess.get("id"):
        sess = _new_session()

    prior = sess.get("_worker")
    if sess.get("busy") or (prior is not None and prior.is_alive()):
        try:
            gr.Info("Still finishing your previous message. Open a new browser "
                    "tab to run a second task at the same time, or use Clear to "
                    "start fresh.")
        except Exception:
            pass
        yield history, sess, message  # keep their text in the box
        return

    out: dict = {}
    info: dict = {}
    job = _start_job(sess["id"], message)
    sess["busy"] = True
    sess["prev_messages"] = copy.deepcopy(sess["messages"])  # for Retry rewind
    force_model = _force_model_for(engine_choice)
    try:
        memory.log_message(sess["id"], "user", message)   # transcript -> History
    except Exception:
        pass
    try:
        marks = []
        if img_block:
            marks.append("image attached")
        if attach_names:
            marks.append("attached: " + ", ".join(attach_names))
        shown = message + (("\n\n*[" + "; ".join(marks) + "]*") if marks else "")
        history = _append_pair(history, shown)
        q: queue.Queue = queue.Queue()

        def on_text(t):
            if job["cancel"].is_set():
                raise _Cancelled()        # abort an in-progress stream
            q.put(t)

        status = {"phase": "starting"}
        def on_status(phase):
            status["phase"] = str(phase)

        def worker():
            try:
                out["reply"] = main.run_turn(
                    brain, memory, sess["messages"], message,
                    auto_approve=bool(full_access), session_id=sess["id"],
                    on_text=on_text, should_cancel=job["cancel"].is_set,
                    force_model=force_model, turn_info=info,
                    images=[img_block] if img_block else None,
                    attachments=attach_text, on_status=on_status)
                try:                          # compact within the timed turn
                    sess["messages"] = main.compact_history(brain, sess["messages"])
                except Exception:
                    pass
            except _Cancelled:
                out["cancelled"] = True
            except Exception as exc:  # surface, don't crash the UI
                out["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                q.put(None)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        sess["_worker"] = worker_thread

        acc = ""
        start = time.time()
        timeout = getattr(config, "TURN_TIMEOUT", 0)

        def _working(extra=""):
            # Live status shown while the turn is working but not streaming text,
            # so a long model call or tool run isn't an unexplained blank wait.
            secs = int(time.time() - start)
            line = f"_working — {status['phase']} ({secs}s)_"
            return (acc + "\n\n" + line) if acc.strip() else line

        _set_assistant(history, _working())   # never show a blank bubble
        yield history, sess, ""

        while True:
            try:
                tok = q.get(timeout=1.0)
            except queue.Empty:
                if timeout and (time.time() - start) > timeout:
                    job["cancel"].set()          # ask the worker to wind down
                    out["timeout"] = True
                    out["error"] = (
                        f"This turn ran past {timeout}s and was stopped. The model "
                        "may be doing repeated tool calls or a slow local "
                        "generation, or a local model/engine may be unreachable. "
                        "Try a shorter prompt, switch engine, untick Web access, "
                        "or raise AGENT_TURN_TIMEOUT.")
                    break
                _set_assistant(history, _working())   # tick the live status
                yield history, sess, ""
                continue                          # still working; keep waiting
            if tok is None:
                break
            if not isinstance(tok, str):     # safety net: never crash the stream
                tok = "".join(tok) if isinstance(tok, list) and all(
                    isinstance(x, str) for x in tok) else str(tok)
            acc += tok
            _set_assistant(history, acc)
            yield history, sess, ""

        tag = ""
        if out.get("timeout"):
            final = ((acc.strip() + "\n\n" + out["error"]) if acc.strip()
                     else out["error"])
            _seal_history(sess["messages"])
        elif job["cancel"].is_set() or out.get("cancelled"):
            final = (acc.strip() + "\n\n[cancelled]") if acc.strip() else "[cancelled]"
            _seal_history(sess["messages"])
        elif out.get("error"):
            final = out["error"]
            _seal_history(sess["messages"])
        else:
            final = out.get("reply") or acc or "_(no response)_"
            # Fact extraction uses the local engine and can be slow; run it in
            # the background so it never holds up the turn from completing.
            def _learn(text=final, user=message):
                _s = time.time()
                try:
                    main.auto_learn(brain, memory, user, text)
                    main.console.print("[dim]· auto-learn (background) done in "
                                       f"{time.time() - _s:.1f}s[/dim]")
                except Exception:
                    pass
            threading.Thread(target=_learn, daemon=True).start()
            try:
                memory.log_message(sess["id"], "assistant", final)
            except Exception:
                pass
            _add_usage(info)
            tag = _engine_tag(info)
            sess["last_user"] = message
            sess["last_engine"] = info.get("engine")
            if _speak_enabled:
                try:
                    voice.speak(final)
                except Exception:
                    pass
        _set_assistant(history, final + tag)
        yield history, sess, ""
    finally:
        status = ("cancelled" if (job["cancel"].is_set() or out.get("cancelled"))
                  else "error" if out.get("error") else "done")
        _finish_job(job, status)
        sess["busy"] = False


def _has_cloud() -> bool:
    return getattr(brain, "backend", "") in ("anthropic", "hybrid")


def _drop_last_exchange(history):
    """Remove the trailing user+assistant bubbles from the DISPLAY history
    (tool rounds never appear as separate bubbles, so this is always one pair)."""
    h = list(history or [])
    if CHAT_FORMAT == "messages":
        if h and isinstance(h[-1], dict) and h[-1].get("role") == "assistant":
            h.pop()
        if h and isinstance(h[-1], dict) and h[-1].get("role") == "user":
            h.pop()
    elif h:
        h.pop()
    return h


def _log_escalation(text: str) -> None:
    """Record a prompt the user escalated to Claude, with a local embedding if
    available, so Auto routing sends similar prompts to Claude next time."""
    emb = None
    try:
        from agent import rag
        vecs = rag.embed_texts([text])
        if vecs:
            emb = json.dumps(vecs[0])
    except Exception:
        emb = None
    try:
        memory.add_routing_escalation(text, emb)
    except Exception:
        pass


def retry_on_claude(history, full_access, sess):
    """Re-run the last user turn on Claude. If that turn had been answered by
    a local model, log it as a routing-escalation signal."""
    if not sess or not sess.get("last_user"):
        try:
            gr.Info("Nothing to retry yet — send a message first.")
        except Exception:
            pass
        yield history, sess, ""
        return
    if not _has_cloud():
        try:
            gr.Info("This build has no Claude engine to retry on.")
        except Exception:
            pass
        yield history, sess, ""
        return
    if sess.get("busy"):
        try:
            gr.Info("Still finishing the previous message.")
        except Exception:
            pass
        yield history, sess, ""
        return
    last_user = sess["last_user"]
    if sess.get("last_engine") == "local":      # only log if Claude wasn't already used
        _log_escalation(last_user)
    if "prev_messages" in sess:                 # rewind engine history to pre-turn
        sess["messages"] = sess["prev_messages"]
    history = _drop_last_exchange(history)       # rewind the display
    yield from respond(last_user, history, full_access, "Claude", sess)


def clear_conversation(sess):
    return [], _new_session()


def toggle_note(full_access):
    return FULL_NOTE if full_access else SAFE_NOTE


# Tab action handlers
def do_search_memory(query):
    return _memories_md(query)


def do_show_all_memory():
    return "", _memories_md("")


def do_delete_memory(id_str, query):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _memories_md(query), "Enter a numeric memory ID (the number after #).", _stats_html()
    ok = memory.delete_memory(int(id_str))
    msg = f"Deleted memory #{id_str}." if ok else f"No memory #{id_str} found."
    return _memories_md(query), msg, _stats_html()


def _dedup_preview_md(groups):
    if not groups:
        return ("_No near-duplicate memories found._ Reworded facts are matched "
                "by word overlap and, when the local embedder is running, by "
                "meaning.")
    total = sum(len(g["remove"]) for g in groups)
    out = [f"**Found {len(groups)} group(s) of similar memories — {total} can be "
           "removed.** The most detailed memory in each group is kept; review "
           "below, then apply.\n"]
    for i, g in enumerate(groups, 1):
        k = g["keep"]
        out.append(f"**Group {i}** — keep **#{k['id']}**: {k['content']}")
        for r in g["remove"]:
            out.append(f"  - remove #{r['id']}: {r['content']}")
    return "\n".join(out)


def do_find_duplicates():
    groups = memory.find_similar_groups()
    remove_ids = [r["id"] for g in groups for r in g["remove"]]
    label = (f"Remove {len(remove_ids)} duplicate"
             + ("" if len(remove_ids) == 1 else "s")) if remove_ids else "Nothing to remove"
    return (_dedup_preview_md(groups), remove_ids,
            gr.update(value=label, interactive=bool(remove_ids)))


def do_apply_dedup(remove_ids, query):
    reset_btn = gr.update(value="Nothing to remove", interactive=False)
    if not remove_ids:
        return (_memories_md(query),
                "Run 'Review duplicates' first to find anything to remove.",
                _stats_html(), "", [], reset_btn)
    n = memory.delete_memories(remove_ids)
    msg = (f"Removed {n} duplicate memor" + ("y" if n == 1 else "ies")
           + ", keeping the most detailed in each group.")
    return (_memories_md(query), msg, _stats_html(),
            "_Duplicates removed._", [], reset_btn)


def _build_download():
    """Output-only file component for downloading the backup zip."""
    try:
        return gr.File(label="Download backup", interactive=False)
    except Exception:
        try:
            return gr.File(label="Download backup")
        except Exception:
            return None


def _settings_int(x, lo, default):
    try:
        v = int(round(float(x)))
    except Exception:
        return default
    return max(lo, v)


def do_apply_settings(auto_learn, routing, prompt_cache, subagents,
                      turn_timeout, max_rounds, max_tokens, max_mems,
                      max_hist, compact_trigger, compact_keep):
    config.save_settings({
        "AUTO_LEARN": bool(auto_learn),
        "ROUTING": bool(routing),
        "PROMPT_CACHE": bool(prompt_cache),
        "SUBAGENTS": bool(subagents),
        "TURN_TIMEOUT": _settings_int(turn_timeout, 0, 300),
        "MAX_TOOL_ROUNDS": _settings_int(max_rounds, 1, 15),
        "MAX_TOKENS": _settings_int(max_tokens, 256, 4096),
        "MAX_MEMORIES_IN_CONTEXT": _settings_int(max_mems, 0, 12),
        "MAX_HISTORY_TURNS": _settings_int(max_hist, 1, 20),
        "COMPACT_TRIGGER_TURNS": _settings_int(compact_trigger, 2, 20),
        "COMPACT_KEEP_TURNS": _settings_int(compact_keep, 1, 10),
    })
    return ("**Saved.** These apply from your next message and persist across "
            "restarts.")


def do_reset_settings():
    d = config.reset_settings()
    return (d["AUTO_LEARN"], d["ROUTING"], d["PROMPT_CACHE"], d["SUBAGENTS"],
            d["TURN_TIMEOUT"], d["MAX_TOOL_ROUNDS"], d["MAX_TOKENS"],
            d["MAX_MEMORIES_IN_CONTEXT"], d["MAX_HISTORY_TURNS"],
            d["COMPACT_TRIGGER_TURNS"], d["COMPACT_KEEP_TURNS"],
            "**Reset to defaults.** Saved overrides removed.")


def do_create_backup():
    try:
        rag_store = rag.get_store()
    except Exception:
        rag_store = None
    try:
        path = agent_backup.create_backup(memory, rag_store)
    except Exception as exc:
        return f"Backup failed: {type(exc).__name__}: {exc}", gr.update()
    man = agent_backup.read_manifest(path)
    c = man.get("counts", {})
    msg = ("**Backup saved.**\n\nOn this machine: `" + str(path) + "`\n\n"
           f"Contains {c.get('memories', 0)} memories, {c.get('skills', 0)} skills, "
           f"{c.get('conversations', 0)} conversations, {c.get('documents', 0)} "
           "documents. Use the download below to copy it elsewhere.")
    return msg, gr.update(value=str(path), visible=True)


def _restore_path(uploaded):
    if not uploaded:
        return None
    if isinstance(uploaded, str):
        return uploaded
    if isinstance(uploaded, dict):
        return uploaded.get("path") or uploaded.get("name")
    return getattr(uploaded, "name", None)


def do_restore_backup(uploaded, confirmed):
    path = _restore_path(uploaded)
    if not path:
        return ("Choose a backup .zip to restore first.",
                _memories_md(""), _docs_md(), _stats_html())
    if not confirmed:
        return ("Tick the confirmation box first — restoring replaces all "
                "current data.", _memories_md(""), _docs_md(), _stats_html())
    try:
        rag_store = rag.get_store()
    except Exception:
        rag_store = None
    try:
        summary = agent_backup.restore_backup(path, memory, rag_store)
    except Exception as exc:
        return (f"Restore failed: {type(exc).__name__}: {exc}",
                _memories_md(""), _docs_md(), _stats_html())
    items = ", ".join(summary.get("restored", [])) or "data"
    msg = ("**Restore complete.** Now have: " + items + ". Other tabs have been "
           "refreshed; restart Agent Jo if anything looks stale.")
    return msg, _memories_md(""), _docs_md(), _stats_html()


def do_teach_skill(name, when, what):
    if not (name or "").strip() or not (what or "").strip():
        return _skills_md(), "Need at least a name and what it should do.", _stats_html()
    replaced = memory.add_skill(name, when or "", what)
    clean = name.strip().lower().replace(" ", "-")
    verb = "Updated" if replaced else "Taught"
    return _skills_md(), f"{verb} skill '{clean}'. It now applies in every session.", _stats_html()


def _jobs_md() -> str:
    now = time.time()
    with _jobs_lock:
        running = sorted(_jobs.values(), key=lambda j: j["started"])
        history = list(_job_history[:10])
    lines = [f"RUNNING ({len(running)})", ""]
    if running:
        for j in running:
            lines.append(f"  [{('#'+str(j['id'])):>4}]  {int(now - j['started']):>4}s   {j['message']}")
    else:
        lines.append("  none")
    if history:
        lines += ["", "RECENT", ""]
        for j in history:
            dur = int(j.get("ended", j["started"]) - j["started"])
            lines.append(f"  [{j['status']:<9}] {('#'+str(j['id'])):>4}  {dur:>4}s   {j['message']}")
    return "```\n" + "\n".join(lines) + "\n```"


def do_cancel_job(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _jobs_md(), "Enter a numeric job ID (the number after #)."
    ok = _cancel_job(int(id_str))
    msg = (f"Asked job #{id_str} to stop — it halts at the next safe point "
           f"(usually a few seconds; not an instant kill)." if ok
           else f"No running job #{id_str}.")
    return _jobs_md(), msg


def _docs_md() -> str:
    store = rag.get_store()
    docs = store.list_documents()
    if not docs:
        return ("_No documents indexed yet._\n\nAdd a file or folder below — I'll "
                "read your own files so I can answer from them.")
    sem = sum(1 for d in docs if d["embedded"])
    head = (f"**{len(docs)} document(s) · {store.chunk_count()} chunks** "
            f"({sem} embedded for semantic search)\n")
    lines = [head]
    for d in docs:
        mode = "semantic" if d["embedded"] else "keyword"
        lines.append(f"- **[{d['id']}]** {d['name']} · {d['chunks']} chunks · {mode}")
    return "\n".join(lines)


def _watched_md() -> str:
    try:
        rows = rag.get_store().list_watched_folders()
    except Exception:
        return "_Watched folders unavailable._"
    if not rows:
        return ("_No auto-watched folders. Add one below and Agent Jo will keep it "
                "indexed automatically — new and changed files are picked up, and "
                "deleted files are removed._")
    lines = [f"**{len(rows)} watched folder(s)** (auto-rescanned every "
             f"{max(1, config.RAG_WATCH_INTERVAL // 60)} min):\n"]
    for w in rows:
        last = ""
        if w["last_scan"]:
            last = f" \u00b7 last scan {w['last_scan'][:16]}"
            if w["last_result"]:
                last += f" ({w['last_result']})"
        lines.append(f"- **[{w['id']}]** {w['path']}{last}")
    return "\n".join(lines)


_watch_running = threading.Event()


def _do_rescan(manual: bool = False) -> dict:
    """Run one rescan of all watched folders, guarded against overlap."""
    if _watch_running.is_set():
        return {"skipped": True}
    _watch_running.set()
    try:
        return rag.get_store().rescan_all_watched()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        _watch_running.clear()


def _watch_loop() -> None:
    """Background poller: re-scan watched folders periodically. Cheap, since
    unchanged files are skipped. Skips entirely when nothing is watched."""
    interval = config.RAG_WATCH_INTERVAL
    if interval <= 0:
        return
    time.sleep(min(20, interval))          # initial scan shortly after launch
    while True:
        try:
            if rag.get_store().list_watched_folders():
                _do_rescan()
        except Exception:
            pass
        time.sleep(interval)


def _start_watcher() -> None:
    if config.RAG_WATCH_INTERVAL > 0:
        threading.Thread(target=_watch_loop, daemon=True).start()


def do_watch_folder(path_str):
    """Index a folder now AND keep it auto-indexed."""
    path_str = (path_str or "").strip().strip('"')
    store = rag.get_store()
    res = store.add_watched_folder(path_str)
    if "error" in res:
        return _docs_md(), _watched_md(), res["error"], _stats_html()
    ing = store.ingest_path(res["path"])           # index immediately
    if "error" in ing:
        msg = f"Now watching {res['path']}, but: {ing['error']}"
    else:
        was = "already watched; " if res.get("already") else ""
        msg = (f"{was}watching {res['path']} \u2014 indexed "
               f"{ing.get('added', 0)} new, {ing.get('updated', 0)} updated "
               f"({ing.get('chunks', 0)} chunks, {ing.get('mode', '')}).")
        if ing.get("note"):
            msg += "  " + ing["note"]
    return _docs_md(), _watched_md(), msg, _stats_html()


def do_unwatch_folder(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _watched_md(), "Enter a numeric folder ID (the number in brackets)."
    ok = rag.get_store().remove_watched_folder(int(id_str))
    return _watched_md(), (f"Stopped watching folder #{id_str} (its indexed "
                           f"documents are kept)." if ok else f"No folder #{id_str}.")


def do_rescan_now():
    res = _do_rescan(manual=True)
    if res.get("skipped"):
        msg = "A rescan is already running; try again in a moment."
    elif res.get("error"):
        msg = f"Rescan failed: {res['error']}"
    elif res.get("folders", 0) == 0:
        msg = "No watched folders to rescan."
    else:
        msg = (f"Rescanned {res['folders']} folder(s): {res['added']} new, "
               f"{res['updated']} updated, {res['pruned']} removed.")
    return _docs_md(), _watched_md(), msg, _stats_html()


def do_index_path(path_str):
    path_str = (path_str or "").strip().strip('"')
    if not path_str:
        return _docs_md(), "Enter a file or folder path to index.", _stats_html()
    try:
        res = rag.get_store().ingest_path(path_str)
    except Exception as exc:
        return _docs_md(), f"Could not index: {type(exc).__name__}: {exc}", _stats_html()
    if "error" in res:
        return _docs_md(), res["error"], _stats_html()
    msg = (f"Indexed: {res['added']} new, {res['updated']} updated, "
           f"{res['skipped']} skipped · {res['chunks']} chunks · mode: {res['mode']}.")
    if res.get("note"):
        msg += "  " + res["note"]
    return _docs_md(), msg, _stats_html()


def do_remove_doc(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _docs_md(), "Enter a numeric document ID (the number in brackets).", _stats_html()
    ok = rag.get_store().remove_document(int(id_str))
    return _docs_md(), (f"Removed document #{id_str}." if ok else f"No document #{id_str}."), _stats_html()


def do_search_docs(query):
    hits = rag.get_store().search(query or "")
    if not hits:
        return "_No matching passages._"
    out = [f"**{len(hits)} passage(s):**\n"]
    for h in hits:
        score = f" · score {h['score']}" if h.get("score") is not None else ""
        snippet = h["text"][:400] + ("…" if len(h["text"]) > 400 else "")
        out.append(f"**[{h['source']}]**{score}\n\n{snippet}")
    return "\n\n---\n\n".join(out)


# --------------------------------------------------------------------- #
# Conversation history
# --------------------------------------------------------------------- #
def _sessions_md() -> str:
    rows = memory.list_sessions(30)
    if not rows:
        return "_No saved conversations yet — chat once and it appears here._"
    lines = [f"**{len(rows)} conversation(s)** — load one by ID below\n"]
    for r in rows:
        lines.append(f"- **[{r['session_id'][:8]}]** {r['title']} \u00b7 "
                     f"{r['n']} messages \u00b7 last {r['last_ts'][:16]}")
    return "\n".join(lines)


def _history_from_transcript(rows) -> list:
    """Stored transcript -> the chat display format in use."""
    msgs = [r for r in rows if r["role"] in ("user", "assistant")]
    if CHAT_FORMAT == "messages":
        return [{"role": r["role"], "content": r["content"]} for r in msgs]
    out, pending = [], None
    for r in msgs:
        if r["role"] == "user":
            if pending is not None:
                out.append([pending, ""])
            pending = r["content"]
        else:
            out.append([pending or "", r["content"]])
            pending = None
    if pending is not None:
        out.append([pending, ""])
    return out


def _rebuild_messages(rows) -> list:
    """Stored transcript -> engine-format history: plain-text messages with
    consecutive same-role entries merged so alternation stays valid."""
    msgs: list = []
    for r in rows:
        if r["role"] not in ("user", "assistant"):
            continue
        if msgs and msgs[-1]["role"] == r["role"]:
            msgs[-1]["content"] += "\n\n" + r["content"]
        else:
            msgs.append({"role": r["role"], "content": r["content"]})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(resumed)"})
    if msgs and msgs[-1]["role"] == "user":
        msgs.append({"role": "assistant", "content": "(resumed)"})
    return msgs


def do_load_session(id_str, history, sess):
    id_str = (id_str or "").strip()
    if len(id_str) < 4:
        return history, sess, "Enter at least the first 4 characters of a conversation ID."
    if sess and sess.get("busy"):
        return history, sess, "Wait for the current reply to finish first."
    matches = memory.find_sessions(id_str)
    if not matches:
        return history, sess, f"No conversation starting with '{id_str}'."
    if len(matches) > 1:
        return history, sess, f"{len(matches)} conversations match — add more characters."
    sid = matches[0]
    rows = memory.get_transcript(sid)
    new_sess = {"id": sid, "messages": _rebuild_messages(rows)}
    title = next((r["content"] for r in rows if r["role"] == "user"), "")[:48]
    return (_history_from_transcript(rows), new_sess,
            f"Loaded '{title}' — open the Chat tab to continue it.")


def do_delete_session(id_str):
    id_str = (id_str or "").strip()
    if len(id_str) < 4:
        return _sessions_md(), "Enter at least the first 4 characters of a conversation ID."
    matches = memory.find_sessions(id_str)
    if not matches:
        return _sessions_md(), f"No conversation starting with '{id_str}'."
    if len(matches) > 1:
        return _sessions_md(), f"{len(matches)} conversations match — add more characters."
    n = memory.delete_session(matches[0])
    return _sessions_md(), f"Deleted conversation ({n} messages)."


# --------------------------------------------------------------------- #
# Scheduled tasks
# --------------------------------------------------------------------- #
_FREQ_TO_KIND = {"Daily": "daily", "Weekdays": "weekdays", "Weekly": "weekly",
                 "Hourly": "hourly", "Every N minutes": "minutes"}
_sched_running: set = set()
_sched_guard = threading.Lock()


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")
    except Exception:
        return "-"


def _schedules_md() -> str:
    rows = memory.list_schedules()
    if not rows:
        return ("_No schedules yet. Create one below — note that schedules only "
                "fire while Agent Jo is running._")
    lines = [f"**{len(rows)} schedule(s)**\n"]
    for sch in rows:
        try:
            when = scheduler.describe_spec(scheduler.parse_spec(sch["spec"]))
        except Exception:
            when = "?"
        state = "on" if sch["enabled"] else "off"
        mode = "full access" if sch["full_access"] else "safe"
        line = (f"- **[{sch['id']}] {sch['name']}** ({state}) \u00b7 {when} \u00b7 "
                f"next {_fmt_ts(sch['next_run']) if sch['enabled'] else '-'} \u00b7 "
                f"engine {sch['engine']} \u00b7 {mode}")
        if sch["last_run"]:
            line += f" \u00b7 last {sch['last_status']} {_fmt_ts(sch['last_run'])}"
        lines.append(line)
        if sch.get("last_summary"):
            lines.append(f"  - {sch['last_summary'][:160]}")
    return "\n".join(lines)


def _run_schedule(sch: dict) -> None:
    """Run one scheduled instruction as a tracked, cancellable job. Its
    transcript gets its own session, so the result shows up in History."""
    sid = sch["id"]
    with _sched_guard:
        if sid in _sched_running:
            return
        _sched_running.add(sid)
    job = _start_job(f"sched:{sch['name']}", sch["prompt"])
    info: dict = {}
    sess_id = f"sched-{sid}-{int(time.time())}"
    status, summary = "ok", ""
    try:
        try:
            memory.log_message(sess_id, "user",
                               f"[scheduled: {sch['name']}] {sch['prompt']}")
        except Exception:
            pass
        reply = main.run_turn(
            brain, memory, [], sch["prompt"],
            auto_approve=bool(sch["full_access"]), session_id=sess_id,
            should_cancel=job["cancel"].is_set,
            force_model=_force_model_for(sch["engine"]), turn_info=info)
        summary = " ".join((reply or "").split())[:300] or "(no output)"
        if reply:
            try:
                memory.log_message(sess_id, "assistant", reply)
            except Exception:
                pass
        _add_usage(info)
        if job["cancel"].is_set():
            status = "cancelled"
    except Exception as exc:
        status, summary = "error", f"{type(exc).__name__}: {exc}"[:300]
    finally:
        try:
            nxt = scheduler.next_run(scheduler.parse_spec(sch["spec"]))
        except Exception:
            nxt = None
        try:
            memory.schedule_ran(sid, time.time(), nxt, status, summary)
        except Exception:
            pass
        _finish_job(job, "done" if status == "ok" else status)
        with _sched_guard:
            _sched_running.discard(sid)


def _scheduler_loop() -> None:
    """Background ticker: every 15s, start any due schedules."""
    while True:
        time.sleep(15)
        try:
            for sch in memory.due_schedules(time.time()):
                threading.Thread(target=_run_schedule, args=(sch,),
                                 daemon=True).start()
        except Exception:
            pass


def _start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def do_create_schedule(name, prompt, freq, time_str, dow_name, n_min, engine, full):
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        return _schedules_md(), "Need both a name and an instruction.", _stats_html()
    try:
        kind = _FREQ_TO_KIND.get(freq, "daily")
        dow = (scheduler.WEEKDAYS.index(dow_name)
               if dow_name in scheduler.WEEKDAYS else 0)
        spec_json = scheduler.make_spec(kind, time_str=(time_str or "07:00"),
                                        n=int((n_min or "60").strip()), dow=dow)
        nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    except (ValueError, TypeError) as exc:
        return _schedules_md(), f"Invalid schedule: {exc}", _stats_html()
    memory.create_schedule(name, prompt, spec_json, engine or "Auto",
                           bool(full), nxt)
    return (_schedules_md(),
            f"Scheduled '{name}' — first run {_fmt_ts(nxt)}. Agent Jo must stay "
            f"running (window open) for schedules to fire.", _stats_html())


def do_toggle_schedule(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _schedules_md(), "Enter a numeric schedule ID."
    sch = memory.get_schedule(int(id_str))
    if not sch:
        return _schedules_md(), f"No schedule #{id_str}."
    if sch["enabled"]:
        memory.set_schedule_enabled(sch["id"], False, None)
        return _schedules_md(), f"Disabled '{sch['name']}'."
    try:
        nxt = scheduler.next_run(scheduler.parse_spec(sch["spec"]))
    except Exception:
        nxt = None
    memory.set_schedule_enabled(sch["id"], True, nxt)
    return _schedules_md(), f"Enabled '{sch['name']}' — next run {_fmt_ts(nxt)}."


def do_delete_schedule(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _schedules_md(), "Enter a numeric schedule ID."
    ok = memory.delete_schedule(int(id_str))
    return _schedules_md(), (f"Deleted schedule #{id_str}." if ok
                             else f"No schedule #{id_str}.")


def do_run_schedule_now(id_str):
    id_str = (id_str or "").strip()
    if not id_str.isdigit():
        return _schedules_md(), "Enter a numeric schedule ID."
    sch = memory.get_schedule(int(id_str))
    if not sch:
        return _schedules_md(), f"No schedule #{id_str}."
    threading.Thread(target=_run_schedule, args=(sch,), daemon=True).start()
    return _schedules_md(), (f"Running '{sch['name']}' now — watch the Jobs tab; "
                             f"the result lands in History.")


def _refresh_all():
    return (_stats_html(), _memories_md(""), _skills_md(),
            _tasks_md("all"), _jobs_md(), _docs_md(),
            _sessions_md(), _schedules_md(), _usage_md(), _routing_md(),
            _watched_md())


# ---------------------------------------------------------------------- #
# Layout
# ---------------------------------------------------------------------- #
SLEEK_CSS = """
/* ===================== Agent Jo - executive BI dashboard ===================== */
.gradio-container {
  --bg:#0b0e15; --bg-elev:#141a24; --bg-elev2:#1a212e; --bg-input:#0e131c;
  --border:#222b3a; --border-soft:#1b2330; --border-strong:#2c3850;
  --text:#e7ebf3; --text-dim:#95a1b5; --text-faint:#697587;
  --accent:#3b97f3; --accent-2:#19c2c9; --accent-deep:#2c79d6;
  --good:#3ad29f; --warn:#f5b13d; --bad:#f87171;
  --radius:10px; --radius-lg:14px;
  --shadow:0 1px 2px rgba(0,0,0,.35), 0 2px 6px rgba(0,0,0,.30);
  --shadow-lg:0 10px 34px rgba(0,0,0,.45);
}
.gradio-container, .gradio-container * {
  font-family:'Segoe UI','Inter',system-ui,-apple-system,Roboto,sans-serif !important;
}
code, pre, pre *, [class*="code"] {
  font-family:'Cascadia Code','JetBrains Mono',ui-monospace,Consolas,Menlo,monospace !important;
}
.gradio-container {
  background:
    radial-gradient(1200px 480px at 88% -10%, rgba(25,194,201,.06), transparent 60%),
    radial-gradient(1000px 460px at 6% -8%, rgba(59,151,243,.07), transparent 60%),
    var(--bg) !important;
  color:var(--text) !important; max-width:1320px !important;
}

/* ---- cards / panels ---- */
[class*="block"], [class*="panel"], [class*="form"] {
  background:var(--bg-elev) !important; border:1px solid var(--border) !important;
  border-radius:var(--radius) !important; box-shadow:var(--shadow) !important;
}
.gradio-container > * { gap:14px !important; }
[class*="tabitem"] { padding-top:14px !important; }
hr { border:none !important; border-top:1px solid var(--border) !important; margin:16px 0 !important; }

/* ---- inputs ---- */
input, textarea, [contenteditable="true"], select {
  background:var(--bg-input) !important; color:var(--text) !important;
  border:1px solid var(--border-strong) !important; border-radius:8px !important;
}
input::placeholder, textarea::placeholder { color:var(--text-faint) !important; }
input:focus, textarea:focus, select:focus {
  border-color:var(--accent) !important;
  box-shadow:0 0 0 3px rgba(59,151,243,.16) !important; outline:none !important;
}

/* ---- buttons ---- */
button {
  border-radius:8px !important; border:1px solid var(--border-strong) !important;
  color:var(--text) !important; background:var(--bg-elev2) !important;
  transition:all .14s ease !important; font-weight:600 !important; font-size:13.5px !important;
}
button:hover { border-color:var(--accent) !important; background:#202a3a !important; }
button[class*="primary"], [class*="primary"] > button {
  background:linear-gradient(180deg,var(--accent),var(--accent-deep)) !important;
  border:1px solid #57a6f6 !important; color:#fff !important;
  box-shadow:0 3px 12px rgba(59,151,243,.30) !important;
}
button[class*="primary"]:hover, [class*="primary"] > button:hover { filter:brightness(1.07) !important; }
button[class*="stop"], [class*="stop"] > button {
  background:rgba(248,113,113,.12) !important; border-color:#7f3a3a !important; color:#fca5a5 !important;
}
button[class*="stop"]:hover, [class*="stop"] > button:hover { background:rgba(248,113,113,.18) !important; }

/* ---- report-page tab ribbon (sticky) ---- */
[class*="tab"] { background:transparent !important; }
[class*="tab-nav"], [class*="tabnav"] {
  position:sticky !important; top:0 !important; z-index:20 !important;
  background:rgba(11,14,21,.86) !important; backdrop-filter:blur(10px) !important;
  border-bottom:1px solid var(--border) !important;
  gap:2px !important; padding:6px 4px 0 !important; overflow-x:auto !important;
}
button[role="tab"], [class*="tab-nav"] button, [class*="tabnav"] button {
  color:var(--text-dim) !important; background:transparent !important; border:none !important;
  border-bottom:2px solid transparent !important; border-radius:7px 7px 0 0 !important;
  font-weight:600 !important; font-size:13.5px !important; padding:9px 15px !important;
  white-space:nowrap !important; transition:all .14s ease !important;
}
button[role="tab"]:hover, [class*="tab-nav"] button:hover, [class*="tabnav"] button:hover {
  color:var(--text) !important; background:rgba(59,151,243,.08) !important;
}
button[role="tab"][aria-selected="true"],
[class*="tab-nav"] button.selected, [class*="tabnav"] button.selected,
button[class*="selected"][role="tab"] {
  color:#fff !important; background:rgba(59,151,243,.14) !important;
  border-bottom:2px solid var(--accent) !important;
}

/* ---- header ribbon ---- */
#hdr {
  display:flex; align-items:center; justify-content:space-between;
  padding:15px 20px; background:linear-gradient(180deg,#151c2a,#0f141d);
  border:1px solid var(--border); border-radius:var(--radius-lg); box-shadow:var(--shadow);
}
#hdr .left { display:flex; align-items:center; gap:13px; }
#hdr .logo {
  width:44px; height:44px; border-radius:12px;
  background:linear-gradient(135deg,var(--accent),var(--accent-2));
  box-shadow:0 5px 18px rgba(59,151,243,.40);
  display:flex; align-items:center; justify-content:center;
}
#hdr img.logo {
  object-fit:cover; border:1px solid var(--border-strong);
}
#hdr .name { font-size:18px; font-weight:700; color:#f2f5fa; letter-spacing:.2px; }
#hdr .sub { font-size:12px; color:var(--text-dim); margin-top:2px; }
#hdr .right { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
#hdr .chip {
  display:inline-flex; align-items:center; gap:7px; font-size:12px; color:var(--text-dim);
  background:var(--bg-input); border:1px solid var(--border); border-radius:999px; padding:6px 12px;
}
#hdr .chip b { color:var(--text); font-weight:600; }
#hdr .pulse { width:8px; height:8px; border-radius:50%; background:var(--good);
  box-shadow:0 0 0 0 rgba(58,210,159,.6); animation:pulse 2.4s infinite; }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(58,210,159,.5);}
  70%{box-shadow:0 0 0 7px rgba(58,210,159,0);} 100%{box-shadow:0 0 0 0 rgba(58,210,159,0);} }

/* ---- KPI tiles (Power BI cards) ---- */
#statusbar { width:100%; }
.kpi-row { display:flex; gap:10px; flex-wrap:nowrap; overflow-x:auto; padding:2px 1px 4px; }
.kpi {
  flex:1 1 0; min-width:104px; background:var(--bg-elev);
  border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:var(--radius); padding:11px 14px; box-shadow:var(--shadow);
  transition:transform .14s ease, box-shadow .14s ease, border-color .14s ease;
}
.kpi:hover { transform:translateY(-2px); box-shadow:var(--shadow-lg); }
.kpi .kpi-val { font-size:21px; font-weight:700; color:#f2f5fa; line-height:1.1; letter-spacing:.2px; }
.kpi .kpi-lbl { font-size:10.5px; font-weight:700; color:var(--text-dim);
  text-transform:uppercase; letter-spacing:.09em; margin-top:5px; }
.kpi.good { border-left-color:var(--good); }
.kpi.off { border-left-color:var(--text-faint); }
.kpi.off .kpi-val { color:var(--text-dim); }

/* ---- chat terminal ---- */
#terminal { background:#0d121b !important; border:1px solid var(--border) !important;
  border-radius:var(--radius-lg) !important; box-shadow:var(--shadow) !important; }
#terminal [class*="message"], #terminal [class*="bubble"] {
  background:var(--bg-elev2) !important; color:var(--text) !important;
  border:1px solid var(--border) !important; border-radius:12px !important; }
#terminal [class*="user"] [class*="bubble"], #terminal [data-testid*="user"] {
  background:linear-gradient(180deg,rgba(59,151,243,.20),rgba(59,151,243,.09)) !important;
  border-color:#2f5fa0 !important; }
#terminal, #terminal * { font-size:14.5px !important; line-height:1.6 !important; }
#terminal pre, #terminal code, #terminal pre code, #terminal pre * { font-size:13px !important; }
#terminal h1,#terminal h2,#terminal h3,#terminal h4 { font-size:15px !important; margin:8px 0 4px !important; }

/* ---- composer ---- */
#composer { align-items:center !important; gap:8px !important; background:#0d121b !important;
  border:1px solid var(--border-strong) !important; border-radius:16px !important;
  padding:6px 6px 6px 8px !important; box-shadow:var(--shadow-lg) !important; }
#composer [class*="block"], #composer [class*="form"] {
  background:transparent !important; border:none !important; box-shadow:none !important; }
#composer textarea, #composer input { background:transparent !important; border:none !important;
  box-shadow:none !important; font-size:15px !important; padding:11px 8px !important; }
#composer textarea:focus, #composer input:focus { box-shadow:none !important; }
#composer button { border-radius:11px !important; min-width:84px !important;
  background:linear-gradient(135deg,var(--accent),var(--accent-2)) !important; border:none !important;
  color:#fff !important; font-weight:700 !important; box-shadow:0 4px 14px rgba(25,194,201,.32) !important; }
#composer button:hover { filter:brightness(1.08) !important; }

/* ---- suggestion cards ---- */
#suggestions { gap:10px !important; flex-wrap:wrap !important; }
#suggestions button { text-align:left !important; white-space:normal !important;
  background:var(--bg-elev) !important; border:1px solid var(--border) !important;
  border-radius:12px !important; padding:14px 16px !important; color:#cdd6e4 !important;
  font-weight:500 !important; line-height:1.4 !important; min-height:62px !important;
  box-shadow:var(--shadow) !important; transition:all .14s ease !important; }
#suggestions button:hover { border-color:var(--accent) !important; background:var(--bg-elev2) !important;
  transform:translateY(-2px) !important; box-shadow:var(--shadow-lg) !important; }

/* ---- typography ---- */
.gradio-container { font-size:14.5px !important; line-height:1.55 !important; }
.gradio-container p { font-size:14.5px !important; line-height:1.65 !important; margin:0 0 10px !important; }
.gradio-container li { font-size:14.5px !important; line-height:1.6 !important; margin:3px 0 !important; }
h1 { font-size:20px !important; font-weight:700 !important; margin:4px 0 12px !important; color:#f2f5fa !important; }
h2 { font-size:16.5px !important; font-weight:700 !important; margin:16px 0 8px !important; color:#eef2f8 !important; }
h3 { font-size:14.5px !important; font-weight:650 !important; margin:14px 0 6px !important; color:#e6edf3 !important; }
h4 { font-size:12px !important; font-weight:700 !important; margin:14px 0 6px !important;
  text-transform:uppercase !important; letter-spacing:.08em !important; color:var(--text-dim) !important; }
strong, b { font-weight:650 !important; color:#f2f5fa !important; }
label, span[class*="block-info"], div[class*="block-info"] {
  font-size:12.5px !important; color:var(--text-dim) !important; font-weight:600 !important; }
input, textarea { font-size:14px !important; padding:10px 12px !important; }
a { color:var(--accent) !important; }

/* ---- markdown data tables -> BI grid ---- */
.gradio-container table { border-collapse:collapse !important; width:100% !important;
  border:1px solid var(--border) !important; border-radius:var(--radius) !important; overflow:hidden !important; }
.gradio-container th { background:var(--bg-elev2) !important; color:var(--text) !important;
  font-weight:700 !important; font-size:12px !important; text-transform:uppercase !important;
  letter-spacing:.05em !important; text-align:left !important; padding:10px 12px !important;
  border-bottom:1px solid var(--border) !important; }
.gradio-container td { padding:9px 12px !important; border-bottom:1px solid var(--border-soft) !important;
  font-size:13.5px !important; color:var(--text) !important; }
.gradio-container tr:hover td { background:rgba(59,151,243,.05) !important; }

/* ---- code blocks ---- */
pre { background:#0d121b !important; border:1px solid var(--border) !important;
  border-radius:var(--radius) !important; padding:12px 14px !important; }
pre, code { font-size:13px !important; line-height:1.55 !important; }

/* ---- selectable controls: clear, persistent selected state ---- */
.gradio-container input[type="radio"], .gradio-container input[type="checkbox"] {
  accent-color:var(--accent) !important; width:16px !important; height:16px !important;
}
.gradio-container label:has(input[type="radio"]),
.gradio-container label:has(input[type="checkbox"]) {
  border:1px solid var(--border-strong) !important; border-radius:8px !important;
  padding:7px 12px !important; background:var(--bg-input) !important;
  color:var(--text-dim) !important; transition:all .14s ease !important; cursor:pointer !important;
}
.gradio-container label:has(input[type="radio"]):hover,
.gradio-container label:has(input[type="checkbox"]):hover {
  border-color:var(--accent) !important; color:var(--text) !important;
}
.gradio-container label:has(input[type="radio"]:checked),
.gradio-container label:has(input[type="checkbox"]:checked),
.gradio-container [role="radio"][aria-checked="true"],
.gradio-container label.selected, .gradio-container label[class*="selected"] {
  background:rgba(59,151,243,.16) !important; border-color:var(--accent) !important;
  color:#fff !important; box-shadow:inset 0 0 0 1px var(--accent) !important; font-weight:600 !important;
}

/* ---- scrollbars ---- */
.gradio-container ::-webkit-scrollbar { width:10px; height:10px; }
.gradio-container ::-webkit-scrollbar-track { background:transparent; }
.gradio-container ::-webkit-scrollbar-thumb { background:#2a3446; border-radius:8px; border:2px solid var(--bg); }
.gradio-container ::-webkit-scrollbar-thumb:hover { background:#374459; }
"""

_logo = config.AGENT_NAME[0].upper() if config.AGENT_NAME else "A"

# Agent picture: a PNG shipped next to app.py. The header embeds it as a data
# URI (self-contained, no file serving); the chat uses the file path directly.
_AVATAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "agent_avatar.png")


def _avatar_uri() -> str:
    try:
        with open(_AVATAR_PATH, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""


_AVATAR_URI = _avatar_uri()
_HAS_AVATAR = bool(_AVATAR_URI)


def _logo_markup() -> str:
    """The agent picture for the header (falls back to the sparkle mark)."""
    if _HAS_AVATAR:
        return (f'<img class="logo" src="{_AVATAR_URI}" '
                f'alt="{config.AGENT_NAME}"/>')
    return (
        '<div class="logo">'
        '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">'
        '<path fill="#ffffff" d="M12 2.5l1.6 4.3a4 4 0 0 0 2.4 2.4l4.3 1.6-4.3 '
        '1.6a4 4 0 0 0-2.4 2.4L12 19.5l-1.6-4.3a4 4 0 0 0-2.4-2.4L3.7 11.2 8 '
        '9.6a4 4 0 0 0 2.4-2.4L12 2.5z"/>'
        '<circle cx="18.5" cy="5.5" r="1.6" fill="#ffffff" opacity="0.9"/>'
        '</svg></div>')


def _engine_display(choice: str = "Auto") -> str:
    """Friendly model string for the header chip, given the Engine selection."""
    if choice == "Claude":
        return f"anthropic:{config.MODEL}"
    if choice == "Ollama":
        return f"ollama:{config.OLLAMA_MODEL}"
    if choice == config.OLLAMA_MODEL_2:
        return f"ollama:{config.OLLAMA_MODEL_2}"
    if choice == "DeepSeek Pro":
        return f"deepseek:{config.DEEPSEEK_MODEL_PRO}"
    if choice == "DeepSeek Flash":
        return f"deepseek:{config.DEEPSEEK_MODEL_FLASH}"
    _e = brainmod.custom_engine_by_name(choice)
    if _e is not None:
        return f"{choice} &middot; {_e['model']}"
    try:                                  # Auto -> the routing configuration
        return "auto &middot; " + brain.describe()
    except Exception:
        return f"auto &middot; {config.BACKEND}"


def _header_html(engine_choice: str = "Auto") -> str:
    return f"""
<div id="hdr">
  <div class="left">
    {_logo_markup()}
    <div>
      <div class="name">{config.AGENT_NAME}</div>
      <div class="sub">Executive workspace &middot; persistent memory</div>
    </div>
  </div>
  <div class="right">
    <span class="chip">engine&nbsp;<b>{_engine_display(engine_choice)}</b></span>
    <span class="chip"><span class="pulse"></span>&nbsp;online</span>
  </div>
</div>
"""


try:
    THEME = gr.themes.Base(
        primary_hue="blue", secondary_hue="cyan", neutral_hue="slate",
        font=[gr.themes.GoogleFont("Inter"), "Segoe UI", "system-ui", "sans-serif"],
        font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "Consolas", "monospace"])
except Exception:
    THEME = None

# Gradio 6 moved theme/css to launch(); older versions take them on Blocks.
# Pass them here too (for older Gradio) but suppress the 6.x "moved" warning;
# they're also passed to launch() below, which is what current Gradio uses.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    demo = gr.Blocks(theme=THEME, css=SLEEK_CSS,
                     title=f"{config.AGENT_NAME} — local agent",
                     fill_height=True)

with demo:
    state = gr.State(_new_session())

    header = gr.HTML(_header_html("Auto"), elem_id="hdrwrap")
    stats = gr.HTML(_stats_html(), elem_id="statusbar")

    with gr.Tabs():
        # ---------------- Chat ----------------
        with gr.Tab("Chat"):
            chatbot, CHAT_FORMAT = _build_chatbot()
            with gr.Row(elem_id="composer"):
                box = gr.Textbox(placeholder="Message Agent Jo\u2026  (Enter to send)",
                                 scale=8, show_label=False, autofocus=True)
                send = gr.Button("Send", scale=1, variant="primary")
            with gr.Row():
                full = gr.Checkbox(
                    label="Full access (write files and run commands)",
                    value=False)
                web = gr.Checkbox(
                    label="Web access (search and read pages — queries leave "
                          "this machine)",
                    value=tools.WEB_ENABLED)
                clear = gr.Button("New conversation", scale=0)
                retry_btn = gr.Button("Retry on Claude", scale=0)
            engine = gr.Radio(
                _engine_choices(), value="Auto",
                label="Engine",
                info=f"Auto = smart routing (saves tokens) · Claude = always cloud "
                     f"· Ollama = local {config.OLLAMA_MODEL} "
                     f"· {config.OLLAMA_MODEL_2} = local {config.OLLAMA_MODEL_2} "
                     f"(must be pulled in Ollama)")
            engine.change(_header_html, inputs=[engine], outputs=[header])
            note = gr.Markdown(SAFE_NOTE)
            with gr.Accordion("Voice (local, private)", open=False):
                gr.Markdown(
                    "Speak to type, and optionally have replies read aloud. "
                    "Both run locally: text-to-speech uses your computer's own "
                    "voice, and speech-to-text (needs `pip install faster-whisper`) "
                    "transcribes on this machine. Nothing audio leaves your computer.")
                mic = _build_mic()
                with gr.Row():
                    speak_replies = gr.Checkbox(label="Speak replies aloud",
                                                value=_speak_enabled)
                    stop_speak_btn = gr.Button("Stop speaking", scale=0)
            with gr.Accordion("Image (vision)", open=False):
                gr.Markdown("Attach an image and ask about it. Under Auto, images "
                            "go to Claude; you can also pick Claude or "
                            + config.OLLAMA_MODEL_2 + " (a local vision model) in "
                            "the Engine selector. The image is sent only for that "
                            "message.")
                image_in = _build_image_input()
            with gr.Accordion("Files (PDF, Word, Excel, text, code)", open=False):
                gr.Markdown("Attach documents and ask about them. Their text is "
                            "read on this machine and given to the model, so this "
                            "works on Claude or a local model. Very large files "
                            "are trimmed; for big documents Claude has the most "
                            "room. Files are used only for that message.")
                file_in = _build_file_input()
            gr.Markdown("#### Suggested")
            with gr.Row(elem_id="suggestions"):
                starter_btns = [gr.Button(s, size="sm") for s in STARTERS]

        # ---------------- History ----------------
        with gr.Tab("History"):
            gr.Markdown("Past conversations, saved automatically (web, terminal, "
                        "and scheduled runs). Load one to continue it in the Chat "
                        "tab — new messages extend the same conversation.")
            hist_refresh = gr.Button("Refresh", size="sm")
            hist_view = gr.Markdown(_sessions_md())
            with gr.Row():
                hist_id = gr.Textbox(
                    placeholder="Conversation ID (the bracketed code above)",
                    scale=4, show_label=False)
                hist_load = gr.Button("Load", scale=1, variant="primary")
                hist_del = gr.Button("Delete", scale=1, variant="stop")
            hist_status = gr.Markdown("")

        # ---------------- Memory ----------------
        with gr.Tab("Memory"):
            gr.Markdown("Everything I've learned about you. I update this as we "
                        "talk; you're in control of all of it.")
            with gr.Row():
                mem_search = gr.Textbox(placeholder="Search memories…",
                                        scale=6, show_label=False)
                mem_search_btn = gr.Button("Search", scale=1)
                mem_all_btn = gr.Button("Show all", scale=1)
            mem_view = gr.Markdown(_memories_md(""))
            with gr.Row():
                del_id = gr.Textbox(placeholder="ID to delete (e.g. 3)",
                                    scale=3, show_label=False)
                del_btn = gr.Button("Delete", scale=1, variant="stop")
            del_status = gr.Markdown("")
            gr.Markdown("---")
            gr.Markdown("**Tidy up** — find near-duplicate memories (reworded "
                        "facts that slipped past the exact-duplicate guard), "
                        "review them, then remove the extras.")
            with gr.Row():
                dedup_find_btn = gr.Button("Review duplicates", scale=1)
                dedup_apply_btn = gr.Button("Nothing to remove", scale=1,
                                            variant="stop", interactive=False)
            dedup_state = gr.State([])
            dedup_preview = gr.Markdown("")

        # ---------------- Skills ----------------
        with gr.Tab("Skills"):
            gr.Markdown("Routines I'll follow whenever they apply — in this "
                        "session and every future one.")
            skills_view = gr.Markdown(_skills_md())
            gr.Markdown("#### Teach a new skill")
            sk_name = gr.Textbox(label="Name", placeholder="e.g. daily-briefing")
            sk_when = gr.Textbox(label="When should I use it?",
                                 placeholder="e.g. when I ask for my morning briefing")
            sk_what = gr.Textbox(label="What should I do?", lines=4,
                                 placeholder="e.g. read ~/notes/today.md and summarise "
                                             "the top 5 priorities in under 150 words")
            teach_btn = gr.Button("Teach skill", variant="primary")
            teach_status = gr.Markdown("")

        # ---------------- Documents ----------------
        with gr.Tab("Documents"):
            gr.Markdown("Index your own files so I can answer from them. Embeddings "
                        "run locally on Ollama, so your documents never leave this "
                        "machine. Supported: text and code files, plus PDF/DOCX.")
            with gr.Row():
                doc_path = gr.Textbox(
                    placeholder="Path to a file or folder, e.g. C:\\Users\\you\\Documents\\notes",
                    scale=6, show_label=False)
                doc_index_btn = gr.Button("Index", scale=1, variant="primary")
                doc_watch_btn = gr.Button("Watch folder", scale=1)
            doc_status = gr.Markdown("")
            gr.Markdown("#### Auto-watched folders")
            gr.Markdown("Folders here stay indexed automatically: new and changed "
                        "files are picked up and deleted files are removed, on a "
                        "timer and at startup. Unwatching keeps already-indexed files.")
            watched_view = gr.Markdown(_watched_md())
            with gr.Row():
                watch_unwatch_id = gr.Textbox(
                    placeholder="Folder ID to stop watching (e.g. 1)",
                    scale=3, show_label=False)
                watch_unwatch_btn = gr.Button("Stop watching", scale=1, variant="stop")
                watch_rescan_btn = gr.Button("Rescan now", scale=1)
            watch_status = gr.Markdown("")
            docs_view = gr.Markdown(_docs_md())
            with gr.Row():
                doc_del_id = gr.Textbox(placeholder="Document ID to remove (e.g. 2)",
                                        scale=3, show_label=False)
                doc_del_btn = gr.Button("Remove", scale=1, variant="stop")
            gr.Markdown("#### Test retrieval")
            with gr.Row():
                doc_query = gr.Textbox(placeholder="Search your documents…",
                                       scale=6, show_label=False)
                doc_search_btn = gr.Button("Search", scale=1)
            doc_results = gr.Markdown("")

        # ---------------- Tasks ----------------
        with gr.Tab("Tasks"):
            gr.Markdown("Plans for multi-step jobs. I check each step before "
                        "marking it done, and I can resume these across restarts.")
            task_refresh = gr.Button("Refresh", size="sm")
            tasks_view = gr.Markdown(_tasks_md("all"))

        # ---------------- Jobs ----------------
        with gr.Tab("Jobs"):
            gr.Markdown("What I'm working on across all your tabs, plus recent "
                        "finished jobs. Cancelling asks a job to stop at its next "
                        "safe point (usually a few seconds — not an instant kill).")
            jobs_refresh = gr.Button("Refresh", size="sm")
            jobs_view = gr.Markdown(_jobs_md())
            with gr.Row():
                cancel_id = gr.Textbox(placeholder="Job ID to cancel (e.g. 2)",
                                       scale=3, show_label=False)
                cancel_btn = gr.Button("Cancel job", scale=1, variant="stop")
            cancel_status = gr.Markdown("")
            gr.Markdown("#### Engine usage")
            usage_view = gr.Markdown(_usage_md())
            gr.Markdown("#### Routing feedback")
            routing_view = gr.Markdown(_routing_md())
            with gr.Row():
                routing_refresh = gr.Button("Refresh", size="sm")
                routing_clear = gr.Button("Clear learned escalations", size="sm",
                                          variant="stop")
            routing_status = gr.Markdown("")

        # ---------------- Schedules ----------------
        with gr.Tab("Schedules"):
            gr.Markdown("Standing instructions I run on my own — a morning "
                        "briefing, a weekly summary. Schedules fire only while "
                        "Agent Jo is running; one missed while it was closed runs "
                        "once at the next launch. Results land in History, and "
                        "live runs appear in Jobs.")
            sched_refresh = gr.Button("Refresh", size="sm")
            sched_view = gr.Markdown(_schedules_md())
            gr.Markdown("#### New schedule")
            sch_name = gr.Textbox(label="Name", placeholder="e.g. morning-briefing")
            sch_prompt = gr.Textbox(label="Instruction", lines=3,
                                    placeholder="e.g. Search my documents for open "
                                                "action items and write a 5-point briefing.")
            with gr.Row():
                sch_freq = gr.Dropdown(
                    ["Daily", "Weekdays", "Weekly", "Hourly", "Every N minutes"],
                    value="Daily", label="Frequency")
                sch_time = gr.Textbox(label="Time HH:MM (daily / weekdays / weekly)",
                                      value="07:30")
                sch_dow = gr.Dropdown(scheduler.WEEKDAYS, value="Monday",
                                      label="Weekday (weekly only)")
                sch_n = gr.Textbox(label="Every N minutes (that frequency only)",
                                   value="60")
            with gr.Row():
                sch_engine = gr.Radio(_engine_choices(), value="Auto",
                                      label="Engine for the run")
                sch_full = gr.Checkbox(
                    label="Full access (scheduled runs may write files and run "
                          "commands unattended — use with care)", value=False)
            sch_create = gr.Button("Create schedule", variant="primary")
            sched_status = gr.Markdown("")
            with gr.Row():
                sch_id = gr.Textbox(placeholder="Schedule ID (e.g. 1)",
                                    scale=3, show_label=False)
                sch_run = gr.Button("Run now", scale=1)
                sch_toggle = gr.Button("Enable / disable", scale=1)
                sch_del = gr.Button("Delete", scale=1, variant="stop")

        # ---------------- Backup ----------------
        with gr.Tab("Backup"):
            gr.Markdown("Save a complete backup of everything Agent Jo knows, or "
                        "restore one. Backups stay on this machine.")
            gr.Markdown("### Back up")
            gr.Markdown("One zip with your memories, skills, tasks, schedules, "
                        "conversation history, and document index — plus a "
                        "readable `export.json` copy of the text.")
            backup_btn = gr.Button("Create backup", variant="primary")
            backup_status = gr.Markdown("")
            backup_file = _build_download()
            gr.Markdown("---")
            gr.Markdown("### Restore")
            gr.Markdown("Replace everything with the contents of a backup zip. "
                        "This **overwrites** current memories and documents, so "
                        "back up first if unsure, and do it when you're not "
                        "mid-conversation.")
            restore_file = gr.File(label="Backup .zip to restore",
                                   file_count="single", type="filepath")
            restore_confirm = gr.Checkbox(
                label="I understand this replaces all current data", value=False)
            restore_btn = gr.Button("Restore from backup", variant="stop")
            restore_status = gr.Markdown("")

        # ---------------- Settings ----------------
        with gr.Tab("Settings"):
            gr.Markdown("Adjust how Agent Jo behaves. Changes take effect on your "
                        "next message and are saved across restarts.")
            gr.Markdown("### Behaviour")
            set_autolearn = gr.Checkbox(
                label="Learn facts after each reply (background, uses the local "
                      "model) — turn off if the local model is slow",
                value=config.AUTO_LEARN)
            set_routing = gr.Checkbox(
                label="Auto mode: triage with the local model and send simple "
                      "turns to it (off = always use the cloud model in Auto)",
                value=config.ROUTING)
            set_cache = gr.Checkbox(
                label="Prompt caching on the Claude backend (cuts repeated-context cost)",
                value=config.PROMPT_CACHE)
            set_subagents = gr.Checkbox(
                label="Allow delegating focused sub-tasks to isolated worker agents",
                value=config.SUBAGENTS)
            gr.Markdown("### Limits")
            with gr.Row():
                set_timeout = gr.Number(
                    label="Turn timeout (seconds, 0 = no limit)",
                    value=config.TURN_TIMEOUT, precision=0)
                set_rounds = gr.Number(
                    label="Max tool calls per turn",
                    value=config.MAX_TOOL_ROUNDS, precision=0)
                set_tokens = gr.Number(
                    label="Max reply length (tokens)",
                    value=config.MAX_TOKENS, precision=0)
            with gr.Row():
                set_mems = gr.Number(
                    label="Remembered facts per prompt",
                    value=config.MAX_MEMORIES_IN_CONTEXT, precision=0)
                set_hist = gr.Number(
                    label="Recent turns kept",
                    value=config.MAX_HISTORY_TURNS, precision=0)
                set_ctrigger = gr.Number(
                    label="Summarise after N turns",
                    value=config.COMPACT_TRIGGER_TURNS, precision=0)
                set_ckeep = gr.Number(
                    label="Turns kept verbatim when summarising",
                    value=config.COMPACT_KEEP_TURNS, precision=0)
            with gr.Row():
                settings_apply = gr.Button("Apply settings", variant="primary")
                settings_reset = gr.Button("Reset to defaults")
            settings_status = gr.Markdown("")
            def _ds_line(name, model, keyfn):
                key, url = config.deepseek_creds(model)
                if not keyfn():
                    return f"- {name}: not configured\n"
                return f"- {name}: `{model}` via `{url}`\n"
            if config.deepseek_pro_key() or config.deepseek_flash_key():
                _deepseek_line = (
                    _ds_line("DeepSeek Pro", config.DEEPSEEK_MODEL_PRO,
                             config.deepseek_pro_key)
                    + _ds_line("DeepSeek Flash", config.DEEPSEEK_MODEL_FLASH,
                               config.deepseek_flash_key))
            else:
                _deepseek_line = (
                    "- DeepSeek: not configured (set `AGENT_DEEPSEEK_KEY`, or "
                    "`AGENT_DEEPSEEK_KEY_PRO` / `AGENT_DEEPSEEK_KEY_FLASH` for "
                    "separate keys)\n")
            gr.Markdown(
                "---\n**Engine** (set via environment variables; restart to change)\n\n"
                f"- Backend: `{config.BACKEND}`\n"
                f"- Cloud model: `{config.MODEL}` · fast: `{config.FAST_MODEL}`\n"
                f"- Local model: `{config.OLLAMA_MODEL}` · second: `{config.OLLAMA_MODEL_2}`\n"
                f"- Ollama host: `{config.OLLAMA_HOST}`\n"
                + _deepseek_line +
                f"- Data: `{config.AGENT_HOME}`\n\n"
                "Web access and spoken replies are toggled on the Chat tab.")

        # ---------------- Capabilities ----------------
        with gr.Tab("Engines"):
            gr.Markdown(
                "## Engines\n"
                "Add any **OpenAI-compatible** API as a new engine - OpenAI, "
                "Together, Groq, Mistral, OpenRouter, a local vLLM/LM Studio "
                "server, another DeepSeek account, and so on. It shows up in the "
                "Engine selector on the Chat and Schedules tabs straight away. "
                "Claude, Ollama and the built-in DeepSeek engines are untouched.")
            eng_table = gr.Markdown(_engines_table_md())
            eng_editing = gr.State("")        # name currently loaded for editing
            gr.Markdown("### Add or edit an engine")
            with gr.Row():
                eng_name = gr.Textbox(label="Name", placeholder="e.g. GPT-4o (work)")
                eng_model = gr.Textbox(label="Model id",
                                       placeholder="e.g. gpt-4o-mini")
            eng_url = gr.Textbox(label="Base URL",
                                 placeholder="https://api.openai.com/v1")
            eng_key = gr.Textbox(
                label="API key", type="password",
                placeholder="leave blank for keyless local endpoints")
            with gr.Row():
                eng_tools = gr.Checkbox(
                    value=True, label="Supports tools (function-calling)",
                    info="Turn off for endpoints that reject the tools parameter; "
                         "the engine then answers without using tools.")
                eng_stream = gr.Checkbox(
                    value=True, label="Supports streaming",
                    info="Turn off for endpoints that don't stream; replies arrive "
                         "all at once instead of token-by-token.")
            with gr.Row():
                eng_add_btn = gr.Button("Save engine", variant="primary")
                eng_test_btn = gr.Button("Test connection")
                eng_clear_btn = gr.Button("Clear form")
            eng_status = gr.Markdown("")
            gr.Markdown("---\n### Edit or remove a saved engine")
            _names = brainmod.custom_engine_names()
            with gr.Row():
                eng_remove_dd = gr.Dropdown(
                    _names, label="Your engines",
                    value=(_names[0] if _names else None))
                eng_load_btn = gr.Button("Load into form")
                eng_remove_btn = gr.Button("Remove", variant="stop")
            gr.Markdown(
                "_Pick an engine and **Load into form** to edit it, then **Save "
                "engine**. Keeping the name updates it in place; changing the name "
                "renames it. Engines are saved locally in `engines.json` in your "
                "data folder - API keys are stored there in plain text, so protect "
                "that folder like any credential. Changes apply immediately._")

            _add_outputs = [eng_status, eng_table, engine, sch_engine,
                            eng_remove_dd, eng_name, eng_url, eng_model, eng_key,
                            eng_tools, eng_stream, eng_editing]
            eng_add_btn.click(
                do_add_engine,
                [eng_name, eng_url, eng_model, eng_key, eng_tools, eng_stream,
                 eng_editing], _add_outputs)
            eng_test_btn.click(
                do_test_engine, [eng_name, eng_url, eng_model, eng_key],
                [eng_status])
            eng_load_btn.click(
                do_load_engine, [eng_remove_dd],
                [eng_name, eng_url, eng_model, eng_key, eng_tools, eng_stream,
                 eng_editing, eng_status])
            eng_clear_btn.click(
                do_clear_engine_form, None,
                [eng_name, eng_url, eng_model, eng_key, eng_tools, eng_stream,
                 eng_editing, eng_status])
            eng_remove_btn.click(
                do_remove_engine, [eng_remove_dd],
                [eng_status, eng_table, engine, sch_engine, eng_remove_dd])

        with gr.Tab("Capabilities"):
            gr.Markdown(CAPABILITIES)

    # ---- wiring ----
    chat_out = [chatbot, state, box]
    base_in = [box, chatbot, full, engine, state]

    def _respond_files_only(message, history, full_access, engine_choice, sess, files):
        yield from respond(message, history, full_access, engine_choice, sess,
                           None, files)

    if image_in is not None and file_in is not None:
        _in = base_in + [image_in, file_in]

        def _clear_attach():
            return gr.update(value=None), gr.update(value=None)
        box.submit(respond, _in, chat_out).then(_clear_attach, None, [image_in, file_in])
        send.click(respond, _in, chat_out).then(_clear_attach, None, [image_in, file_in])
    elif image_in is not None:
        _in = base_in + [image_in]
        box.submit(respond, _in, chat_out).then(lambda: gr.update(value=None), None, image_in)
        send.click(respond, _in, chat_out).then(lambda: gr.update(value=None), None, image_in)
    elif file_in is not None:
        _in = base_in + [file_in]
        box.submit(_respond_files_only, _in, chat_out).then(
            lambda: gr.update(value=None), None, file_in)
        send.click(_respond_files_only, _in, chat_out).then(
            lambda: gr.update(value=None), None, file_in)
    else:
        box.submit(respond, base_in, chat_out)
        send.click(respond, base_in, chat_out)
    clear.click(clear_conversation, [state], [chatbot, state])
    retry_btn.click(retry_on_claude, [chatbot, full, state], chat_out)
    full.change(toggle_note, [full], [note])

    def _set_web(enabled):
        tools.WEB_ENABLED = bool(enabled)
    web.change(_set_web, web, None)

    speak_replies.change(_set_speak, speak_replies, None)
    stop_speak_btn.click(do_stop_speaking, None, None)
    if mic is not None:
        try:
            mic.stop_recording(do_transcribe, mic, box)
        except Exception:
            mic.change(do_transcribe, mic, box)
    for b, text in zip(starter_btns, STARTERS):
        b.click(lambda t=text: t, None, box)

    mem_search.submit(do_search_memory, mem_search, mem_view)
    mem_search_btn.click(do_search_memory, mem_search, mem_view)
    mem_all_btn.click(do_show_all_memory, None, [mem_search, mem_view])
    del_btn.click(do_delete_memory, [del_id, mem_search], [mem_view, del_status, stats])
    dedup_find_btn.click(do_find_duplicates, None,
                         [dedup_preview, dedup_state, dedup_apply_btn])
    dedup_apply_btn.click(do_apply_dedup, [dedup_state, mem_search],
                          [mem_view, del_status, stats, dedup_preview,
                           dedup_state, dedup_apply_btn])

    if backup_file is not None:
        backup_btn.click(do_create_backup, None, [backup_status, backup_file])
    else:
        backup_btn.click(lambda: do_create_backup()[0], None, backup_status)
    restore_btn.click(do_restore_backup, [restore_file, restore_confirm],
                      [restore_status, mem_view, docs_view, stats])

    _settings_controls = [set_autolearn, set_routing, set_cache, set_subagents,
                          set_timeout, set_rounds, set_tokens, set_mems,
                          set_hist, set_ctrigger, set_ckeep]
    settings_apply.click(do_apply_settings, _settings_controls, settings_status)
    settings_reset.click(do_reset_settings, None,
                         _settings_controls + [settings_status])

    teach_btn.click(do_teach_skill, [sk_name, sk_when, sk_what],
                    [skills_view, teach_status, stats])

    task_refresh.click(lambda: _tasks_md("all"), None, tasks_view)

    # documents
    doc_index_btn.click(do_index_path, doc_path, [docs_view, doc_status, stats])
    doc_path.submit(do_index_path, doc_path, [docs_view, doc_status, stats])
    doc_del_btn.click(do_remove_doc, doc_del_id, [docs_view, doc_status, stats])
    doc_search_btn.click(do_search_docs, doc_query, doc_results)
    doc_query.submit(do_search_docs, doc_query, doc_results)
    doc_watch_btn.click(do_watch_folder, doc_path,
                        [docs_view, watched_view, doc_status, stats])
    watch_rescan_btn.click(do_rescan_now, None,
                           [docs_view, watched_view, watch_status, stats])
    watch_unwatch_btn.click(do_unwatch_folder, watch_unwatch_id,
                            [watched_view, watch_status])

    jobs_refresh.click(lambda: (_jobs_md(), _usage_md(), _routing_md()), None,
                       [jobs_view, usage_view, routing_view])
    routing_refresh.click(_routing_md, None, routing_view)
    routing_clear.click(do_clear_routing, None, [routing_view, routing_status])
    cancel_btn.click(do_cancel_job, cancel_id, [jobs_view, cancel_status])

    # history
    hist_refresh.click(lambda: _sessions_md(), None, hist_view)
    hist_load.click(do_load_session, [hist_id, chatbot, state],
                    [chatbot, state, hist_status])
    hist_del.click(do_delete_session, hist_id, [hist_view, hist_status])

    # schedules
    sched_refresh.click(lambda: _schedules_md(), None, sched_view)
    sch_create.click(do_create_schedule,
                     [sch_name, sch_prompt, sch_freq, sch_time, sch_dow,
                      sch_n, sch_engine, sch_full],
                     [sched_view, sched_status, stats])
    sch_run.click(do_run_schedule_now, sch_id, [sched_view, sched_status])
    sch_toggle.click(do_toggle_schedule, sch_id, [sched_view, sched_status])
    sch_del.click(do_delete_schedule, sch_id, [sched_view, sched_status])

    # Live auto-refresh of the Jobs view + stats bar (if this Gradio has Timer).
    try:
        _job_timer = gr.Timer(2.0)
        _job_timer.tick(lambda: (_jobs_md(), _stats_html(), _usage_md()),
                        None, [jobs_view, stats, usage_view])
    except Exception:
        pass  # older Gradio without gr.Timer — the Refresh button still works

    # Refresh stats and all tabs whenever the page loads.
    demo.load(_refresh_all, None,
              [stats, mem_view, skills_view, tasks_view, jobs_view,
               docs_view, hist_view, sched_view, usage_view, routing_view,
               watched_view])


if __name__ == "__main__":
    _start_scheduler()       # background ticker for the Schedules tab
    _start_watcher()         # background re-indexer for auto-watched folders

    # Let several conversations (separate browser tabs) run at the same time.
    try:
        demo.queue(default_concurrency_limit=8)
    except TypeError:
        try:
            demo.queue(concurrency_count=8)   # older Gradio parameter name
        except TypeError:
            demo.queue()

    # 7860 is often still held by a previous, still-running instance — pick the
    # first free port instead of crashing.
    port = 7860
    for _p in range(7860, 7881):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            try:
                _s.bind(("127.0.0.1", _p))
                port = _p
                break
            except OSError:
                continue

    # Current Gradio (6.x) takes theme/css on launch(); older versions took them
    # on Blocks (passed there too). Try the full call, then fall back.
    base = dict(server_name="127.0.0.1", server_port=port, inbrowser=True,
                allowed_paths=[os.path.dirname(os.path.abspath(__file__))])
    try:
        demo.launch(theme=THEME, css=SLEEK_CSS, **base)
    except TypeError:
        try:
            demo.launch(**base)
        except TypeError:
            base.pop("allowed_paths", None)   # very old Gradio
            demo.launch(**base)
