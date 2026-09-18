"""Agent Jo - production web API (FastAPI).

Wraps the existing agent engine (the `agent/` package) with a REST + Server-Sent
Events interface so a custom web frontend can replace the Gradio UI. The engine,
persistent memory, tools, and multi-engine routing are all reused unchanged.

Run:  python run_web.py      (or: uvicorn web.server:app --host 0.0.0.0 --port 8000)

This is a foundation, not a finished SaaS. Auth, billing, per-user data
isolation, and horizontal scaling are deliberately NOT included yet - see
README "Production web app" for the layering plan. In particular, conversation
history is held in this process's memory and keyed by id, which is fine for a
single-tenant deployment but must move to a shared store before running multiple
workers or serving multiple customers.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent.config as config
import agent.main as main
import agent.brain as brainmod
import agent.rag as rag
import agent.files as agent_files
import agent.tools as agent_tools
import agent.scheduler as scheduler
import agent.voice as voice
import agent.outreach as outreach
import agent.watchers as watchers
import agent.autoresume as autoresume
import agent.folderwatch as folderwatch
import agent.audit as audit
import agent.timemachine as timemachine
import agent.selfimprove as selfimprove
import agent.datapipeline as datapipeline
import agent.blenderlab as blenderlab
import agent.neural3d as neural3d
import agent.trendscout as trendscout
import agent.crew as crew
import agent.backup as backupmod
import agent.jobscout as jobscout
import agent.portal as portal
import agent.cv as cvmod
import agent.boards as boards
import agent.jobalerts as jobalerts
import agent.models as localmodels
import agent.routing as routing
import agent.intercepts as intercepts
import agent.mcpdiscover as mcpdiscover
import agent.codemap as codemap
import agent.modelbuild as modelbuild
import agent.dataprep as dataprep
import agent.outcomes as outcomes
import agent.finetune as finetune
import agent.health as health
import agent.breaker as breaker
import agent.costs as costs
import agent.evals as evals
import agent.capabilities as capabilities
import agent.dashboard as dashboard
import agent.setup as setupmod
import agent.engines as engines
import agent.turbo as turbo
import agent.skills as skillsmod
import agent.tour as tourmod
import agent.presenter as presenter

# The agent's tool layer writes progress through a rich Console. In the CLI
# that's the terminal; in the web process it goes to the server log, which is
# where the launcher window shows it. Every crew call site referenced a
# `console` that was never defined here, so Crew 500'd with a NameError the
# moment it was used from a panel — and the tests never saw it because they
# call crew.run() directly with their own Console.
from rich.console import Console as _RichConsole
console = _RichConsole()
import agent.challenges as challenges
import agent.issues as issues
import web.auth as auth
from web.ratelimit import RateLimiter
from agent.brain import make_brain, EngineNotConfigured
from agent.memory import MemoryStore

# Path resolution works both as plain Python and when frozen into a single .exe
# by PyInstaller (which unpacks bundled data under sys._MEIPASS).
if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    APP_DIR = _BASE / "web"
    STATIC = APP_DIR / "static"
    PROJECT_ROOT = _BASE
else:
    APP_DIR = Path(__file__).resolve().parent
    STATIC = APP_DIR / "static"
    PROJECT_ROOT = APP_DIR.parent

app = FastAPI(title=f"{config.AGENT_NAME} API", version="1.0.0")

# Same-origin in the bundled setup; permissive so a separately-hosted frontend
# (e.g. a Vite dev server) can also talk to it during development.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

# Paths reachable without a session: the SPA shell + its assets, health, and the
# auth handshake itself. Everything else requires a valid cookie once a password
# has been set. When no password is set, the guard is a no-op (localhost mode).
_AUTH_PUBLIC = {"/", "/login", "/avatar", "/favicon.ico", "/api/health",
                "/api/meta", "/api/auth/login", "/api/auth/logout",
                "/api/auth/status"}


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    if request.method == "OPTIONS" or not auth.is_enabled():
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_PUBLIC or path.startswith("/static"):
        return await call_next(request)
    if auth.verify_token(request.cookies.get(auth.COOKIE, "")):
        return await call_next(request)
    return JSONResponse({"detail": "Authentication required."}, status_code=401)


# Rate limiting: throttle password guesses and runaway chat usage. On by default
# with generous limits; tune via env or set AGENT_RATE_LIMIT=off to disable.
_RATE_ON = os.environ.get("AGENT_RATE_LIMIT", "on").strip().lower() not in ("off", "0", "false")
_login_rl = RateLimiter(int(os.environ.get("AGENT_LOGIN_RATE", "10")), 300)   # 10 / 5 min
_chat_rl = RateLimiter(int(os.environ.get("AGENT_CHAT_RATE", "40")), 60)      # 40 / min


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:                                       # behind a reverse proxy
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "local"


@app.middleware("http")
async def _rate_guard(request: Request, call_next):
    if _RATE_ON and request.method == "POST":
        limiter = None
        path = request.url.path
        if path == "/api/auth/login":
            limiter = _login_rl
        elif path == "/api/chat":
            limiter = _chat_rl
        if limiter is not None:
            ok, retry = limiter.hit(_client_key(request))
            if not ok:
                wait = int(retry) + 1
                return JSONResponse(
                    {"detail": f"Too many requests - try again in {wait}s."},
                    status_code=429, headers={"Retry-After": str(wait)})
    return await call_next(request)


# --- security headers ------------------------------------------------------- #
_SECURITY_HEADERS_ON = os.environ.get(
    "AGENT_SECURITY_HEADERS", "on").strip().lower() not in ("0", "off", "false", "no")
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    if _SECURITY_HEADERS_ON:
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy",
                     "geolocation=(), camera=(), microphone=(self)")
        h.setdefault("Content-Security-Policy", _CSP)
    return resp


@app.exception_handler(EngineNotConfigured)
async def _engine_not_configured(request: Request, exc: EngineNotConfigured):
    """An engine that isn't set up is a 503 with an explanation, not a 500.

    This escaped as an unhandled exception and reached the browser as a stack
    trace — the user had configured a working engine, and the traceback named
    Anthropic, which is the least useful thing it could have said.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": exc.message
                 + (f" {exc.fix}" if getattr(exc, "fix", "") else "")})


@app.middleware("http")
async def _capture_server_errors(request: Request, call_next):
    """Any unhandled endpoint exception is picked up automatically into the
    error log (visible in 🐞 Issues) before the usual 500 goes out. Expected
    HTTPExceptions (4xx refusals like 'budget reached') pass through — the
    browser reports those from its side with the button context attached."""
    try:
        return await call_next(request)
    except Exception as exc:
        issues.note_error(f"http:{request.url.path[:60]}",
                          f"{type(exc).__name__}: {exc}")
        raise
# Memory is thread-safe SQLite. The brain is created lazily so the server boots
# even without an API key configured, and reports that state via /api/health.
memory = MemoryStore(check_same_thread=False)
_brain = None
_brain_lock = threading.Lock()


# A key saved from the setup screen must reach the engines before anything
# constructs a brain, or a freshly-installed copy stays unconfigured until the
# next restart.
try:
    setupmod.load_saved_credentials()
except Exception:
    pass


def get_brain():
    """The engine, or a clear error — never a dead process.

    A fresh install has no API key by definition, so this has to survive that
    and let the window load. Otherwise the one page that could accept a key
    is the page that can't render.
    """
    global _brain
    if _brain is None:
        with _brain_lock:
            if _brain is None:
                _brain = make_brain()
    return _brain


def get_brain_or_none():
    """For anything that only wants to DESCRIBE the engine, not call it."""
    try:
        return get_brain()
    except Exception:
        return None


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("AGENT_API_KEY"))


class _TurboDone(Exception):
    """The local draft was good enough — skip the cloud pass."""


def _est_turn_cost(turn_info: dict) -> float:
    """What this turn would plausibly have cost on the cloud engine, used
    only to report savings honestly."""
    try:
        u = (turn_info or {}).get("usage") or {}
        return costs.cost_of("Claude", u) if u else 0.004
    except Exception:
        return 0.004


def _reset_brain() -> None:
    """Drop the cached brain so a newly-saved key takes effect immediately
    rather than after a restart."""
    global _brain
    with _brain_lock:
        _brain = None


def _engine_configured() -> bool:
    """Is at least one engine usable (so chat can work)?"""
    return bool(_has_anthropic_key()
                or config.deepseek_pro_key() or config.deepseek_flash_key()
                or brainmod.custom_engine_names()
                or config.BACKEND in ("ollama", "hybrid"))


def _engine_list() -> list:
    """Selectable engines, in display order, with a 'kind' for the UI and the
    price (USD per million tokens) used for cost estimates."""
    def _withprice(item):
        p_in, p_out = brainmod.engine_price(item["id"])
        item["price_in"], item["price_out"] = p_in, p_out
        return item
    items = [
        {"id": "Auto", "label": "Auto", "model": "smart routing + failover",
         "kind": "router"},
    ]
    # Claude was a permanent built-in: an engine you could neither edit nor
    # remove, so a wrong key or model id had nowhere to be corrected — and a
    # hardcoded fall-through turned any unrecognised engine name into a 404
    # from Anthropic. It's a seed now: shown only until you define your own,
    # and yours wins the moment you do.
    if not any(e["name"].lower() == "claude"
               for e in brainmod.load_custom_engines(refresh=True)):
        # Offer it, but say plainly that it can't run without a key. It used
        # to look identical to a working engine, so a fresh install without a
        # key showed Claude selected and every message failed — with nothing
        # in the list suggesting why.
        _has_key = bool(os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("AGENT_API_KEY"))
        items.append({"id": "Claude", "label": "Claude",
                      "model": config.MODEL, "kind": "cloud",
                      "seed": True, "needs_key": not _has_key,
                      "hint": ("Built in. Add an engine called Claude with "
                               "your own key and model to take it over."
                               if _has_key else
                               "No API key set, so this can't run. Add one in "
                               "Settings, or add a local engine — those need "
                               "no key.")})
    # The preloaded "Ollama" engine only works when the running brain actually
    # has a live local model (hybrid/ollama backend with Ollama up). On the
    # anthropic backend it can't run and would just refuse — so hide it there
    # and let the user's own custom local engines (Make local models selectable)
    # be the local path instead.
    try:
        # Describing the engines must never depend on HAVING a working one.
        # This ran on first load, and with no API key `get_brain()` called
        # sys.exit — which inside a request meant /api/meta returned 500 and
        # the window never finished loading. Without a brain we simply can't
        # say whether a local model is live; the rest of the list stands.
        # `return items` here was wrong and mine: it left the function before
        # the user's own custom engines were added, so every engine they saved
        # was stored correctly and never appeared. Skip the bit that needs a
        # brain; carry on with the rest.
        _b = get_brain_or_none()
        if _b is not None and getattr(_b, "local", None) is not None:
            items.append({"id": "Ollama", "label": "Ollama",
                          "model": config.OLLAMA_MODEL, "kind": "local"})
    except Exception:
        pass
    # The two DeepSeek entries were built in when they were the only
    # alternative worth wiring by hand. They are not special any more — one of
    # them was quietly retired by the provider in August and kept failing
    # daily — and an engine you cannot edit or remove is worse than one you
    # add yourself. Add them as custom engines like anything else.
    for e in brainmod.load_custom_engines(refresh=True):
        # "custom" described where it came from, not what it is. Routing needs
        # to know whether a call leaves this machine — and a local model
        # mislabelled as cloud gets counted against a spend cap it never hit.
        items.append({"id": e["name"], "label": e["name"],
                      "model": e["model"],
                      "kind": engines.classify(e["name"])["kind"],
                      "custom": True, "removable": True, "editable": True,
                      "base_url": e.get("base_url", ""),
                      "price_in": float(e.get("price_in", 0) or 0),
                      "price_out": float(e.get("price_out", 0) or 0)})
    return [_withprice(it) if "price_in" not in it else it for it in items]


def _force_model(choice: str):
    """Translate an engine id into a run_turn force_model value (mirrors the
    desktop app's mapping, without the Gradio coupling)."""
    b = get_brain()
    backend = getattr(b, "backend", "")
    local = getattr(b, "local", None)
    has_local = backend == "ollama" or (backend == "hybrid" and local is not None)
    # your own definition first — the built-in is only a seed
    if choice in brainmod.custom_engine_names():
        return choice
    if choice == "Claude":
        return None
    if choice == "DeepSeek Pro":
        return config.DEEPSEEK_MODEL_PRO
    if choice == "DeepSeek Flash":
        return config.DEEPSEEK_MODEL_FLASH
    if choice in brainmod.custom_engine_names():
        return choice
    if choice == "Ollama":
        if backend == "hybrid" and local is not None:
            return b.fast_model
        return main._LOCAL_UNAVAILABLE
    if choice == config.OLLAMA_MODEL_2:
        if has_local:
            return config.OLLAMA_MODEL_2
        return main._LOCAL_UNAVAILABLE
    return main._AUTO


# --- conversation message stores (per id, in-process) ----------------------- #
_conversations: dict = {}
_conv_lock = threading.Lock()

# Cancel flags for in-flight turns (set by /api/chat/cancel, read by run_turn's
# should_cancel between steps so a stuck turn can be stopped).
_cancel_flags: dict = {}
_cancel_lock = threading.Lock()


def _messages_for(cid: str) -> list:
    """Return the in-process Anthropic-format message list for a conversation,
    hydrating it from the persisted transcript the first time it's touched (so
    context survives a server restart)."""
    with _conv_lock:
        if cid not in _conversations:
            hist = []
            try:
                for m in memory.get_transcript(cid):
                    if m["role"] in ("user", "assistant") and m.get("content"):
                        hist.append({"role": m["role"], "content": m["content"]})
            except Exception:
                hist = []
            _conversations[cid] = hist
        return _conversations[cid]


# --- persistent conversation registry + projects (shared agent.db) ---------- #
_ensured_conns: set = set()


def _ensure_web_tables() -> None:
    key = id(memory.conn)
    if key in _ensured_conns:
        return
    memory.conn.execute(
        "CREATE TABLE IF NOT EXISTS projects ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at REAL)")
    memory.conn.execute(
        "CREATE TABLE IF NOT EXISTS session_meta ("
        "session_id TEXT PRIMARY KEY, project_id INTEGER, title TEXT, "
        "created_at REAL, updated_at REAL)")
    memory.conn.commit()
    _ensured_conns.add(key)


def _touch_conversation(cid: str, first_user_text: str | None = None) -> None:
    """Create the registry row on first message, else bump its updated_at."""
    _ensure_web_tables()
    row = memory.conn.execute(
        "SELECT session_id FROM session_meta WHERE session_id = ?", (cid,)).fetchone()
    now = time.time()
    if row is None:
        title = " ".join((first_user_text or "New conversation").split())[:60] \
            or "New conversation"
        memory.conn.execute(
            "INSERT INTO session_meta (session_id, project_id, title, created_at, "
            "updated_at) VALUES (?, NULL, ?, ?, ?)", (cid, title, now, now))
    else:
        memory.conn.execute(
            "UPDATE session_meta SET updated_at = ? WHERE session_id = ?", (now, cid))
    memory.conn.commit()


def _conversation_list(project: str = "all") -> list[dict]:
    _ensure_web_tables()
    base = ("SELECT session_id, project_id, title, created_at, updated_at "
            "FROM session_meta ")
    if project == "all":
        rows = memory.conn.execute(base + "ORDER BY updated_at DESC").fetchall()
    elif project == "none":
        rows = memory.conn.execute(
            base + "WHERE project_id IS NULL ORDER BY updated_at DESC").fetchall()
    else:
        rows = memory.conn.execute(
            base + "WHERE project_id = ? ORDER BY updated_at DESC",
            (int(project),)).fetchall()
    return [{"id": r["session_id"], "project_id": r["project_id"],
             "title": r["title"], "created_at": r["created_at"],
             "updated_at": r["updated_at"]} for r in rows]


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# --- models ---------------------------------------------------------------- #
class EngineBody(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""
    tools: bool = True
    stream: bool = True
    price_in: float = 0.0
    price_out: float = 0.0


# --- meta / health --------------------------------------------------------- #
class EvalRunBody(BaseModel):
    engine: str = ""
    feature: str = ""


@app.get("/api/presenter")
def presenter_state():
    return presenter.state()


@app.get("/api/tour")
def tour_state():
    return tourmod.state()


@app.get("/api/challenges")
def challenges_state():
    return {"report": challenges.report(),
            "sources": challenges.sources(),
            "backlog": challenges.backlog_size()}


class ChallengeScanBody(BaseModel):
    engine: str = ""


class ChallengeProposeBody(BaseModel):
    key: str = ""
    brief: dict = {}
    engine: str = ""


@app.post("/api/challenges/propose")
def challenges_propose(body: ChallengeProposeBody):
    """How Agent Jo might help — and what needs the model itself to change."""
    brief = body.brief or {}
    if not brief and body.key:
        brief = next((b for b in (challenges.report().get("briefs") or [])
                      if b.get("key") == body.key), {})
    if not brief:
        raise HTTPException(status_code=404, detail="no such brief")
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = challenges.propose(brief, get_brain(), model=model)
    if not r.get("ok"):
        raise HTTPException(status_code=502, detail=r["error"][:300])
    return r


class ProposalBody(BaseModel):
    proposal: dict


@app.post("/api/challenges/build")
def challenges_build(body: ProposalBody):
    r = challenges.to_self_improve(body.proposal or {})
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/challenges/feature-note")
def challenges_feature_note(body: ProposalBody):
    r = challenges.as_feature_note(body.proposal or {})
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/challenges/deep-scan")
def challenges_deep_scan(body: ChallengeScanBody | None = None):
    """Reconsider everything seen before that never made it into a brief."""
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    rep = challenges.deep_scan(get_brain(), model=model)
    if rep.get("error") and not rep.get("briefs"):
        raise HTTPException(status_code=502, detail=rep["error"][:400])
    return {"report": rep}


@app.post("/api/challenges/scan")
def challenges_scan(body: ChallengeScanBody | None = None):
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    rep = challenges.scan_and_digest(get_brain(), model=model)
    if rep.get("error") and not rep.get("briefs"):
        raise HTTPException(status_code=502, detail=rep["error"][:400])
    return {"report": rep}


class ChallengeCrewBody(BaseModel):
    index: int
    engine: str = ""          # the panel's picker; blank = the member's own


@app.post("/api/challenges/to-crew")
def challenges_to_crew(body: ChallengeCrewBody):
    r = challenges.to_crew_task(int(body.index))
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    # The picker was only ever sent to the scan, so qualifying a brief fell
    # back to the member's default and quietly billed the cloud engine — the
    # same shape as the image-attach override. An explicit choice must reach
    # every call that runs a turn, not just the obvious one.
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    res = crew.run("BizDev", r["task"], get_brain(), memory, console,
                   session_id="challenge", model=model)
    return {**r, "report": res.get("report", ""), "ok": res.get("ok", False)}


@app.get("/api/skills/list")
def skills_list():
    return {"skills": skillsmod.listing(memory),
            "unused": [s["name"] for s in skillsmod.unused(memory)]}


class SkillRunBody(BaseModel):
    name: str
    input: str = ""


@app.post("/api/skills/run")
def skills_run(body: SkillRunBody):
    """Prepare a skill run. The prompt is handed back so it goes through the
    normal chat path — tools, permissions and the audit trail all behave
    exactly as they do for any other turn."""
    s = skillsmod.find(memory, body.name)
    if s is None:
        raise HTTPException(status_code=404,
                            detail=f"No skill called '{body.name}'.")
    skillsmod.mark_used(memory, s["name"])
    return {"ok": True, "skill": s["name"],
            "prompt": skillsmod.build_run_prompt(s, body.input)}


@app.get("/api/turbo")
def turbo_status():
    inv = engines.inventory()
    ok, why = turbo.usable(inv)
    return {"enabled": bool(config.TURBO), "usable": ok, "why": why,
            **turbo.stats()}


@app.get("/api/engines/{name}")
def engine_get(name: str):
    """One engine's settings, for the edit form. The key is never returned."""
    e = brainmod.get_custom_engine(name)
    if e is None:
        raise HTTPException(status_code=404, detail="no such engine")
    return e


class EngineEditBody(BaseModel):
    name: str
    new_name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    tools: bool | None = None
    stream: bool | None = None
    price_in: float | None = None
    price_out: float | None = None


@app.post("/api/engines/edit")
def engine_edit(body: EngineEditBody):
    ok, msg = brainmod.update_custom_engine(
        body.name, base_url=body.base_url, api_key=body.api_key,
        model=body.model, tools=body.tools, stream=body.stream,
        price_in=body.price_in, price_out=body.price_out,
        new_name=body.new_name)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    # a rename must not leave the default pointing at an engine that's gone
    if body.new_name and config.DEFAULT_ENGINE == body.name:
        config.save_settings({"DEFAULT_ENGINE": body.new_name})
    return {"ok": True, "message": msg, "engines": _engine_list()}


@app.get("/api/routing")
def routing_state():
    """The rules, which engine fills each tier, and where work went."""
    return {"rules": routing.rules(), "tiers": routing.tier_engines(),
            "usage": routing.usage_summary(),
            "recent": routing.recent_decisions(25),
            "tier_names": list(routing.TIERS)}


class RoutingRulesBody(BaseModel):
    rules: list[dict] = []


@app.post("/api/routing/rules")
def routing_save(body: RoutingRulesBody):
    return {"rules": routing.save_rules(body.rules)}


@app.post("/api/routing/reset")
def routing_reset():
    return {"rules": routing.reset_rules()}


class RoutingTestBody(BaseModel):
    text: str = ""
    attachments: bool = False


@app.post("/api/routing/test")
def routing_test(body: RoutingTestBody):
    """Ask what would happen, before it happens."""
    return routing.decide(body.text, body.attachments)


@app.get("/api/intercepts")
def intercepts_list():
    return intercepts.summary()


class InterceptBody(BaseModel):
    id: str
    approve: bool = False
    note: str = ""


@app.post("/api/intercepts/decide")
def intercepts_decide(body: InterceptBody):
    r = intercepts.decide(body.id, body.approve, body.note)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r["error"])
    return r


@app.post("/api/intercepts/clear")
def intercepts_clear():
    return intercepts.clear_decided()


class TuneBody(BaseModel):
    goal: str
    base: str = ""
    params_b: float = 14
    vram_gb: float = 24
    examples: int = 400
    engine: str = ""
    name: str = ""


@app.post("/api/finetune/plan")
def finetune_plan(body: TuneBody):
    """What it would take — and whether tuning is the right tool at all."""
    return finetune.plan(body.goal, body.params_b, body.vram_gb,
                         body.examples)


@app.post("/api/finetune/dataset")
def finetune_dataset(body: TuneBody):
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = finetune.build_dataset(body.goal, get_brain(), body.examples,
                               model=model)
    if not r.get("ok"):
        raise HTTPException(status_code=502,
                            detail="No usable examples came back.")
    return {**r, "sample": finetune.sample(body.goal, 5)}


@app.post("/api/finetune/script")
def finetune_script(body: TuneBody):
    r = finetune.write_script(body.goal, body.base or "unsloth/Qwen3-14B",
                              body.params_b)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/finetune/sample")
def finetune_sample(goal: str, n: int = 8):
    return {"examples": finetune.sample(goal, n)}


@app.get("/api/models/catalogue")
def models_catalogue(vram: float = 24.0):
    """Open-weight models worth running on this card, with the fit maths."""
    return {"vram_gb": vram,
            "recommended": localmodels.recommend(vram),
            "catalogue": localmodels.CATALOGUE,
            "quants": localmodels.QUANTS,
            "derived": localmodels.derived()}


class ModelFitBody(BaseModel):
    params_b: float
    vram_gb: float = 24.0
    quant: str = "q4_K_M"
    context: int = 8192


@app.post("/api/models/fit")
def models_fit(body: ModelFitBody):
    return localmodels.fits(body.params_b, body.vram_gb, body.quant,
                            body.context)


class ModelDeriveBody(BaseModel):
    name: str
    base: str
    system: str = ""
    temperature: float | None = None
    context: int | None = None


@app.post("/api/models/derive")
def models_derive(body: ModelDeriveBody):
    """Make a named variant of an existing model — no training required."""
    r = localmodels.derive(body.name, body.base, body.system,
                           body.temperature, body.context)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class ModelTrainBody(BaseModel):
    base: str
    params_b: float = 8
    vram_gb: float = 24.0
    examples: int = 500
    out_name: str = "my-model"
    dataset_path: str = "train.jsonl"


@app.post("/api/models/training-plan")
def models_training_plan(body: ModelTrainBody):
    plan = localmodels.training_plan(body.base, body.params_b, body.vram_gb,
                                     body.examples)
    return {**plan,
            "script": localmodels.training_script(
                body.base, body.out_name, body.dataset_path, body.params_b)}


@app.get("/api/engines/inventory")
def engines_inventory():
    """What's local, what's cloud, and why — decided from endpoints, not
    from what the engine happens to be called."""
    return engines.inventory()


@app.get("/api/setup")
def setup_state():
    return setupmod.state()


class SetupKeyBody(BaseModel):
    api_key: str = ""


@app.post("/api/setup/key")
def setup_key(body: SetupKeyBody):
    r = setupmod.save_api_key(body.api_key)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    _reset_brain()
    return {**r, "state": setupmod.state()}


@app.post("/api/setup/recheck")
def setup_recheck():
    _reset_brain()
    return setupmod.state()


@app.get("/api/dashboard")
def dashboard_report(cached: bool = False):
    # `cached` returns the last board instantly, so the first paint after a
    # restart isn't an empty pane while the real one is assembled
    if cached:
        snap = dashboard.last_snapshot()
        if snap:
            return {**snap, "cached": True}
    rep = dashboard.report(memory)
    dashboard.save_snapshot(rep)
    return rep


class DashPrefsBody(BaseModel):
    hidden: list[str] | None = None
    order: list[str] | None = None


@app.post("/api/dashboard/prefs")
def dashboard_prefs(body: DashPrefsBody):
    dashboard.save_prefs(hidden=body.hidden, order=body.order)
    rep = dashboard.report(memory)
    dashboard.save_snapshot(rep)
    return rep


@app.get("/api/capabilities")
def capabilities_status():
    return capabilities.status()


@app.get("/api/evals")
def evals_status():
    hist = evals.history(20)
    latest = {}
    for h in hist:
        latest.setdefault(h.get("engine", "?"), h)
    return {"history": hist, "cases": len(evals.CASES),
            "advice": evals.recommendation(list(latest.values()))}


@app.post("/api/evals/run")
def evals_run(body: EvalRunBody):
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    with costs.attribute("evals"):
        return evals.run(get_brain(), engine=body.engine or "",
                         feature=body.feature or "")


@app.get("/api/costs")
def costs_report():
    return {**costs.month_report(),
            "budget": costs.check(),
            "engines": costs.engine_map(),
            "features": costs.KNOWN_FEATURES}


class CostEngineBody(BaseModel):
    feature: str
    engine: str = "Auto"


@app.post("/api/costs/engine")
def costs_engine(body: CostEngineBody):
    return {"engines": costs.set_engine(body.feature, body.engine)}


@app.get("/api/breakers")
def breakers_list():
    return {"breakers": breaker.status()}


@app.post("/api/breakers/{feature}/reset")
def breakers_reset(feature: str):
    r = breaker.reset(feature)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return {**r, "breakers": breaker.status()}


@app.get("/api/phone")
def phone_setup():
    """What to type into a phone, and whether this machine is even listening
    on the network."""
    import socket
    host = "127.0.0.1"
    try:
        # no packet is sent; this just asks the OS which interface it would
        # use to reach the outside, which is the address a phone can see
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        host = sk.getsockname()[0]
        sk.close()
    except Exception:
        pass
    port = int(os.environ.get("AGENT_WEB_PORT", "8765") or 8765)
    bound = os.environ.get("AGENT_WEB_HOST", "")
    lan_ready = bound in ("0.0.0.0", "::", "")
    return {
        "url": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "reachable_on_lan": lan_ready and not host.startswith("127."),
        "why": ("" if lan_ready else
                "The app is bound to this machine only, so a phone can't "
                "reach it. Restart with --host 0.0.0.0."),
        "steps": [
            "Put the phone on the same Wi-Fi as this computer.",
            f"Open http://{host}:{port} in Chrome or Safari.",
            "Sign in, then use the browser menu: Chrome says 'Install app', "
            "Safari is Share \u2192 'Add to Home Screen'.",
        ],
        "caution": ("Anyone on this network who has your password can reach "
                    "it. Don't forward the port to the internet \u2014 use a "
                    "private network like Tailscale if you need it away from "
                    "home."),
        "installed_hint": ("Once added, it opens in its own window with no "
                           "browser bar. The agent keeps running here; the "
                           "phone is a remote control for it."),
    }


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(str(STATIC / "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    """Served from the root on purpose: a service worker can only control
    pages at or below its own path, so one under /static could never manage
    the app itself."""
    return FileResponse(str(STATIC / "sw.js"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@app.get("/api/health")
def health_probe():
    # named health_probe, not health: `health` is the imported health module,
    # and a function of that name shadows it for every later reference
    return {"status": "ok", "app": config.AGENT_NAME,
            "configured": _engine_configured(), "backend": config.BACKEND}


@app.get("/api/meta")
def meta():
    _meta_engines = _engine_list()
    return {"name": config.AGENT_NAME, "configured": _engine_configured(),
            "backend": config.BACKEND,
            "brand": getattr(config, "BRAND_NAME", "Symbolic Synapse"),
            "tagline": getattr(config, "BRAND_TAGLINE", ""),
            "brand_url": getattr(config, "BRAND_URL", ""),
            "engines": _meta_engines,
            # what you chose, and what can actually run. Overloading one
            # field with both meanings silently changed a stored setting
            # into a suggestion — the setting round-trip stops being true.
            "default_engine": config.DEFAULT_ENGINE,
            "start_engine": _usable_default(_meta_engines),
            "voice": {"stt": _stt_ok()}}


def _usable_default(engines: list) -> str:
    """The engine to start on — one that can actually run.

    DEFAULT_ENGINE is "Auto", which routes hard work to the cloud tier, which
    is Claude. On a machine with no API key that means every first message
    fails, and nothing in the picker suggests why. So if the stored default
    can't run, fall to one that can: a local engine needs no key, and a
    custom cloud engine carries its own.
    """
    want = config.DEFAULT_ENGINE
    by_id = {e["id"]: e for e in engines}
    chosen = by_id.get(want)
    broken = (chosen or {}).get("needs_key")

    # "Auto" is only as good as the engines it can route to: with Claude the
    # only cloud engine and no key, it has nothing to escalate to
    if want == "Auto":
        usable = [e for e in engines
                  if e["id"] != "Auto" and not e.get("needs_key")]
        if not usable:
            return want                     # nothing better exists; say so
        if any(e.get("needs_key") for e in engines
               if e["id"] not in ("Auto",)) and len(usable) >= 1:
            # a working engine exists, so start there rather than on a router
            # whose only cloud option can't run
            if all(e.get("needs_key") for e in engines
                   if e.get("kind") == "cloud"):
                return usable[0]["id"]
        return want

    if chosen is None or broken:
        for e in engines:
            if e["id"] != "Auto" and not e.get("needs_key"):
                return e["id"]
    return want


def _stt_ok() -> bool:
    try:
        return bool(voice.stt_available())
    except Exception:
        return False


# --- usage / cost (session running total, priced per engine) ---------------- #
# Tokens are tallied per engine label so each is priced at its own rate — Claude,
# DeepSeek, local (free), and any custom engine you've added.
_usage_by_engine: dict = {}
_usage_lock = threading.Lock()


def _engine_slot(label: str) -> dict:
    return _usage_by_engine.setdefault(
        label, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})


def _accumulate_cost(turn_info: dict) -> None:
    """Fold a finished turn's token usage into the per-engine running totals.
    Uses the per-engine breakdown when present (so multi-engine turns are split
    correctly) and falls back to the turn's single engine label otherwise."""
    info = turn_info or {}
    by_engine = info.get("by_engine")
    feature = info.get("feature") or "chat"
    if by_engine:
        for label, u in by_engine.items():
            _record_persistent_cost(label or "claude", u, feature)
    elif info.get("usage"):
        _record_persistent_cost(info.get("engine") or "claude",
                                info["usage"], feature)
    with _usage_lock:
        if by_engine:
            for label, u in by_engine.items():
                slot = _engine_slot(label or "claude")
                for k in slot:
                    slot[k] += int((u or {}).get(k, 0) or 0)
        else:
            label = info.get("engine") or "claude"
            u = info.get("usage") or {}
            slot = _engine_slot(label)
            for k in slot:
                slot[k] += int(u.get(k, 0) or 0)


def _cost_of(label: str, u: dict) -> float:
    """USD for one engine's token tally, at that engine's price. Cache multipliers
    (write 1.25×, read 0.10×) only matter for Claude, which is the only engine that
    reports cache tokens; for everyone else those counts are zero."""
    p_in, p_out = brainmod.engine_price(label)
    return (u.get("in", 0) * p_in
            + u.get("cache_write", 0) * p_in * 1.25
            + u.get("cache_read", 0) * p_in * 0.10
            + u.get("out", 0) * p_out) / 1_000_000


def _record_persistent_cost(label: str, usage: dict, feature: str = "") -> None:
    """Mirror the in-memory tally into the persistent ledger, so a monthly
    ceiling survives restarts and spend can be attributed per feature."""
    try:
        if usage:
            costs.record(label, usage, feature=feature)
    except Exception:
        pass


def _est_cost() -> float:
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage_by_engine.items()}
    return sum(_cost_of(label, u) for label, u in snapshot.items())


def _is_local_engine(label: str) -> bool:
    key = (label or "").strip().lower()
    return (key in ("local", "ollama")
            or key == (config.OLLAMA_MODEL_2 or "").lower())


def _cost_breakdown() -> list:
    """Per-engine cost + tokens, for a detailed view. `priced` is True when the
    figure is trustworthy — a real price is set, or the engine is a free local
    model. It's False only for a custom engine whose price hasn't been set yet,
    so the UI can prompt for one."""
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage_by_engine.items()}
    rows = []
    for label, u in snapshot.items():
        toks = u.get("in", 0) + u.get("out", 0)
        if toks <= 0:
            continue
        p_in, p_out = brainmod.engine_price(label)
        rows.append({"engine": label,
                     "tokens_in": u.get("in", 0), "tokens_out": u.get("out", 0),
                     "cost": round(_cost_of(label, u), 4),
                     "free": _is_local_engine(label),
                     "priced": bool(p_in or p_out) or _is_local_engine(label)})
    rows.sort(key=lambda r: (-r["cost"], r["engine"]))
    return rows


def _budget_status() -> dict:
    """Where the session stands against the spend cap. cap<=0 means no cap."""
    cap = float(getattr(config, "BUDGET_USD", 0) or 0)
    spent = _est_cost()
    out = {"cap": round(cap, 2), "spent": round(spent, 4), "enabled": cap > 0}
    if cap > 0:
        out["remaining"] = round(max(0.0, cap - spent), 4)
        out["pct"] = min(100, round(spent / cap * 100)) if cap else 0
        out["exceeded"] = spent >= cap
        out["warn"] = spent >= cap * 0.8        # nearing the cap
    else:
        out.update(remaining=None, pct=0, exceeded=False, warn=False)
    return out


def _budget_blocks_autonomy() -> bool:
    """True when a spend cap is set and already reached — used to hold autonomous
    actions (which run unattended) without disarming them."""
    cap = float(getattr(config, "BUDGET_USD", 0) or 0)
    if cap <= 0:
        return False
    try:
        # the persistent month-to-date total is the real number; the in-memory
        # figure only covers since the last restart
        return costs.check(cap)["blocked"] or _est_cost() >= cap
    except Exception:
        return _est_cost() >= cap


@app.get("/api/auto-chain")
def auto_chain():
    """The engines Auto will try, in order, plus the capability-score ladder it
    uses to escalate a struggling turn to a smarter model. Open this in a browser
    to confirm what failover/escalation has to work with: if `chain` shows only
    ['Claude'], no other engine is configured to fall back to."""
    try:
        b = get_brain()
        chain = main._auto_chain(b, None)
        ladder = main._quality_ladder(b)
        from agent import telemetry as _tel
        tstats = _tel.stats()
        return {
            "chain": [main._engine_label_for(t, b) for t in chain],
            "count": len(chain),
            "ladder": [{"engine": main._engine_label_for(t, b),
                        "score": main._engine_score(t, b),
                        "base": main._BASE_ENGINE_SCORES.get(
                            main._engine_label_for(t, b),
                            (getattr(config, "ENGINE_SCORES", {}) or {}).get(
                                main._engine_label_for(t, b), 70)),
                        "observed": tstats.get(main._engine_label_for(t, b))}
                       for t in ladder],
            "auto_escalate": bool(getattr(config, "AUTO_ESCALATE", True)),
        }
    except Exception as exc:
        return {"chain": [], "count": 0, "error": type(exc).__name__}


@app.get("/api/stats")
def stats():
    """KPI snapshot for the dashboard strip."""
    def _safe(fn, default=0):
        try:
            return fn()
        except Exception:
            return default
    _ensure_web_tables()
    convs = _safe(lambda: memory.conn.execute(
        "SELECT COUNT(*) FROM session_meta").fetchone()[0])
    return {
        "memories": _safe(memory.memory_count),
        "skills": _safe(memory.skill_count),
        "learned": {"playbooks": _safe(memory.playbook_count),
                    "lessons": _safe(memory.lesson_count)},
        "tasks": _safe(lambda: len(memory.active_tasks())),
        "documents": _safe(lambda: rag.get_store().doc_count()),
        "conversations": convs,
        "schedules": _safe(lambda: len(memory.list_schedules())),
        "cost": round(_est_cost(), 2),
        "cost_by_engine": _cost_breakdown(),
        "budget": _budget_status(),
        "web": bool(getattr(agent_tools, "WEB_ENABLED", False)),
        "teamwork": bool(config.TEAMWORK),
        "privacy": getattr(config, "PRIVACY_MODE", "off"),
        "mcp": _safe(agent_mcp.manager.summary),
    }


class BudgetBody(BaseModel):
    cap: float = 0.0


@app.get("/api/budget")
def get_budget():
    return _budget_status()


@app.post("/api/budget")
def set_budget(body: BudgetBody):
    """Set the session spend cap (USD). 0 clears it. Persists like other settings."""
    cap = max(0.0, float(body.cap or 0))
    config.save_settings({"BUDGET_USD": cap})
    audit.record("config", name="budget", detail=f"cap=${cap:.2f}")
    return _budget_status()


# --- issue reports: local, structured, paste-able bug reports ---------------- #
class IssueBody(BaseModel):
    note: str
    conversation_id: str = ""
    engine: str = ""


class JobsAutoBody(BaseModel):
    enabled: bool | None = None
    dry_run: bool | None = None
    min_score: int | None = None
    daily_cap: int | None = None
    require_clean_check: bool | None = None
    signature: str | None = None


@app.post("/api/jobs/auto")
def jobs_auto(body: JobsAutoBody):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"auto": jobscout.save_auto_config(patch)}


class JobsCycleBody(BaseModel):
    engine: str = ""


@app.post("/api/jobs/auto/run")
def jobs_auto_run(body: JobsCycleBody | None = None):
    # This took no engine at all, so every scoring and drafting call went to
    # the default — the paid cloud engine — regardless of what was picked
    # anywhere else. Same shape as the Challenges bug.
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = jobscout.auto_apply(get_brain(), model=model)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"][:300])
    return r


class PortalApplyBody(BaseModel):
    key: str
    submit: bool = False


@app.post("/api/jobs/portal")
def jobs_portal(body: PortalApplyBody):
    """Open the advert in a real browser and fill the form.

    Runs on the server's desktop session — this is a desktop agent, so the
    window opens on the machine Agent Jo is running on, not on a phone
    connected to it."""
    role = jobscout.get_role(body.key)
    if role is None:
        raise HTTPException(status_code=404, detail="no such role")
    res = portal.apply_to_portal(role, jobscout.profile(),
                                 submit=bool(body.submit))
    if res.get("state") == portal.SUBMITTED:
        jobscout.set_stage(body.key, "applied", "submitted via portal")
    elif res.get("state") == portal.FILLED:
        jobscout.update_role(body.key, portal_filled_at=res.get("at", ""))
    return res


@app.get("/api/jobs/portal/history")
def jobs_portal_history():
    return {"runs": portal.history(20),
            "ready": portal.readiness(jobscout.profile()),
            "browser_profile": str(portal.profile_dir())}


class JobsSearchBody(BaseModel):
    query: str = ""


class JobsUrlBody(BaseModel):
    url: str
    use_browser: bool = False


@app.post("/api/jobs/search/url")
def jobs_search_url(body: JobsUrlBody):
    """Search any page the user names — the 'anywhere' path."""
    r = jobscout.search_url(body.url, use_browser=bool(body.use_browser))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/search")
def jobs_search(body: JobsSearchBody | None = None):
    """Look now, record nothing — a preview, so forty unwanted roles don't
    have to be undone."""
    return jobscout.search((body.query if body else "") or "")


class JobsAddBody2(BaseModel):
    items: list = []


@app.post("/api/jobs/search/add")
def jobs_search_add(body: JobsAddBody2):
    return jobscout.add_from_search(body.items)


class JobsSearchCfgBody(BaseModel):
    queries: list[str] | None = None
    exclude: list[str] | None = None
    locations: list[str] | None = None
    remote_only: bool | None = None
    require_email: bool | None = None


@app.get("/api/jobs/boards/match")
def jobs_boards_match():
    """Boards that suit this profile and serve somewhere they can work."""
    return {"boards": boards.suggest(jobscout.profile())}


@app.post("/api/jobs/boards/auto")
def jobs_boards_auto():
    """Find, verify and add — only boards that actually return roles."""
    r = boards.auto_add(jobscout.profile())
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/jobs/sources/suggested")
def jobs_sources_suggested():
    """Offered, never imposed — the user picks which to add."""
    have = {x.get("url") for x in jobscout.job_sources()}
    return {"suggested": [dict(x, added=x["url"] in have)
                          for x in jobscout.SUGGESTED_SOURCES]}


@app.get("/api/jobs/search/config")
def jobs_search_cfg():
    # quietly fix sources saved without a scheme, so an old mistake doesn't
    # keep failing every run
    fixed = jobscout.repair_sources()
    return {"repaired": fixed, "search": jobscout.search_config(),
            "sources": jobscout.job_sources()}


@app.post("/api/jobs/search/config")
def jobs_search_cfg_save(body: JobsSearchCfgBody):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"search": jobscout.save_search_config(patch),
            "sources": jobscout.job_sources()}


class JobsSourceBody(BaseModel):
    name: str
    url: str = ""
    kind: str = "rss"
    on: bool | None = None


@app.post("/api/jobs/sources")
def jobs_sources_add(body: JobsSourceBody):
    if body.on is not None and not body.url:
        r = jobscout.set_job_source(body.name, body.on)
    else:
        r = jobscout.add_job_source(body.name, body.url, body.kind)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class JobsSourceRemoveBody(BaseModel):
    name: str = ""
    url: str = ""


@app.post("/api/jobs/sources/remove")
def jobs_sources_remove(body: JobsSourceRemoveBody):
    """The identifier goes in the body, not the path.

    It used to be a path parameter, which 404'd for any source whose name
    contained a slash — a url pasted as a name, for instance. The encoded
    %2F is decoded back to / before routing, so the path grew extra segments
    and matched nothing."""
    r = jobscout.remove_job_source(body.url or body.name)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


class JobsRemoveBody(BaseModel):
    key: str = ""
    keys: list[str] = []
    forget: bool = True


@app.post("/api/jobs/remove")
def jobs_remove(body: JobsRemoveBody):
    """Stop tracking a role. By default it won't be found again."""
    if body.keys:
        r = jobscout.remove_roles(body.keys, forget=body.forget)
    else:
        r = jobscout.remove_role(body.key, forget=body.forget)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


class JobsClearBody(BaseModel):
    stage: str = ""
    never_scored: bool = False
    forget: bool = False


class JobsAlertBody(BaseModel):
    messages: list[dict] = []


@app.post("/api/jobs/alerts/ingest")
def jobs_alerts_ingest(body: JobsAlertBody):
    """Turn job-alert emails into tracked roles."""
    r = jobalerts.ingest(body.messages or [])
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class JobsPasteBody(BaseModel):
    raw: str = ""
    sender: str = ""


@app.post("/api/jobs/alerts/paste")
def jobs_alerts_paste(body: JobsPasteBody):
    """Parse one pasted alert email. No setup, no credentials."""
    if not (body.raw or "").strip():
        raise HTTPException(status_code=400, detail="nothing pasted")
    r = jobalerts.from_raw(body.raw, body.sender)
    if not r.get("roles"):
        raise HTTPException(
            status_code=422,
            detail=("No job links found in that. Paste the whole email "
                    "including its links — a plain-text copy often loses "
                    "them."))
    res = jobscout.add_roles(r["roles"])
    return {**r, "added": res.get("added", 0)}


@app.post("/api/jobs/alerts/folder")
def jobs_alerts_folder():
    r = jobalerts.ingest_folder()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/jobs/alerts/guide")
def jobs_alerts_guide():
    return {"guide": jobalerts.setup_guide(),
            "why": ("Boards that block automated readers will happily email "
                    "you the same listings. It's the route they support, so "
                    "it doesn't break when they change their pages.")}


class JobsAppliedBody(BaseModel):
    key: str
    how: str = "by hand"
    note: str = ""


@app.post("/api/jobs/applied")
def jobs_applied(body: JobsAppliedBody):
    """Record an application you made yourself."""
    r = jobscout.mark_applied(body.key, body.how, body.note)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r["error"])
    return r


class JobsArchiveBody(BaseModel):
    applied_before_days: int = 0


@app.post("/api/jobs/archive")
def jobs_archive(body: JobsArchiveBody):
    r = jobscout.archive_closed(body.applied_before_days)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/jobs/archive")
def jobs_archive_list():
    return jobscout.archive_summary()


class JobsUnarchiveBody(BaseModel):
    key: str


@app.post("/api/jobs/unarchive")
def jobs_unarchive(body: JobsUnarchiveBody):
    r = jobscout.unarchive(body.key)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@app.post("/api/jobs/sweep")
def jobs_sweep():
    """Check the oldest adverts and close the ones that say they've shut."""
    return jobscout.sweep_expired(limit=15)


@app.get("/api/jobs/auto/preview")
def jobs_auto_preview():
    """What the next unattended run would do. Costs nothing to ask."""
    return {**jobscout.auto_preview(), "runs": jobscout.recent_runs()}


@app.get("/api/jobs/outcomes")
def jobs_outcomes():
    """What happened to the applications, and what it's fair to conclude."""
    return outcomes.summary()


@app.get("/api/jobs/pipeline")
def jobs_pipeline():
    return jobscout.pipeline()


@app.post("/api/jobs/dedupe")
def jobs_dedupe():
    r = jobscout.dedupe_roles()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/autopilot")
def jobs_autopilot():
    """Set the whole loop running daily, in one action.

    Every piece existed; joining them up was left as an exercise. Rehearsal
    stays on, so the first days produce drafts to read rather than sent mail."""
    cfg = jobscout.auto_config()
    jobscout.save_auto_config({**cfg, "enabled": True,
                               "dry_run": cfg.get("dry_run", True)})
    existing = [s2 for s2 in memory.list_schedules()
                if (s2.get("action") or "") == "jobscout"]
    if existing:
        for s2 in existing:
            memory.set_schedule_enabled(s2["id"], True)
        return {"ok": True, "created": False,
                "note": "The daily run was already set up; it's enabled."}
    memory.create_schedule(
        name="Job scout — daily", prompt="find and apply", spec_json="{}",
        action="jobscout", payload="{}")
    return {"ok": True, "created": True,
            "dry_run": jobscout.auto_config().get("dry_run", True),
            "note": ("It will find, de-duplicate, screen, score, draft and "
                     "apply once a day. Rehearsal is on, so nothing is sent "
                     "until you turn it off.")}


@app.post("/api/jobs/prune")
def jobs_prune():
    """Clear out entries that were never vacancies (scraped category links)."""
    r = jobscout.prune_junk()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/clear")
def jobs_clear(body: JobsClearBody):
    r = jobscout.clear_roles(stage=body.stage,
                             never_scored=body.never_scored,
                             forget=body.forget)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/unignore")
def jobs_unignore(body: JobsRemoveBody):
    return jobscout.unignore(body.key)


# Defined BEFORE the endpoints that use it. FastAPI resolves the annotation
# when the route is registered, and a model declared later isn't found — so
# the parameter was treated as a QUERY field and every call came back 422
# saying "body: Field required", which reads like the client's fault.
class JobsKeyBody(BaseModel):
    key: str
    engine: str = ""


@app.post("/api/jobs/ats")
def jobs_ats(body: JobsKeyBody):
    """Keyword coverage against the advert. Local, instant, no engine call."""
    r = jobscout.get_role(body.key)
    if r is None:
        raise HTTPException(status_code=404, detail="no such role")
    return cvmod.ats_scan(r, jobscout.profile())


@app.post("/api/jobs/cv")
def jobs_cv(body: JobsKeyBody):
    """A CV ordered for this advert, built only from the profile."""
    r = jobscout.get_role(body.key)
    if r is None:
        raise HTTPException(status_code=404, detail="no such role")
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    res = cvmod.tailor_cv(r, jobscout.profile(), get_brain(), model=model)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res["error"][:300])
    saved = cvmod.save_cv(r, res["cv"], jobscout.profile())
    jobscout.update_role(body.key, tailored_cv=res["cv"],
                         cv_path=saved.get("path", ""))
    return {**res, "path": saved.get("path", "")}


@app.post("/api/jobs/interview")
def jobs_interview(body: JobsKeyBody):
    r = jobscout.get_role(body.key)
    if r is None:
        raise HTTPException(status_code=404, detail="no such role")
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    res = cvmod.interview_prep(r, jobscout.profile(), get_brain(), model=model)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res["error"][:300])
    jobscout.update_role(body.key, interview=res)
    return res


@app.get("/api/jobs/claims")
def jobs_claims():
    """What the fabrication guard is holding, and why."""
    return jobscout.held_claims()


class JobsClaimBody(BaseModel):
    term: str
    where: str = "technologies"


@app.post("/api/jobs/claims/confirm")
def jobs_claim_confirm(body: JobsClaimBody):
    r = jobscout.confirm_claim(body.term, body.where)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/claims/dismiss")
def jobs_claim_dismiss(body: JobsClaimBody):
    r = jobscout.dismiss_claim(body.term)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/jobs/discover")
def jobs_discover():
    return jobscout.discover()


@app.post("/api/jobs/cycle")
def jobs_cycle(body: JobsCycleBody | None = None):
    """Find, score, draft and send in one pass — the unattended run."""
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    return jobscout.auto_cycle(get_brain(), model=model)


@app.post("/api/jobs/follow-up")
def jobs_follow_up(body: JobsKeyBody):
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = jobscout.draft_follow_up(body.key, get_brain(), model=model)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"][:300])
    return r


@app.post("/api/jobs/save-file")
def jobs_save_file(body: JobsKeyBody):
    r = jobscout.save_application_file(body.key)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/jobs")
def jobs_list():
    # a role can be old without being provably closed; the list should say so
    # the bucket comes from role_state, so the list, the counts and the tabs
    # all describe a role the same way
    _cfg = jobscout.auto_config()
    return {"roles": [dict(r, stale=jobscout.looks_stale(r),
                           days_listed=round(jobscout.days_listed(r)),
                           bucket=jobscout.role_state(r, _cfg)["bucket"],
                           state_why=jobscout.role_state(r, _cfg)["why"])
                      for r in jobscout.roles()],
            "profile": jobscout.profile(),
            "auto": jobscout.auto_config(),
            "summary": jobscout.summary(),
            "follow_ups": jobscout.follow_ups(),
            "daily": jobscout.schedule_enabled(memory)}


@app.post("/api/jobs/profile")
async def jobs_profile(request: Request):
    body = await request.json()
    return {"profile": jobscout.save_profile(body or {})}


class JobsAddBody(BaseModel):
    roles: list = []


@app.post("/api/jobs/add")
def jobs_add(body: JobsAddBody):
    return jobscout.add_roles(body.roles)




def _job_failure(err: str) -> HTTPException:
    """Split "the role isn't there" from "the engine refused".

    Mapping every failure to one status made a missing role look like an
    outage, and an outage look like a malformed request. The status is the
    first thing anyone reads in a log."""
    e = str(err or "")
    if "no such role" in e.lower() or "not found" in e.lower():
        return HTTPException(status_code=404, detail=e[:200])
    if "profile" in e.lower() and "empty" in e.lower():
        return HTTPException(status_code=409, detail=e[:300])
    return HTTPException(status_code=502,
                         detail=jobscout._explain_engine_error(e))


@app.post("/api/jobs/score")
def jobs_score(body: JobsKeyBody):
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = jobscout.score_role(body.key, get_brain(), model=model)
    if not r.get("ok"):
        # 400 said "your request was malformed", which it wasn't — the engine
        # refused. And the raw provider string ("invalid x-api-key") is not
        # something anyone can act on.
        raise _job_failure(r["error"])
    return r


@app.post("/api/jobs/draft")
def jobs_draft(body: JobsKeyBody):
    model, why = _trend_model(body.engine or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    r = jobscout.draft_application(body.key, get_brain(), model=model)
    if not r.get("ok"):
        raise _job_failure(r["error"])
    return r


class JobsStageBody(BaseModel):
    key: str
    stage: str
    note: str = ""


@app.post("/api/jobs/stage")
def jobs_stage(body: JobsStageBody):
    r = jobscout.set_stage(body.key, body.stage, body.note)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class JobsDailyBody(BaseModel):
    enabled: bool


@app.post("/api/jobs/schedule")
def jobs_schedule(body: JobsDailyBody):
    return {"daily": jobscout.set_schedule(memory, scheduler,
                                           bool(body.enabled))}


@app.get("/api/health/board")
def health_report():
    """The full health board.

    NOT /api/health: that path was already taken by the liveness probe above,
    which is registered first and therefore wins. The board silently returned
    the probe's payload instead, so the panel rendered nothing and looked
    dead."""
    return health.report(memory)


@app.get("/api/backups")
def backups_list():
    return {"backups": backupmod.list_backups(),
            "nightly": backupmod.schedule_enabled(memory)}


class BackupCreateBody(BaseModel):
    include_bulk: bool = False
    include_key: bool = False
    note: str = ""


@app.post("/api/backups/create")
def backups_create(body: BackupCreateBody):
    try:
        p = backupmod.create_backup(
            memory, rag.get_store(), include_bulk=body.include_bulk,
            include_key=body.include_key, note=body.note)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}")
    return {"ok": True, "name": os.path.basename(p),
            "backups": backupmod.list_backups()}


@app.post("/api/backups/{name}/verify")
def backups_verify(name: str):
    return backupmod.verify_backup(name)


@app.post("/api/backups/{name}/restore")
def backups_restore(name: str):
    p = backupmod._resolve(name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such backup")
    try:
        return backupmod.restore_backup(str(p), memory, rag.get_store())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}")


@app.delete("/api/backups/{name}")
def backups_delete(name: str):
    r = backupmod.delete_backup(name)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@app.get("/api/backups/{name}/download")
def backups_download(name: str):
    p = backupmod._resolve(name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such backup")
    return FileResponse(str(p), filename=p.name,
                        media_type="application/zip")


class BackupNightlyBody(BaseModel):
    enabled: bool


@app.post("/api/backups/schedule")
def backups_schedule(body: BackupNightlyBody):
    return {"nightly": backupmod.set_schedule(memory, scheduler,
                                              bool(body.enabled))}


class CrewChainBody(BaseModel):
    chain: str = "opportunity"
    task: str
    engine: str = ""


@app.post("/api/crew/chain")
def crew_chain(body: CrewChainBody):
    if not body.task.strip():
        raise HTTPException(status_code=400, detail="Task is empty.")
    _chmodel, _chwhy = _trend_model(body.engine or "")
    if _chwhy:
        raise HTTPException(status_code=409, detail=_chwhy)
    res = crew.run_chain(body.chain, body.task, get_brain(), memory, console,
                         session_id="crew-chain", model=_chmodel)
    if not res.get("ok") and res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"][:300])
    return res


@app.get("/api/crew")
def crew_list():
    return {"members": crew.members(), "runs": crew.recent_runs(15),
            "chains": crew.chains(), "chain_runs": crew.chain_runs(10)}


class CrewMemberBody(BaseModel):
    name: str
    role: str = ""
    brief: str = ""
    engine: str = "Auto"
    keywords: list[str] = []


@app.post("/api/crew/member")
def crew_member(body: CrewMemberBody):
    r = crew.upsert(body.model_dump())
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error", "failed"))
    return r


@app.delete("/api/crew/member/{name}")
def crew_member_delete(name: str):
    r = crew.remove(name)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


class CrewRunBody(BaseModel):
    task: str
    member: str = ""          # blank = let the dispatcher choose
    engine: str = ""          # blank = the member's own engine


@app.post("/api/crew/run")
def crew_run(body: CrewRunBody):
    if not body.task.strip():
        raise HTTPException(status_code=400, detail="Task is empty.")
    _cmodel, _cwhy = _trend_model(body.engine or "")
    if _cwhy:
        raise HTTPException(status_code=409, detail=_cwhy)
    if body.member:
        res = crew.run(body.member, body.task, get_brain(), memory, console,
                       session_id=f"crew-{body.member}", model=_cmodel)
    else:
        res = crew.dispatch(body.task, get_brain(), memory, console,
                            session_id="crew-dispatch", model=_cmodel)
    if not res.get("ok") and res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"][:300])
    return res


class CrewScheduleBody(BaseModel):
    member: str
    kind: str = ""            # "" clears the schedule
    time: str = "07:30"
    dow: int = 0
    task: str = ""


@app.post("/api/crew/schedule")
def crew_schedule(body: CrewScheduleBody):
    spec = None if not body.kind else {
        "kind": body.kind, "time": body.time, "dow": body.dow,
        "task": body.task}
    r = crew.set_schedule(body.member, spec, memory, scheduler)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/trends")
def trends_status():
    _p = trendscout.progress()
    return {"report": trendscout.report(),
            "progress": ({"done": _p.get("next", 0),
                          "total": len(_p.get("items", [])),
                          "error": _p.get("error", "")} if _p else None),
            "weekly": trendscout.schedule_enabled(memory),
            "engine": trendscout.load_config().get("engine",
                                                   config.DEFAULT_ENGINE),
            "engines": ["Auto", "Claude"] + list(
                brainmod.custom_engine_names())}


class TrendScanBody(BaseModel):
    engine: str = ""


def _trend_model(choice: str):
    """Resolve an engine name to something a turn can actually run on.

    Returns (model, reason_it_cannot). Auto/blank keeps the brain's default.

    The important case is an engine name nobody recognises. _force_model
    answers with the AUTO sentinel for those — indistinguishable from the user
    actually choosing Auto — so a typo, a renamed engine or a stale setting
    quietly became "use the default", which is the paid cloud engine. That is
    the bug this whole resolver exists to prevent, so an unrecognised name is
    refused with a reason instead.
    """
    choice = (choice or config.DEFAULT_ENGINE or "Auto").strip()
    if choice in ("", "Auto"):
        return None, ""
    forced = _force_model(choice)
    if forced is main._LOCAL_UNAVAILABLE:
        return None, (f"'{choice}' is a local engine but no local model is "
                      f"running to serve it. Start Ollama, or pick another "
                      f"engine.")
    if forced is main._AUTO:
        # not Auto by request — the name simply wasn't recognised
        try:
            names = ", ".join(["Auto", "Claude"]
                              + list(brainmod.custom_engine_names())) or "Auto"
        except Exception:
            names = "Auto, Claude"
        return None, (f"'{choice}' isn't an engine this app knows, so it will "
                      f"not be used — and it will not silently fall back to a "
                      f"paid engine. Available: {names}.")
    return forced, ""


@app.post("/api/trends/resume")
def trends_resume(body: TrendScanBody | None = None):
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    rep = trendscout.resume(get_brain(), model=model)
    return {"report": rep}


@app.post("/api/trends/scan")
def trends_scan(body: TrendScanBody | None = None):
    model, why = _trend_model((body.engine if body else "") or "")
    if why:
        raise HTTPException(status_code=409, detail=why)
    rep = trendscout.scan_and_digest(get_brain(), model=model)
    # a partial run is a success with work saved — the panel shows the
    # error and a Resume button rather than throwing everything away
    if rep.get("error") and not rep.get("resumable") and not rep.get("trends"):
        raise HTTPException(status_code=502, detail=rep["error"][:400])
    return {"report": rep}


class TrendAdoptBody(BaseModel):
    index: int


@app.post("/api/trends/adopt")
def trends_adopt(body: TrendAdoptBody):
    try:
        r = trendscout.adopt(int(body.index), memory)
    except Exception as exc:                     # never an opaque 500
        raise HTTPException(
            status_code=400,
            detail=(f"Adopt failed: {type(exc).__name__}: {exc}. If this "
                    f"mentions 'strip', the running app predates the fix — "
                    f"restart it (build {getattr(config, 'BUILD_ID', '?')})."))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error", "failed"))
    return {**r, "report": trendscout.report()}


class TrendEngineBody(BaseModel):
    engine: str


@app.post("/api/trends/engine")
def trends_engine(body: TrendEngineBody):
    cfg = trendscout.load_config()
    cfg["engine"] = (body.engine or "Auto").strip()
    trendscout.save_config(cfg)
    return {"engine": cfg["engine"]}


class TrendWeeklyBody(BaseModel):
    enabled: bool


@app.post("/api/trends/schedule")
def trends_schedule(body: TrendWeeklyBody):
    return {"weekly": trendscout.set_schedule(memory, scheduler,
                                              bool(body.enabled))}


@app.get("/api/neural3d/detect")
def neural3d_detect():
    return {"found": neural3d.detect(),
            "current": getattr(config, "NEURAL3D_CMD", "")}


class Neural3DCheckBody(BaseModel):
    command: str = ""


@app.post("/api/neural3d/check")
def neural3d_check(body: Neural3DCheckBody):
    return neural3d.validate(body.command
                             or getattr(config, "NEURAL3D_CMD", ""))


@app.get("/api/neural3d")
def neural3d_jobs():
    return {"configured": bool(getattr(config, "NEURAL3D_CMD", "").strip()),
            "jobs": neural3d.jobs(20)}


@app.get("/api/neural3d/file")
def neural3d_file(job: str, name: str):
    p = neural3d.file_path(job, name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(str(p), filename=p.name)


@app.get("/api/blender")
def blender_jobs():
    return {"blender": blenderlab.find_blender() or None,
            "jobs": blenderlab.jobs(20)}


@app.get("/api/blender/file")
def blender_file(job: str, name: str):
    p = blenderlab.model_path(job, name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such model file")
    return FileResponse(str(p), filename=p.name,
                        media_type="application/octet-stream")


@app.get("/api/blender/image")
def blender_image(job: str, name: str):
    p = blenderlab.image_path(job, name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such render")
    return FileResponse(str(p))


@app.get("/api/pipelines")
def pipelines_status():
    return {"pipelines": datapipeline.list_pipelines()}


@app.get("/api/self/suggestions")
def self_suggestions():
    return {"suggestions": selfimprove.suggestions(memory)}


@app.get("/api/self/history")
def self_history():
    return {"history": selfimprove.history(30)}


class SelfRevertBody(BaseModel):
    id: str


@app.post("/api/self/revert")
def self_revert(body: SelfRevertBody):
    r = selfimprove.revert(body.id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/selfimprove")
def selfimprove_status():
    return {"enabled": bool(config.SELFIMPROVE), **selfimprove.proposal()}


@app.post("/api/selfimprove/apply")
def selfimprove_apply():
    res = selfimprove.apply()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "failed"))
    return res


@app.post("/api/selfimprove/discard")
def selfimprove_discard():
    audit.record("selfimprove", name="discard")
    return selfimprove.discard()


@app.get("/api/timemachine")
def timemachine_list():
    return {"enabled": bool(config.TIMEMACHINE),
            "entries": timemachine.entries(80),
            "store_bytes": timemachine.store_size()}


@app.get("/api/timemachine/{entry_id}/diff")
def timemachine_diff(entry_id: str):
    return timemachine.diff(entry_id)


@app.post("/api/timemachine/{entry_id}/restore")
def timemachine_restore(entry_id: str):
    res = timemachine.restore(entry_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", "failed"))
    return {**res, "entries": timemachine.entries(80)}


class AuditClearBody(BaseModel):
    keep_days: int = 0
    archive: bool = True


@app.post("/api/audit/clear")
def audit_clear(body: AuditClearBody):
    r = audit.clear(keep_days=int(body.keep_days or 0),
                    archive=bool(body.archive))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/audit/archives")
def audit_archives():
    return {"archives": audit.archives()}


class AuditArchiveBody(BaseModel):
    names: list[str] = []


@app.post("/api/audit/archives/delete")
def audit_archives_delete(body: AuditArchiveBody):
    r = audit.delete_archives(body.names or None)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/audit/reseal")
def audit_reseal():
    r = audit.reseal()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/audit")
def audit_list(limit: int = 100, kind: str = ""):
    return {"enabled": bool(config.AUDIT), "count": audit.count(),
            "chain": audit.verify(),
            "entries": audit.recent(min(max(1, limit), 500), kind.strip())}


@app.get("/api/audit/export")
def audit_export(fmt: str = "text"):
    text = audit.export_csv() if fmt == "csv" else audit.export_text()
    return {"count": audit.count(), "fmt": fmt, "text": text}


@app.post("/api/audit/verify")
def audit_verify():
    return audit.verify()


@app.get("/api/issues")
def issues_list():
    items = issues.list_issues(20)
    return {"count": issues.issue_count(),
            "errors": issues.recent_errors(10),
            "issues": [{"ts": e.get("ts"), "iso": e.get("iso"),
                        "note": e.get("note"), "engine": e.get("engine"),
                        "text": issues.format_issue(e)} for e in items]}


class ClientErrorBody(BaseModel):
    message: str
    url: str = ""
    status: int = 0
    detail: str = ""
    ui: str = ""


_client_err_times: list = []


@app.post("/api/client-errors")
def client_errors(body: ClientErrorBody):
    """Automatic error pickup from the browser: uncaught JS errors and every
    API call that fails or returns an error status land here, with the element
    that was active (usually the clicked button) attached. Capped per hour so a
    broken loop can't flood the log."""
    now = time.time()
    _client_err_times[:] = [t for t in _client_err_times if now - t < 3600]
    if len(_client_err_times) >= 120:
        return {"ok": True, "stored": False}
    _client_err_times.append(now)
    bits = []
    if body.ui.strip():
        bits.append(f"[{body.ui.strip()[:40]}]")
    if body.url.strip():
        bits.append(f"{body.url.strip()[:80]}" +
                    (f" -> {body.status}" if body.status else ""))
    msg = " ".join(bits + [body.message.strip()[:200]])
    if body.detail.strip():
        msg += f" — {body.detail.strip()[:150]}"
    issues.note_error("ui", msg)
    return {"ok": True, "stored": True}


@app.post("/api/issues/clear-errors")
def issues_clear_errors():
    return {"ok": True, "cleared": issues.clear_errors()}


@app.post("/api/issues")
def issues_create(body: IssueBody):
    if not body.note.strip():
        raise HTTPException(status_code=400, detail="Describe what went wrong.")
    entry = issues.record_issue(memory, body.note,
                                session_id=body.conversation_id,
                                engine=body.engine)
    return {"ok": True, "count": issues.issue_count(),
            "text": issues.format_issue(entry)}


@app.get("/api/issues/export")
def issues_export():
    return {"count": issues.issue_count(), "text": issues.export_all()}


@app.post("/api/issues/clear")
def issues_clear():
    return {"ok": True, "cleared": issues.clear_all()}


# --- outreach / email ------------------------------------------------------- #
class EmailConfigBody(BaseModel):
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    from_name: str | None = None
    use_tls: bool | None = None
    enabled: bool | None = None
    footer: str | None = None


class EmailSendBody(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""
    dry_run: bool = True


class CampaignBody(BaseModel):
    subject: str = ""
    body: str = ""
    contacts: list = []
    dry_run: bool = True
    confirm: bool = False


@app.get("/api/email/status")
def email_status():
    return outreach.status()


@app.post("/api/email/config")
def email_config(body: EmailConfigBody):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return outreach.save_config(data)


@app.post("/api/email/send")
def email_send(body: EmailSendBody):
    return outreach.send(body.to, body.subject, body.body, dry_run=body.dry_run)


@app.post("/api/campaign/draft")
def campaign_draft(body: CampaignBody):
    """Render personalised drafts for review — never sends."""
    drafts = outreach.build_campaign(body.subject, body.body, body.contacts)
    valid = sum(1 for d in drafts if d["valid"])
    return {"drafts": drafts, "count": len(drafts), "valid": valid}


@app.post("/api/campaign/send")
def campaign_send(body: CampaignBody):
    """Send a campaign. confirm=true actually delivers; otherwise it's a dry-run
    that renders + logs without sending. Rate-limited and audited."""
    drafts = [d for d in outreach.build_campaign(body.subject, body.body, body.contacts)
              if d["valid"]]
    return outreach.send_campaign(drafts, dry_run=not body.confirm)


@app.get("/api/email/log")
def email_log():
    return {"log": outreach.recent_log(100)}


class AutopilotBody(BaseModel):
    autonomous_enabled: bool | None = None
    require_allowlist: bool | None = None
    max_per_run: int | None = None
    allowed_recipients: list | str | None = None
    allowed_domains: list | str | None = None


class AutopilotRunBody(BaseModel):
    subject: str = ""
    body: str = ""
    contacts: list = []
    dry_run: bool = True


@app.post("/api/email/autopilot")
def email_autopilot(body: AutopilotBody):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return outreach.save_config(data)


@app.post("/api/email/autopilot/run")
def email_autopilot_run(body: AutopilotRunBody):
    """Run an outreach job end-to-end with no per-send approval. Honours the arm
    switch, allowlist, per-run cap and rate limits. dry_run previews safely."""
    if not body.dry_run and _budget_blocks_autonomy():
        return {"ok": False, "sent": 0, "blocked": 0, "skipped": 0,
                "error": f"budget reached (${config.BUDGET_USD:.2f}); raise or clear "
                         f"the cap to send. Dry-run is still available."}
    return outreach.run_autopilot(body.subject, body.body, body.contacts,
                                  dry_run=body.dry_run)


@app.post("/api/email/autopilot/pause")
def email_autopilot_pause():
    return outreach.pause_autonomous()


class WatcherBody(BaseModel):
    name: str = "Watcher"
    source_type: str = "url"        # url | search
    source: str = ""
    instruction: str = ""
    mode: str = "draft"             # draft (observe) | send
    kind: str = "hourly"
    time: str = "07:00"
    n: int = 60
    dow: int = 0
    full_access: bool = False


@app.post("/api/watchers")
def watcher_create(body: WatcherBody):
    """Create a monitor that checks a source on a cadence and, when it changes,
    hands the change to the agent to decide what to do (it can only send within
    the auto-pilot fence)."""
    import json as _json
    if body.kind not in scheduler.KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown frequency: {body.kind}")
    if not body.source.strip():
        raise HTTPException(status_code=400, detail="A source (URL or search query) is required.")
    if body.source_type not in ("url", "search"):
        raise HTTPException(status_code=400, detail="source_type must be 'url' or 'search'.")
    try:
        spec_json = scheduler.make_spec(body.kind, time_str=(body.time or "07:00"),
                                        n=int(body.n or 60), dow=int(body.dow or 0))
        nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}")
    payload = _json.dumps({"source_type": body.source_type, "source": body.source.strip(),
                           "instruction": body.instruction.strip(),
                           "mode": "send" if body.mode == "send" else "draft"})
    prompt = f"Watch {body.source_type}: {body.source.strip()[:120]}"
    sid = memory.create_schedule(body.name.strip() or "Watcher", prompt, spec_json,
                                 "Auto", bool(body.full_access), nxt,
                                 action="watch", payload=payload)
    _start_scheduler()
    return {"ok": True, "id": sid, "next_run": nxt,
            "describe": scheduler.describe_spec(scheduler.parse_spec(spec_json))}


# --- auto-resume (bounded sweep that nudges stalled tasks forward) ---------- #
class AutoResumeBody(BaseModel):
    enabled: bool | None = None
    idle_minutes: int | None = None
    max_attempts: int | None = None
    max_per_sweep: int | None = None
    full_access: bool | None = None
    cadence: str | None = None


def _ensure_autoresume_schedule(cfg: dict):
    """Keep a single managed schedule in sync with the auto-resume policy."""
    old = cfg.get("schedule_id")
    if old:
        try:
            memory.delete_schedule(int(old))
        except Exception:
            pass
    if not cfg.get("enabled"):
        return None
    cadence = cfg.get("cadence", "hourly")
    if cadence not in scheduler.KINDS:
        cadence = "hourly"
    spec_json = scheduler.make_spec(cadence, time_str="07:00", n=60, dow=0)
    nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    return memory.create_schedule("Auto-resume stuck tasks",
                                  "Resume any stalled task plans within the fence",
                                  spec_json, "Auto", bool(cfg.get("full_access")), nxt,
                                  action="autoresume", payload="{}")


@app.get("/api/autoresume")
def autoresume_status():
    return {**autoresume.status(), "log": autoresume.recent_log(20)}


def _ensure_folderwatch_schedule() -> int | None:
    """One managed schedule that sweeps the watched folders. Exists iff at
    least one enabled folder does; recreated on cadence change."""
    cfg = folderwatch.load_config()
    old = cfg.get("schedule_id")
    if old:
        try:
            memory.delete_schedule(int(old))
        except Exception:
            pass
    if not folderwatch.enabled_folders():
        folderwatch.save_config({"schedule_id": None})
        return None
    cadence = cfg.get("cadence", "hourly")
    if cadence not in scheduler.KINDS:
        cadence = "hourly"
    spec_json = scheduler.make_spec(cadence, time_str="07:00", n=30, dow=0)
    nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    sid = memory.create_schedule("Watch folders — ingest new documents",
                                 "Sweep watched folders into the document store",
                                 spec_json, "Auto", False, nxt,
                                 action="folderwatch", payload="{}")
    folderwatch.save_config({"schedule_id": sid})
    return sid


class FolderBody(BaseModel):
    path: str


class FolderToggleBody(BaseModel):
    path: str
    enabled: bool


@app.get("/api/folders")
def folders_status():
    st = folderwatch.status()
    try:
        st["doc_count"] = rag.get_store().doc_count()
    except Exception:
        st["doc_count"] = None
    return st


@app.post("/api/folders")
def folders_add(body: FolderBody):
    import os as _os
    p = body.path.strip()
    if not p:
        raise HTTPException(status_code=400, detail="Give a folder path.")
    if not _os.path.isdir(_os.path.expanduser(p)):
        raise HTTPException(status_code=400,
                            detail="That folder doesn't exist on this machine.")
    folderwatch.add_folder(p)
    audit.record("config", name="folder_watch_add", detail=p)
    _ensure_folderwatch_schedule()
    _start_scheduler()
    summary = folderwatch.sweep()          # first index right away
    return {**folderwatch.status(), "first_sweep": summary}


@app.post("/api/folders/toggle")
def folders_toggle(body: FolderToggleBody):
    folderwatch.set_enabled(body.path, body.enabled)
    _ensure_folderwatch_schedule()
    return folderwatch.status()


@app.delete("/api/folders")
def folders_remove(path: str):
    folderwatch.remove_folder(path)
    audit.record("config", name="folder_watch_remove", detail=path)
    _ensure_folderwatch_schedule()
    return folderwatch.status()


@app.post("/api/folders/scan")
def folders_scan():
    return {"ok": True, "summary": folderwatch.sweep(),
            **folderwatch.status()}


@app.post("/api/autoresume")
def autoresume_config(body: AutoResumeBody):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    autoresume.save_config(data)
    sid = _ensure_autoresume_schedule(autoresume.load_config())
    autoresume.save_config({"schedule_id": sid})
    _start_scheduler()
    return autoresume.status()


@app.get("/api/autoresume/candidates")
def autoresume_candidates():
    """Preview which stuck tasks a sweep would pick up right now (no side effects)."""
    cfg = autoresume.load_config()
    picked = autoresume.candidates(memory)[:cfg["max_per_sweep"]]
    return {"count": len(picked),
            "tasks": [{"id": t["id"], "title": t["title"],
                       "next_step": (t.get("next_step") or {}).get("description", "")}
                      for t in picked]}


@app.post("/api/autoresume/run")
def autoresume_run():
    cfg = autoresume.load_config()
    if not cfg.get("schedule_id"):
        raise HTTPException(status_code=400, detail="Arm auto-resume first.")
    sch = memory.get_schedule(int(cfg["schedule_id"]))
    if not sch:
        raise HTTPException(status_code=400, detail="Auto-resume schedule missing; re-arm it.")
    threading.Thread(target=_run_schedule, args=(sch,), daemon=True).start()
    return {"ok": True, "message": "Auto-resume sweep started."}


@app.post("/api/autoresume/pause")
def autoresume_pause():
    autoresume.pause()
    cfg = autoresume.load_config()
    if cfg.get("schedule_id"):
        try:
            memory.set_schedule_enabled(int(cfg["schedule_id"]), False)
        except Exception:
            pass
    return autoresume.status()


# --- autonomy dashboard: one view + one master kill switch ------------------ #
def _watch_mode(sch: dict) -> str:
    import json as _json
    try:
        return (_json.loads(sch.get("payload") or "{}").get("mode") or "draft")
    except Exception:
        return "draft"


def _autonomy_overview() -> dict:
    scheds = memory.list_schedules()
    watch = [s for s in scheds if (s.get("action") == "watch")]
    ap_sched = [s for s in scheds if (s.get("action") == "autopilot")]
    ar_cfg = autoresume.load_config()
    ar_sched = [s for s in scheds if s.get("id") == ar_cfg.get("schedule_id")]
    op = outreach.status()
    ar_st = autoresume.status()

    def _timing(rows):
        en = [s for s in rows if s.get("enabled")]
        nexts = [s["next_run"] for s in en if s.get("next_run")]
        lasts = [(s.get("last_run"), s.get("last_status")) for s in rows if s.get("last_run")]
        last = max(lasts, key=lambda x: x[0]) if lasts else (None, None)
        return {"next_run": min(nexts) if nexts else None,
                "last_run": last[0], "last_status": last[1]}

    feed = []
    for e in outreach.recent_log(40):
        txt = f"{e.get('status', '')} → {e.get('to', '')}".strip(" →")
        if e.get("subject"):
            txt += f" · {e.get('subject')}"
        feed.append({"ts": e.get("ts", 0), "source": "outreach", "text": txt})
    for e in autoresume.recent_log(40):
        feed.append({"ts": e.get("ts", 0), "source": "auto-resume",
                     "text": f"{e.get('event', '')} task #{e.get('task', '')} "
                             f"{e.get('title', '')}".strip()})
    for e in watchers.recent_log(40):
        feed.append({"ts": e.get("ts", 0), "source": "watcher",
                     "text": f"{e.get('name', '')}: "
                             f"{'change — ' if e.get('changed') else ''}"
                             f"{e.get('summary', '')}".strip()})
    feed.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return {
        "autopilot": {"armed": op["autonomous_enabled"],
                      "allow_recipients": len(op.get("allowed_recipients", [])),
                      "allow_domains": len(op.get("allowed_domains", [])),
                      "max_per_run": op.get("max_per_run"),
                      "schedules": sum(1 for s in ap_sched if s.get("enabled")),
                      "timing": _timing(ap_sched)},
        "watchers": {"total": len(watch),
                     "enabled": sum(1 for s in watch if s.get("enabled")),
                     "send_mode": sum(1 for s in watch
                                      if s.get("enabled") and _watch_mode(s) == "send"),
                     "timing": _timing(watch)},
        "autoresume": {"armed": ar_st["enabled"], "cadence": ar_st["cadence"],
                       "full_access": ar_st["full_access"],
                       "max_attempts": ar_st["max_attempts"],
                       "timing": _timing(ar_sched)},
        "feed": feed[:40],
    }


@app.get("/api/autonomy")
def autonomy_overview():
    return _autonomy_overview()


class ToggleBody(BaseModel):
    enabled: bool = False


@app.post("/api/watchers/enable-all")
def watchers_enable_all(body: ToggleBody):
    """Enable or disable every watcher schedule at once (the Watchers card toggle)."""
    n = 0
    for s in memory.list_schedules():
        if s.get("action") == "watch":
            nxt = (scheduler.next_run(scheduler.parse_spec(s["spec"]))
                   if body.enabled else None)
            if memory.set_schedule_enabled(s["id"], body.enabled, nxt):
                n += 1
    if body.enabled:
        _start_scheduler()
    return {"ok": True, "count": n, "overview": _autonomy_overview()}


@app.post("/api/autonomy/pause-all")
def autonomy_pause_all():
    """One master kill switch: disarm auto-pilot and auto-resume, and disable every
    autonomous schedule (watchers, scheduled auto-pilot jobs, the resume sweep)."""
    paused = {"autopilot": False, "autoresume": False, "schedules": 0}
    try:
        outreach.pause_autonomous(); paused["autopilot"] = True
    except Exception:
        pass
    try:
        autoresume.pause(); paused["autoresume"] = True
    except Exception:
        pass
    for s in memory.list_schedules():
        if s.get("action") in ("watch", "autopilot", "autoresume") and s.get("enabled"):
            try:
                memory.set_schedule_enabled(s["id"], False)
                paused["schedules"] += 1
            except Exception:
                pass
    return {"ok": True, "paused": paused, "overview": _autonomy_overview()}


class AutopilotScheduleBody(BaseModel):
    name: str = "Outreach auto-pilot"
    subject: str = ""
    body: str = ""
    contacts: list = []
    kind: str = "daily"
    time: str = "07:00"
    n: int = 60
    dow: int = 0


@app.post("/api/email/autopilot/schedule")
def email_autopilot_schedule(body: AutopilotScheduleBody):
    """Create a recurring schedule that runs an auto-pilot outreach job unattended.
    The job still passes through the arm switch + allowlist + caps at fire time."""
    import json as _json
    if body.kind not in scheduler.KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown frequency: {body.kind}")
    if not body.contacts:
        raise HTTPException(status_code=400, detail="Add at least one recipient.")
    try:
        spec_json = scheduler.make_spec(body.kind, time_str=(body.time or "07:00"),
                                        n=int(body.n or 60), dow=int(body.dow or 0))
        nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}")
    payload = _json.dumps({"subject": body.subject, "body": body.body,
                           "contacts": body.contacts})
    prompt = (f"Auto-pilot outreach: {body.subject or '(no subject)'} "
              f"→ {len(body.contacts)} recipient(s)")
    sid = memory.create_schedule(body.name.strip() or "Outreach auto-pilot", prompt,
                                 spec_json, "Auto", False, nxt,
                                 action="autopilot", payload=payload)
    _start_scheduler()
    return {"ok": True, "id": sid, "next_run": nxt,
            "describe": scheduler.describe_spec(scheduler.parse_spec(spec_json))}


@app.post("/api/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    """Transcribe a short audio clip locally with faster-whisper - no audio
    leaves the machine. Returns {text}."""
    if not _stt_ok():
        import sys as _sys
        _why = ""
        try:
            _why = voice.stt_reason()
        except Exception:
            pass
        if getattr(_sys, "frozen", False):
            _fix = ("This packaged .exe was built without voice support. "
                    "Rebuild it with build_exe.bat after installing "
                    "faster-whisper in the build's .venv, or run Agent Jo from "
                    "source (.venv\\Scripts\\python.exe run_web.py) which "
                    "uses your installed packages.")
        else:
            _fix = ("Run `pip install faster-whisper` in the app's virtualenv, "
                    "then restart.")
        raise HTTPException(
            status_code=503,
            detail=("Local speech-to-text isn't available. " + _fix
                    + f" [served by: {_sys.executable}"
                    + (" | frozen .exe" if getattr(_sys, "frozen", False)
                       else "") + "]"
                    + (f" [import error: {_why[:180]}]" if _why else "")))
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="No audio was received.")
    updir = config.AGENT_HOME / "web_uploads"
    updir.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio.filename or "rec.webm").suffix or ".webm"
    tmp = updir / f"voice-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(raw)
    try:
        text = voice.transcribe(str(tmp))
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"Transcription failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    return {"text": (text or "").strip()}


# --- engines --------------------------------------------------------------- #
@app.get("/api/engines")
def list_engines():
    return {"engines": _engine_list()}


@app.post("/api/engines")
def add_engine(body: EngineBody):
    ok, msg = brainmod.add_custom_engine(
        body.name, body.base_url, body.api_key, body.model,
        tools=body.tools, stream=body.stream,
        price_in=body.price_in, price_out=body.price_out)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "engines": _engine_list()}


@app.delete("/api/engines/{name}")
def remove_engine(name: str):
    ok, msg = brainmod.remove_custom_engine(name)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg, "engines": _engine_list()}


# --- settings (same managed keys + settings.json as the desktop app) -------- #
@app.get("/api/settings")
def get_settings():
    return {"settings": config.current_settings()}


@app.post("/api/settings")
def update_settings(updates: dict = Body(default={})):
    _MODEL_KEYS = {"MODEL", "FAST_MODEL", "OLLAMA_MODEL", "OLLAMA_FAST_MODEL"}
    touches_model = bool(_MODEL_KEYS & set((updates or {}).keys()))
    settings = config.save_settings(updates or {})
    audit.record("config", name="settings", detail=",".join(sorted((updates or {}).keys()))[:200])
    if touches_model:
        # the brain caches its model handles — rebuild so the new choice is used
        global _brain
        with _brain_lock:
            _brain = None
    return {"ok": True, "settings": settings, "engine_reloaded": touches_model}


def _web_state_file():
    return config.AGENT_HOME / "web.json"


def _load_persisted_web():
    """Restore the saved web-access choice at startup, if one exists."""
    try:
        import json
        data = json.loads(_web_state_file().read_text("utf-8"))
        if "enabled" in data:
            agent_tools.WEB_ENABLED = bool(data["enabled"])
    except Exception:
        pass


def _save_persisted_web(enabled: bool):
    try:
        import json
        p = _web_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(enabled)}), "utf-8")
    except Exception:
        pass


@app.get("/api/web")
def get_web():
    return {"web": bool(getattr(agent_tools, "WEB_ENABLED", False))}


class WebBody(BaseModel):
    enabled: bool = True


@app.post("/api/web")
def set_web(body: WebBody):
    """Turn the agent's web access (search + fetch) on or off, and remember it."""
    agent_tools.WEB_ENABLED = bool(body.enabled)
    _save_persisted_web(agent_tools.WEB_ENABLED)
    audit.record("config", name="web", status="on" if body.enabled else "off")
    return {"web": agent_tools.WEB_ENABLED}


_load_persisted_web()


try:
    import sys as _bsys
    _in_venv = _bsys.prefix != getattr(_bsys, "base_prefix", _bsys.prefix)
    _frozen = getattr(_bsys, "frozen", False)
    print(f"[agent-jo] serving with: {_bsys.executable}"
          f" ({'frozen .exe' if _frozen else ('venv' if _in_venv else 'NOT a venv')})")
    _v_ok = _stt_ok()
    print(f"[agent-jo] voice input: {'ready' if _v_ok else 'unavailable'}"
          + ("" if _v_ok else f" — {voice.stt_reason()[:160]}"))
except Exception:
    pass


@app.get("/api/teamwork")
def get_teamwork():
    return {"teamwork": bool(config.TEAMWORK)}


# --- MCP servers: connect external tool servers ------------------------------ #
import agent.mcp as agent_mcp


class McpServerBody(BaseModel):
    name: str
    transport: str = "stdio"          # "stdio" | "http"
    command: str = ""                 # stdio: full command line, e.g. "npx -y @modelcontextprotocol/server-github"
    url: str = ""                     # http: endpoint URL
    env: str = ""                     # stdio: KEY=VALUE per line
    headers: str = ""                 # http: Header: value per line
    enabled: bool = True


# Mapping only READS source and never runs it, so the guard is narrow: refuse
# to walk a system root or a whole drive, which would take minutes and tell
# you nothing about your own code.
_SYSTEM_ROOTS = ("/", "/usr", "/etc", "/bin", "/sbin", "/var", "/proc",
                 "/sys", "/dev", "/boot", "/lib", "c:\\", "c:\\windows",
                 "c:\\program files", "c:\\program files (x86)",
                 "c:\\users", "c:\\programdata")


def _unsafe_root(folder: str) -> str:
    from pathlib import Path as _P
    try:
        p = _P(folder).expanduser().resolve()
    except Exception:
        return "that path can't be read."
    if not p.is_dir():
        return f"{folder} isn't a folder."
    low = str(p).lower().rstrip("\\/")
    if low in [r.rstrip("\\/") for r in _SYSTEM_ROOTS] or len(p.parts) <= 2:
        return ("Point this at a project folder rather than a drive or system "
                "directory — walking one takes minutes and tells you nothing "
                "about your own code.")
    return ""


class CodeMapBody(BaseModel):
    folder: str
    module: str = ""


class DataBody(BaseModel):
    path: str = ""
    target: str = ""
    name: str = ""
    drop: list[str] = []
    row: dict = {}


@app.get("/api/models/hardware")
def models_hardware():
    """What the card is, and what it's worth using for."""
    return modelbuild.hardware()


@app.post("/api/models/diagnose")
def models_diagnose(body: DataBody):
    bad = _unsafe_root(str(Path(body.path).expanduser().parent))
    if bad and not Path(body.path).expanduser().is_file():
        raise HTTPException(status_code=403, detail=bad)
    r = dataprep.diagnose(body.path)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/clean")
def models_clean(body: DataBody):
    r = dataprep.clean(body.path, apply_all_safe=True)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/plan")
def models_plan(body: DataBody):
    r = modelbuild.plan(body.path, body.target)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/leaks")
def models_leaks(body: DataBody):
    r = modelbuild.find_leaks(body.path, body.target)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/features")
def models_features(body: DataBody):
    r = (dataprep.build_features(body.path, body.target) if body.target
         else dataprep.propose_features(body.path))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/train")
def models_train(body: DataBody):
    """Detect, de-leak and train in one call, with the gates kept."""
    r = modelbuild.auto(body.path, body.target, name=body.name)
    if not r.get("ok"):
        raise HTTPException(status_code=400,
                            detail=r.get("error", "training failed"))
    return r


@app.post("/api/models/predict")
def models_predict(body: DataBody):
    r = modelbuild.predict(body.name, body.row or body.path)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class EvalBody(BaseModel):
    name: str
    path: str = ""
    threshold: float | None = None


@app.post("/api/models/evaluate")
def models_evaluate(body: EvalBody):
    """Score a model on a file YOU held back — the only fully fair test."""
    r = modelbuild.evaluate_on(body.name, body.path, body.threshold)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/models/threshold")
def models_threshold(body: EvalBody):
    r = modelbuild.threshold_curve(body.name, body.path)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/models/importance")
def models_importance(name: str):
    r = modelbuild.importance(name)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/data/columns")
def data_columns(body: DataBody):
    r = dataprep.columns(body.path)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class ActionsBody(BaseModel):
    path: str
    actions: list[dict] = []


@app.post("/api/data/actions")
def data_actions(body: ActionsBody):
    r = dataprep.apply_actions(body.path, body.actions)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


class FeatureBody(BaseModel):
    path: str
    target: str = ""
    idea: dict = {}


@app.post("/api/data/feature")
def data_feature(body: FeatureBody):
    """Build ONE feature and measure whether it helped."""
    r = dataprep.feature_one(body.path, body.target, body.idea)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.get("/api/models/saved")
def models_saved():
    return {"models": modelbuild.saved(),
            "hardware": modelbuild.hardware()}


@app.get("/api/models/card")
def models_card(name: str):
    r = modelbuild.card(name)
    if r.get("ok") is False:
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@app.post("/api/codemap")
def codemap_scan(body: CodeMapBody):
    """Index a folder and report how its code hangs together."""
    bad = _unsafe_root(body.folder)
    if bad:
        raise HTTPException(status_code=403, detail=bad)
    r = codemap.report(body.folder)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    r["saved_to"] = codemap.save(body.folder, r)
    # the graph itself is large; the panel wants the findings
    r.pop("edges", None)
    return r


@app.post("/api/codemap/diagram")
def codemap_diagram(body: CodeMapBody):
    """Positions for a drawing: layered, and small enough to read."""
    bad = _unsafe_root(body.folder)
    if bad:
        raise HTTPException(status_code=403, detail=bad)
    g = codemap.scan(body.folder)
    if not g.get("ok"):
        raise HTTPException(status_code=400, detail=g["error"])
    return codemap.layout(g, body.module or "", hops=1)


class CodeMapExportBody(BaseModel):
    folder: str
    format: str = "mermaid"
    module: str = ""


@app.post("/api/codemap/export")
def codemap_export(body: CodeMapExportBody):
    bad = _unsafe_root(body.folder)
    if bad:
        raise HTTPException(status_code=403, detail=bad)
    r = codemap.export(body.folder, body.format, body.module)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/codemap/impact")
def codemap_impact(body: CodeMapBody):
    """What would feel a change to one module."""
    g = codemap.scan(body.folder)
    if not g.get("ok"):
        raise HTTPException(status_code=400, detail=g["error"])
    if body.module not in g["modules"]:
        raise HTTPException(status_code=404,
                            detail=f"'{body.module}' isn't in that folder")
    return codemap.impact(g, body.module)


@app.get("/api/mcp/discover")
def mcp_discover(include_stale: bool = False):
    """Search the npm registry for MCP servers available right now."""
    r = mcpdiscover.discover(include_stale=include_stale)
    return {**r, "installed": mcpdiscover.installed_summary()}


class McpInstallBody(BaseModel):
    package: dict


@app.post("/api/mcp/install")
def mcp_install(body: McpInstallBody):
    """Add a discovered server — switched OFF, with its needs listed."""
    r = mcpdiscover.propose_install(body.package or {})
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r["error"])
    return r


@app.get("/api/mcp")
def mcp_list():
    return {"servers": agent_mcp.manager.statuses(),
            "summary": agent_mcp.manager.summary()}


@app.post("/api/mcp")
def mcp_add(body: McpServerBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the server a name.")
    if body.transport == "http":
        if not body.url.strip().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Give a valid URL.")
        headers = {}
        for ln in body.headers.splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip()] = v.strip()
        spec = {"transport": "http", "url": body.url.strip(),
                "headers": headers, "enabled": bool(body.enabled)}
    else:
        parts = body.command.split()
        if not parts:
            raise HTTPException(status_code=400,
                                detail="Give the command that starts the server.")
        env = {}
        for ln in body.env.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
        spec = {"transport": "stdio", "command": parts[0], "args": parts[1:],
                "env": env, "enabled": bool(body.enabled)}
    agent_mcp.manager.add(name, spec)
    audit.record("config", name="mcp_add", detail=name)
    res = agent_mcp.manager.connect_now(name)
    return {"ok": True, "connected": res.get("ok", False),
            "tools": res.get("tools", 0), "error": res.get("error", ""),
            "servers": agent_mcp.manager.statuses()}


class McpToggleBody(BaseModel):
    enabled: bool


@app.post("/api/mcp/{name}/toggle")
def mcp_toggle(name: str, body: McpToggleBody):
    agent_mcp.manager.set_enabled(name, body.enabled)
    return {"ok": True, "servers": agent_mcp.manager.statuses()}


@app.post("/api/mcp/{name}/connect")
def mcp_connect(name: str):
    res = agent_mcp.manager.connect_now(name)
    return {"ok": res.get("ok", False), "error": res.get("error", ""),
            "tools": res.get("tools", 0),
            "servers": agent_mcp.manager.statuses()}


@app.delete("/api/mcp/{name}")
def mcp_delete(name: str):
    agent_mcp.manager.remove(name)
    audit.record("config", name="mcp_remove", detail=name)
    return {"ok": True, "servers": agent_mcp.manager.statuses()}


class TeamworkBody(BaseModel):
    enabled: bool = True


@app.post("/api/teamwork")
def set_teamwork(body: TeamworkBody):
    """Toggle cost-tiered teamwork (local worker does the grunt work). Persists
    like other settings; no engine rebuild needed."""
    config.save_settings({"TEAMWORK": bool(body.enabled)})
    audit.record("config", name="teamwork", status="on" if body.enabled else "off")
    return {"teamwork": bool(config.TEAMWORK)}


@app.post("/api/engines/enable-local")
def enable_local_engines():
    """Turn the locally-installed Ollama models into first-class, removable
    custom engines that connect straight to your local Ollama server — so
    selecting them runs THAT model under any backend, instead of silently
    falling back to Claude. Only registers models Ollama actually reports as
    installed (so you can't create an engine for a model that will just error).
    Idempotent: models already registered are skipped."""
    installed = brainmod.list_ollama_models() or []
    if not installed:
        return {"ok": False, "reachable": False, "added": [], "skipped": [],
                "detail": ("Ollama isn't reachable at "
                           f"{config.OLLAMA_HOST} — start it (and `ollama pull "
                           "<model>`), then try again."),
                "engines": _engine_list()}
    made, skipped = [], []
    existing = {n.lower() for n in brainmod.custom_engine_names()}
    for model in installed:
        if model.lower() in existing or model.lower() in _reserved_lower():
            skipped.append(model)
            continue
        ok, msg = brainmod.register_ollama_engine(model, model)
        (made if ok else skipped).append(model)
    audit.record("config", name="enable_local_engines",
                 detail=",".join(made)[:200])
    return {"ok": True, "reachable": True, "installed": installed,
            "added": made, "skipped": skipped, "engines": _engine_list()}


def _reserved_lower():
    # only the truly fixed engine names — NOT the Ollama model ids, which are
    # exactly what we want to convert into removable custom engines
    return {"auto", "claude", "ollama", "deepseek pro", "deepseek flash"}


@app.get("/api/models")
def list_models():
    """Choices for the model pickers in Settings. Local models come from the live
    Ollama instance (empty list => Ollama not reachable, UI shows a text field);
    cloud models are the known ids plus whatever is configured now."""
    local = brainmod.list_ollama_models()
    cloud = []
    for mid in (config.MODEL, config.FAST_MODEL,
                "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
                "claude-opus-4-6"):
        if mid and mid not in cloud:
            cloud.append(mid)
    return {
        "cloud": cloud,
        "local": local,
        "local_available": bool(local),
        "current": {"MODEL": config.MODEL, "FAST_MODEL": config.FAST_MODEL,
                    "OLLAMA_MODEL": config.OLLAMA_MODEL,
                    "OLLAMA_FAST_MODEL": config.OLLAMA_FAST_MODEL},
    }


@app.post("/api/settings/reset")
def reset_settings_endpoint():
    return {"ok": True, "settings": config.reset_settings()}


# --- documents / RAG (same documents.db + search_documents tool the chat uses) #
class PathBody(BaseModel):
    path: str


def _documents_payload() -> dict:
    store = rag.get_store()
    return {"documents": store.list_documents(),
            "doc_count": store.doc_count(),
            "chunk_count": store.chunk_count(),
            "embed_model": config.EMBED_MODEL}


@app.get("/api/documents")
def documents_list():
    return _documents_payload()


@app.post("/api/documents")
async def documents_upload(file: UploadFile = File(...)):
    store = rag.get_store()
    updir = config.AGENT_HOME / "web_uploads"
    updir.mkdir(parents=True, exist_ok=True)
    dest = updir / Path(file.filename or "upload.txt").name
    dest.write_bytes(await file.read())
    result = store.ingest_path(str(dest))
    return {"ok": "error" not in result, "result": result, **_documents_payload()}


@app.post("/api/documents/survey")
def documents_survey(body: DataBody):
    """What's in a folder, before spending an hour indexing it."""
    r = rag.survey(body.path)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@app.post("/api/documents/path")
def documents_ingest_path(body: PathBody):
    if not body.path.strip():
        raise HTTPException(status_code=400, detail="Provide a file or folder path.")
    result = rag.get_store().ingest_path(body.path.strip())
    return {"ok": "error" not in result, "result": result, **_documents_payload()}


@app.get("/api/documents/search")
def documents_search(q: str = ""):
    if not q.strip():
        return {"results": []}
    rows = rag.get_store().search(q.strip())
    return {"results": [{"source": r.get("source"), "score": r.get("score"),
                         "text": (r.get("text") or "")[:320]} for r in rows]}


@app.delete("/api/documents/{doc_id}")
def documents_remove(doc_id: int):
    if not rag.get_store().remove_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"ok": True, **_documents_payload()}


@app.delete("/api/documents")
def documents_clear():
    removed = rag.get_store().clear()
    return {"ok": True, "removed": removed, **_documents_payload()}


# --- memory & skills (shared agent.db with the desktop app) ---------------- #
class MemoryBody(BaseModel):
    content: str
    category: str = "general"


class SkillBody(BaseModel):
    name: str
    description: str = ""
    instructions: str


def _memory_payload() -> dict:
    return {"memories": memory.all_memories(),
            "skills": memory.get_skills(),
            "memory_count": memory.memory_count(),
            "skill_count": memory.skill_count()}


@app.get("/api/memory")
def memory_list():
    return _memory_payload()


@app.get("/api/memory/search")
def memory_search(q: str = ""):
    if not q.strip():
        return {"memories": []}
    return {"memories": memory.search_memories(q.strip(), limit=50)}


@app.post("/api/memory")
def memory_add(body: MemoryBody):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Memory text is empty.")
    mid = memory.add_memory(body.content.strip(),
                            category=(body.category or "general"))
    if mid is None:
        return {"ok": False,
                "message": "Already remembered (or too short to keep).",
                **_memory_payload()}
    return {"ok": True, "id": mid, **_memory_payload()}


@app.delete("/api/memory/{memory_id}")
def memory_delete(memory_id: int):
    if not memory.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, **_memory_payload()}


@app.post("/api/skills")
def skill_add(body: SkillBody):
    if not body.name.strip() or not body.instructions.strip():
        raise HTTPException(status_code=400,
                            detail="Skill name and instructions are required.")
    replaced = memory.add_skill(body.name.strip(), body.description.strip(),
                                body.instructions.strip())
    return {"ok": True, "replaced": replaced, **_memory_payload()}


@app.delete("/api/skills/{name}")
def skill_delete(name: str):
    if not memory.delete_skill(name):
        raise HTTPException(status_code=404, detail="Skill not found.")
    return {"ok": True, **_memory_payload()}


# --- permissions (always-allow rules for commands & write directories) ------ #
class PermissionBody(BaseModel):
    kind: str
    pattern: str


def _permissions_payload() -> dict:
    perms = memory.list_permissions()
    return {"permissions": perms, "count": len(perms)}


@app.get("/api/permissions")
def permissions_list():
    return _permissions_payload()


@app.post("/api/permissions")
def permissions_add(body: PermissionBody):
    kind = (body.kind or "").strip()
    if kind not in ("command", "write_dir"):
        raise HTTPException(status_code=400,
                            detail="kind must be 'command' or 'write_dir'.")
    if not body.pattern.strip():
        raise HTTPException(status_code=400, detail="A pattern is required.")
    added = memory.add_permission(kind, body.pattern.strip())
    return {"ok": True, "added": added, **_permissions_payload()}


@app.delete("/api/permissions/{perm_id}")
def permissions_delete(perm_id: int):
    if not memory.delete_permission(perm_id):
        raise HTTPException(status_code=404, detail="Permission not found.")
    return {"ok": True, **_permissions_payload()}


# --- tasks (read-only view of the agent's multi-step plans) ----------------- #
@app.get("/api/tasks")
def tasks_list():
    return {"tasks": memory.list_tasks("all", 50)}


@app.post("/api/tasks/{tid}/reset")
def task_reset(tid: int):
    """Restart a task in place (reuse id, keep step notes as history)."""
    if not memory.reset_task_plan(tid):
        raise HTTPException(status_code=404, detail="Task not found.")
    autoresume.reset_task_state(tid)       # fresh attempt budget after a manual restart
    return {"ok": True, "tasks": memory.list_tasks("all", 50)}


# --- scheduler (cron-like prompts that run on their own) -------------------- #
_sched_running: set[int] = set()
_sched_guard = threading.Lock()
_scheduler_started = False


def _run_schedule(sch: dict) -> None:
    """Run one scheduled instruction as a tracked job in its own session, then
    record the outcome and compute the next fire time. Mirrors the desktop."""
    sid = sch["id"]
    with _sched_guard:
        if sid in _sched_running:
            return
        _sched_running.add(sid)
    info: dict = {}
    sess_id = f"sched-{sid}-{int(time.time())}"
    status, summary = "ok", ""
    _bkey = ""
    try:
        action = (sch.get("action") or "prompt")
        # A scheduled job that keeps failing shouldn't keep costing money to
        # fail — the trend scan burned four identical runs before anyone
        # noticed. The breaker refuses cheaply and says why.
        _bkey = f"schedule:{action}"
        _ballowed, _bwhy = breaker.allow(_bkey)
        if not _ballowed:
            status, summary = "skipped", _bwhy
            return
        if action in ("autopilot", "watch", "autoresume") and _budget_blocks_autonomy():
            status, summary = "skipped", (
                f"budget reached (${config.BUDGET_USD:.2f}) — autonomous action held")
            return
        if action == "autopilot":
            import json as _json
            try:
                job = _json.loads(sch.get("payload") or "{}")
            except Exception:
                job = {}
            rep = outreach.run_autopilot(job.get("subject", ""), job.get("body", ""),
                                         job.get("contacts", []), dry_run=False)
            if rep.get("ok"):
                summary = (f"auto-pilot: {rep['sent']} sent"
                           + (f", {rep['blocked']} blocked" if rep.get("blocked") else "")
                           + (f", {rep['skipped']} skipped" if rep.get("skipped") else ""))
            else:
                status, summary = "skipped", f"auto-pilot: {rep.get('error')}"
            return
        if action == "watch":
            import json as _json
            try:
                w = _json.loads(sch.get("payload") or "{}")
            except Exception:
                w = {}
            w["id"] = f"sched-{sid}"
            w["name"] = sch.get("name", "")
            # structured mode when the watcher defines an item selector:
            # reports NEW ITEMS rather than "the page changed", and can
            # re-derive its own selectors when the site alters its markup
            if w.get("item_selector"):
                res = watchers.check_structured(w, brain=get_brain())
                if res.get("ok") and res.get("healed"):
                    watchers.record_run(
                        sch.get("name", ""), True,
                        "selectors self-healed: " + res["summary"][:200])
                if res.get("needs_attention"):
                    status = "error"
                    summary = ("watch: " + res.get("error", "")
                              + " — this watcher needs a look")
                    watchers.record_run(sch.get("name", ""), False, summary)
                    return
                if res.get("ok"):
                    res = {**res, "new_text": _json.dumps(
                        res.get("new", []), indent=2)[:4000]}
            else:
                res = watchers.check(w)
            if not res.get("ok"):
                # A watcher whose host has gone will fail every hour for ever,
                # and its failures trip the breaker that guards every OTHER
                # watcher. Count identical failures and pause it once it's
                # clear the site isn't coming back.
                _wname = sch.get("name", "")
                _st = watchers.note_failure(_wname, res.get("error", ""))
                status, summary = "error", f"watch: {res.get('error')}"
                if _st.get("paused"):
                    try:
                        memory.set_schedule_enabled(sid, False)
                    except Exception:
                        pass
                    summary = (f"watch: {_st['reason']} — this schedule has "
                               f"been paused. Fix the address and re-enable "
                               f"it in the Schedules panel.")
                watchers.record_run(_wname, False, summary)
                return
            watchers.note_success(sch.get("name", ""))
            if not res.get("changed"):
                summary = f"watch: {res.get('summary')}"
                watchers.record_run(sch.get("name", ""), False, res.get("summary", ""))
                return
            # a real change — let the agent decide what to do (it can send only
            # within the auto-pilot fence, via send_email's allowlist/arm checks)
            brain = get_brain()
            prompt = watchers.build_agent_prompt(w, res["summary"], res["new_text"])
            try:
                memory.log_message(sess_id, "user", f"[watch: {sch['name']}] change detected")
            except Exception:
                pass
            observe = (w.get("mode") or "draft") != "send"
            if observe:
                outreach.set_draft_only(True)   # observe mode: agent may draft, never deliver
            try:
                reply = main.run_turn(
                    brain, memory, [], prompt,
                    auto_approve=bool(sch["full_access"]), session_id=sess_id,
                    force_model=_force_model(sch["engine"]), turn_info=info)
            finally:
                if observe:
                    outreach.set_draft_only(False)
            if reply:
                try:
                    memory.log_message(sess_id, "assistant", reply)
                except Exception:
                    pass
            _tag = "observed" if observe else "acted"
            summary = f"change detected ({_tag}) — " + (" ".join((reply or "").split())[:240] or "(handled)")
            watchers.record_run(sch.get("name", ""), True, summary)
            return
        if action == "jobscout":
            if jobscout.auto_config().get("enabled"):
                # find first, then apply — without sourcing, an unattended
                # run could only work through roles someone had already added
                _ja = jobscout.auto_cycle(get_brain())
                if _ja.get("ok"):
                    status = "ok"
                    summary = (f"found {_ja.get('discovered', 0)} new role(s); "
                              f"{len(_ja.get('sent') or [])} sent"
                              + (" (dry run)" if _ja.get("dry_run") else "")
                              + f", {len(_ja.get('held') or [])} held for you")
                    return
            _jsum = jobscout.summary()
            status = "ok"
            summary = (f"{_jsum['total']} role(s) tracked; "
                      f"{_jsum['by_stage'].get('found', 0)} unreviewed; "
                      f"{_jsum['follow_ups']} awaiting follow-up. "
                      f"Nothing is ever sent automatically.")
            return

        if action == "backup":
            try:
                _bp = backupmod.create_backup(
                    memory, rag.get_store(), note="scheduled nightly backup")
                status = "ok"
                summary = f"backed up to {os.path.basename(_bp)}"
            except Exception as exc:
                status = "error"
                summary = f"backup failed: {type(exc).__name__}: {exc}"
            return

        if action == "crew":
            try:
                pl = json.loads(sch.get("payload") or "{}")
            except Exception:
                pl = {}
            _cm = pl.get("member", "")
            _res = crew.run(_cm, pl.get("task", ""), get_brain(), memory,
                            console, session_id=f"crew-{_cm}")
            status = "ok" if _res.get("ok") else "error"
            summary = (_res.get("report") or _res.get("error", ""))[:400]
            return

        if action == "trendscout":
            _tmodel, _twhy = _trend_model(
                trendscout.load_config().get("engine", ""))
            if _twhy:
                status, summary = "error", _twhy[:300]
                return
            rep = trendscout.scan_and_digest(get_brain(), model=_tmodel)
            if rep.get("error"):
                status, summary = "error", rep["error"][:300]
            elif rep.get("no_new"):
                status, summary = "ok", "no new items since last scan"
            else:
                status = "ok"
                summary = (f"{rep.get('item_count', 0)} new items → "
                          f"{len(rep.get('trends', []))} trend(s); review "
                          f"in the Trends panel")
                try:
                    memory.add_memory(
                        "Trend scout: " + "; ".join(
                            t["title"] for t in rep.get("trends", [])[:5]),
                        "trend")
                except Exception:
                    pass
            return

        if action == "folderwatch":
            s = folderwatch.sweep()
            if s["errors"]:
                status, summary = "error", "; ".join(s["errors"])[:300]
            else:
                status, summary = "ok", (f"{s['added']} added, "
                                         f"{s['updated']} updated, "
                                         f"{s['skipped']} unchanged")
            return

        if action == "autoresume":
            ar_cfg = autoresume.load_config()
            brain = get_brain()

            def _resume_runner(task):
                p = autoresume.build_prompt(task)
                try:
                    memory.log_message(sess_id, "user", f"[auto-resume] task #{task['id']}")
                except Exception:
                    pass
                rep = main.run_turn(
                    brain, memory, [], p,
                    auto_approve=bool(ar_cfg.get("full_access")), session_id=sess_id,
                    force_model=_force_model(sch["engine"]), turn_info=info)
                if rep:
                    try:
                        memory.log_message(sess_id, "assistant", rep)
                    except Exception:
                        pass
                return rep

            rep = autoresume.sweep(memory, _resume_runner)
            if not rep.get("ok"):
                status, summary = "skipped", f"auto-resume: {rep.get('error')}"
            else:
                summary = (f"auto-resume: {rep['resumed']} resumed "
                           f"({rep['progressed']} progressed, {rep['stalled']} stalled)")
            return
        brain = get_brain()
        try:
            memory.log_message(sess_id, "user",
                               f"[scheduled: {sch['name']}] {sch['prompt']}")
        except Exception:
            pass
        reply = main.run_turn(
            brain, memory, [], sch["prompt"],
            auto_approve=bool(sch["full_access"]), session_id=sess_id,
            force_model=_force_model(sch["engine"]), turn_info=info)
        summary = " ".join((reply or "").split())[:300] or "(no output)"
        if reply:
            try:
                memory.log_message(sess_id, "assistant", reply)
            except Exception:
                pass
    except Exception as exc:
        status, summary = "error", f"{type(exc).__name__}: {exc}"[:300]
        issues.note_error(f"schedule:{sch.get('name', '')[:20]}", summary)
    finally:
        # feed the outcome back to the breaker for this action type. "skipped"
        # is neither success nor failure — it never ran.
        try:
            if _bkey and status not in ("skipped",):
                if status == "error":
                    breaker.record_failure(_bkey, summary)
                else:
                    breaker.record_success(_bkey)
        except Exception:
            pass
        try:
            audit.record("autonomy", action=action, name=sch.get("name", ""),
                         status=status, summary=summary)
        except Exception:
            pass
        try:
            nxt = scheduler.next_run(scheduler.parse_spec(sch["spec"]))
        except Exception:
            nxt = None
        try:
            memory.schedule_ran(sid, time.time(), nxt, status, summary)
        except Exception:
            pass
        with _sched_guard:
            _sched_running.discard(sid)


def _scheduler_loop() -> None:
    while True:
        time.sleep(15)
        try:
            for sch in memory.due_schedules(time.time()):
                threading.Thread(target=_run_schedule, args=(sch,),
                                 daemon=True).start()
        except Exception:
            pass


def _start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()


@app.on_event("startup")
def _on_startup():
    # a MODEL saved as something Anthropic can't run fails every turn; fix
    # it on the way up rather than making someone find it from a 404
    try:
        _rep = config.repair_model()
        if _rep.get("changed"):
            console.print(f"[yellow]· {_rep['note']}[/yellow]")
            issues.note_error("startup", _rep["note"])
    except Exception:
        pass
    auth.bootstrap_from_env()
    _start_scheduler()


# --- authentication (optional password protection) -------------------------- #
class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    new_password: str
    current_password: str | None = None


def _session_cookie(resp: JSONResponse) -> JSONResponse:
    resp.set_cookie(auth.COOKIE, auth.make_token(), httponly=True,
                    samesite="lax", max_age=30 * 24 * 3600, path="/")
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request):
    enabled = auth.is_enabled()
    authed = (not enabled) or auth.verify_token(request.cookies.get(auth.COOKIE, ""))
    return {"enabled": enabled, "authenticated": authed}


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request):
    if not auth.is_enabled():
        return {"ok": True, "enabled": False}
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    _login_rl.reset(_client_key(request))      # success clears this client's tally
    return _session_cookie(JSONResponse({"ok": True}))


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/auth/password")
def auth_password(body: PasswordBody):
    # When protection is already on, the guard has verified the session; we still
    # require the current password so a borrowed session can't silently change it.
    if auth.is_enabled():
        if not body.current_password or not auth.verify_password(body.current_password):
            raise HTTPException(status_code=403, detail="Current password is incorrect.")
    try:
        auth.set_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _session_cookie(JSONResponse({"ok": True, "enabled": True}))


@app.post("/api/auth/disable")
def auth_disable(body: LoginBody):
    if auth.is_enabled() and not auth.verify_password(body.password):
        raise HTTPException(status_code=403, detail="Incorrect password.")
    auth.disable()
    resp = JSONResponse({"ok": True, "enabled": False})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


def _schedule_view(s: dict) -> dict:
    out = dict(s)
    try:
        out["describe"] = scheduler.describe_spec(scheduler.parse_spec(s["spec"]))
    except Exception:
        out["describe"] = s.get("spec", "")
    return out


def _schedules_payload() -> dict:
    return {"schedules": [_schedule_view(s) for s in memory.list_schedules()]}


class ScheduleBody(BaseModel):
    name: str
    prompt: str
    kind: str = "daily"        # minutes | hourly | daily | weekdays | weekly
    time: str = "07:00"
    n: int = 60
    dow: int = 0
    engine: str = "Auto"
    full_access: bool = False


@app.get("/api/schedules")
def schedules_list():
    return _schedules_payload()


@app.post("/api/schedules")
def schedule_create(body: ScheduleBody):
    if not body.name.strip() or not body.prompt.strip():
        raise HTTPException(status_code=400,
                            detail="A name and an instruction are both required.")
    if body.kind not in scheduler.KINDS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown frequency: {body.kind}")
    try:
        spec_json = scheduler.make_spec(
            body.kind, time_str=(body.time or "07:00"),
            n=int(body.n or 60), dow=int(body.dow or 0))
        nxt = scheduler.next_run(scheduler.parse_spec(spec_json))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}")
    sid = memory.create_schedule(body.name.strip(), body.prompt.strip(),
                                 spec_json, body.engine or "Auto",
                                 bool(body.full_access), nxt)
    _start_scheduler()
    return {"ok": True, "id": sid, "next_run": nxt, **_schedules_payload()}


@app.post("/api/schedules/{sid}/toggle")
def schedule_toggle(sid: int):
    sch = memory.get_schedule(sid)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    if sch["enabled"]:
        memory.set_schedule_enabled(sid, False, None)
    else:
        try:
            nxt = scheduler.next_run(scheduler.parse_spec(sch["spec"]))
        except Exception:
            nxt = None
        memory.set_schedule_enabled(sid, True, nxt)
    return {"ok": True, **_schedules_payload()}


@app.post("/api/schedules/{sid}/run")
def schedule_run_now(sid: int):
    sch = memory.get_schedule(sid)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    threading.Thread(target=_run_schedule, args=(sch,), daemon=True).start()
    return {"ok": True, "message": f"Running '{sch['name']}' now."}


@app.post("/api/schedules/{sid}/preview")
def schedule_preview(sid: int):
    """Show what the next fire *would* do, without side effects: auto-pilot jobs
    are dry-run (rendered, who's allowed vs blocked, nothing sent); watchers fetch
    and compare without consuming the change or running the agent."""
    sch = memory.get_schedule(sid)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    import json as _json
    action = sch.get("action") or "prompt"
    try:
        job = _json.loads(sch.get("payload") or "{}")
    except Exception:
        job = {}
    if action == "autopilot":
        rep = outreach.run_autopilot(job.get("subject", ""), job.get("body", ""),
                                     job.get("contacts", []), dry_run=True)
        return {"action": "autopilot", "ok": True,
                "would_send": rep.get("sent", 0), "blocked": rep.get("blocked", 0),
                "skipped": rep.get("skipped", 0), "total": rep.get("total", 0),
                "results": rep.get("results", [])[:50]}
    if action == "watch":
        w = dict(job); w["id"] = f"sched-{sid}"; w["name"] = sch.get("name", "")
        res = watchers.check(w, persist=False)        # don't consume the change
        if not res.get("ok"):
            return {"action": "watch", "ok": False, "error": res.get("error")}
        mode = (job.get("mode") or "draft")
        return {"action": "watch", "ok": True, "changed": res.get("changed"),
                "first_run": res.get("first_run"), "summary": res.get("summary"),
                "mode": mode,
                "note": ("Would hand this change to the agent in "
                         + ("observe mode (draft only — nothing sent)." if mode != "send"
                            else "send mode (may send within the allowlist/arm fence).")
                         if res.get("changed") else
                         "No change — the agent would not run.")}
    return {"action": "prompt", "ok": True,
            "note": "This is a prompt schedule; it would run its instruction through the agent."}


@app.delete("/api/schedules/{sid}")
def schedule_delete(sid: int):
    if not memory.delete_schedule(sid):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"ok": True, **_schedules_payload()}


# --- chat (streaming, with optional file/image uploads) -------------------- #
_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}


def _image_block_bytes(raw: bytes, ext: str):
    """Build an Anthropic image content block, downscaling oversized images when
    Pillow is available (it's optional; without it the bytes pass through)."""
    media = _IMAGE_MEDIA.get(ext, "image/png")
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(raw))
        fmt = (im.format or "").upper()
        media = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif",
                 "WEBP": "image/webp"}.get(fmt, media)
        long_edge = max(im.size)
        if long_edge > 1568 or len(raw) > 4_500_000:
            scale = min(1.0, 1568 / long_edge)
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)),
                                max(1, int(im.height * scale))))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            raw = buf.getvalue()
            media = "image/jpeg"
    except Exception:
        pass
    import base64
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(raw).decode()}}


async def _process_uploads(files):
    """Split uploads into vision image blocks and prepended attachment text."""
    images, chunks, names = [], [], []
    updir = config.AGENT_HOME / "web_uploads"
    updir.mkdir(parents=True, exist_ok=True)
    for f in files or []:
        if not f or not f.filename:
            continue
        raw = await f.read()
        name = Path(f.filename).name
        names.append(name)
        ext = Path(name).suffix.lower()
        if ext in _IMAGE_MEDIA:
            block = _image_block_bytes(raw, ext)
            if block:
                images.append(block)
        else:
            dest = updir / name
            dest.write_bytes(raw)
            try:
                text, _note = agent_files.extract_text(str(dest))
            except Exception:
                text = None
            if text:
                chunks.append(f"===== FILE: {name} =====\n{text}\n===== END FILE =====")
    attachments = ""
    if chunks:
        attachments = ("The user attached the following file(s). Use their "
                       "contents to answer.\n\n" + "\n\n".join(chunks) + "\n\n")
    return images, attachments, names


@app.post("/api/chat")
async def chat(
    message: str = Form(""),
    conversation_id: str = Form(""),
    engine: str = Form("Auto"),
    full_access: str = Form("false"),
    second_opinion: str = Form("false"),
    files: list[UploadFile] = File(default=[]),
):
    if not message.strip() and not files:
        raise HTTPException(status_code=400, detail="Message is empty.")
    if not _engine_configured():
        raise HTTPException(
            status_code=503,
            detail="No engine is configured. Set an API key (or a local "
                   "backend) and reload.")
    cid = conversation_id or ("web-" + uuid.uuid4().hex)
    messages = _messages_for(cid)
    brain = get_brain()
    force = _force_model(engine)
    if force is main._LOCAL_UNAVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=(f"'{engine}' is a local model, but no local model is "
                    f"running to serve it. Either start Ollama and switch the "
                    f"app to the hybrid backend, or add '{engine}' as a custom "
                    f"engine (MCP-style) so it connects directly — the engine "
                    f"picker's 'Make local models selectable' does this for "
                    f"you. (Refusing rather than silently answering with "
                    f"Claude.)"))
    fa = str(full_access).lower() in ("true", "1", "yes", "on")
    _msg_l = message.lower()
    second = (str(second_opinion).lower() in ("true", "1", "yes", "on")
              or "second opinion" in _msg_l
              or "double-check this" in _msg_l or "double check this" in _msg_l)

    images, attachments, _names = await _process_uploads(files)
    if images and force is main._AUTO:
        # No explicit engine pinned — Auto's smart-routing reasonably prefers
        # a vision-capable engine (Claude) for images. But if the user
        # explicitly picked an engine (a custom one, a local model, etc.),
        # that choice must be respected — silently swapping to Claude behind
        # their back is exactly what caused unexpected Anthropic billing to
        # fire while the UI still showed the custom engine as active.
        force = None

    # persist the user turn up-front so the conversation survives a refresh
    user_log = message.strip() or ("[" + ", ".join(_names) + "]" if _names else "[attachment]")
    try:
        memory.log_message(cid, "user", user_log)
        _touch_conversation(cid, first_user_text=user_log)
    except Exception:
        pass

    q: queue.Queue = queue.Queue()
    turn_info: dict = {}
    state = {"reply": None, "error": None}
    with _cancel_lock:
        _cancel_flags.pop(cid, None)          # clear any stale stop request

    def on_text(tok):
        q.put(("token", tok))

    def on_status(phase):
        q.put(("status", phase))

    def _cancelled():
        with _cancel_lock:
            return bool(_cancel_flags.get(cid))

    def worker():
        try:
            # --- Turbo: draft locally, escalate only if the draft fails ---
            # NOTE: this runs inside a generator, so assigning to `message`
            # here would make it local to this function and every read of it
            # would raise UnboundLocalError — including on the ordinary path
            # where turbo never runs. Work on a copy instead.
            _msg = message
            _turbo_on = False
            _turbo_force = force
            # ONE decision. Routing and Turbo were asking the same
            # question — "can a small model do this?" — with two classifiers
            # over the same variable, so whichever wrote `force` last
            # silently disabled the other. Turbo is now what it always was
            # underneath: the escalation policy for work routed local.
            _turbo_route = None
            _plan = None
            # NOT `force = ...`: assigning the enclosing function's parameter
            # inside this generator makes Python treat it as local for the
            # whole generator, so the read above it raises UnboundLocalError.
            _force_use = force
            if force is main._AUTO and not images and not second:
                try:
                    _rinv = engines.inventory()
                    _worth, _wwhy = routing.worth_routing(_rinv)
                    if _worth:
                        _plan = routing.plan(
                            _msg, has_attachments=bool(attachments),
                            inventory=_rinv,
                            turbo_on=bool(config.TURBO))
                        routing.log_decision(_plan)
                        console.print("[dim]· "
                                      + routing.describe_plan(_plan)
                                      + "[/dim]")
                        _tok = _force_model(_plan["engine"])
                        if _tok is main._LOCAL_UNAVAILABLE:
                            _plan = None        # the engine isn't usable here
                        elif _plan.get("escalate"):
                            # local first, with the gate deciding whether to
                            # escalate — the Turbo path, chosen by the rules
                            _turbo_on, _turbo_force = True, _tok
                            _turbo_route = {"category": _plan["category"],
                                            "try_local": True}
                        else:
                            _force_use = _tok   # settled: no second guess
                except Exception:
                    _plan = None        # routing is an optimisation, never a
                                        # reason a turn can't happen
            try:
                if _turbo_on:
                    _draft = main.run_turn(
                        brain, memory, list(messages), _msg,
                        auto_approve=fa, session_id=cid,
                        on_text=None, force_model=_turbo_force,
                        turn_info=turn_info, on_status=on_status,
                        should_cancel=_cancelled, attachments=attachments)
                    _g = turbo.gate(_draft)
                    if _g["ok"]:
                        # the cheap engine handled it; nothing was billed
                        on_text(_draft)
                        state["reply"] = _draft
                        turbo.record(True,
                                     cloud_cost_estimate=_est_turn_cost(
                                         turn_info),
                                     category=(_turbo_route or {}).get(
                                         "category", ""))
                        raise _TurboDone()
                    on_status and on_status(
                        "local draft fell short — escalating")
                    _msg = (turbo.escalation_prompt(_draft, _g["reasons"])
                            + "\n\nUSER REQUEST:\n" + _msg)
                    turbo.record(False, reasons=_g["reasons"],
                                 cloud_cost_estimate=_est_turn_cost(turn_info),
                                 category=(_turbo_route or {}).get(
                                     "category", ""))
                state["reply"] = main.run_turn(
                    brain, memory, messages, _msg,
                    auto_approve=fa, session_id=cid,
                    on_text=on_text, force_model=_force_use,
                    turn_info=turn_info, on_status=on_status,
                    should_cancel=_cancelled,
                    images=(images or None), attachments=attachments,
                    second_opinion=second)
            except _TurboDone:
                pass
            except Exception as _exc:
                # Seamless failover: if the chosen engine can't answer for a
                # reason another engine could survive — no credit, rate limit,
                # unreachable — run it on the other kind instead of handing
                # back an error. Silence would be worse than the error, so the
                # reply says which engine actually answered and why.
                _alt = engines.fallback_for(
                    engine if engine and engine != "Auto" else "Claude",
                    f"{type(_exc).__name__}: {_exc}")
                if not _alt:
                    raise
                _note = f"_[{_alt['why']}]_\n\n"
                on_text(_note)
                _altforce = _force_model(_alt["engine"])
                if _altforce is main._LOCAL_UNAVAILABLE:
                    raise
                state["reply"] = _note + main.run_turn(
                    brain, memory, messages, _msg,
                    auto_approve=fa, session_id=cid,
                    on_text=on_text, force_model=_altforce,
                    turn_info=turn_info, on_status=on_status,
                    should_cancel=_cancelled,
                    images=(images or None), attachments=attachments,
                    second_opinion=False)
                try:
                    audit.record("engine", name="failover",
                                 detail=_alt["engine"],
                                 summary=_alt["why"][:200])
                except Exception:
                    pass
            if state["reply"]:
                try:
                    memory.log_message(cid, "assistant", state["reply"])
                    _touch_conversation(cid)
                except Exception:
                    pass
                # learn from the exchange the same way the CLI does, but off the
                # critical path so it never delays the reply the user already saw
                if config.AUTO_LEARN:
                    def _learn(u_text, a_text):
                        try:
                            main.auto_learn(brain, memory, u_text, a_text)
                        except Exception:
                            pass
                    threading.Thread(target=_learn, args=(message, state["reply"]),
                                     daemon=True).start()
            try:
                _accumulate_cost(turn_info)
            except Exception:
                pass
        except Exception as exc:           # never leak a raw traceback to the UI
            state["error"] = f"{type(exc).__name__}: {exc}"
            issues.note_error("chat", state["error"])
        finally:
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        yield _sse({"type": "start", "conversation_id": cid})
        start = time.time()
        last_real = start
        # escalating "still working" notices after periods of total silence
        thresholds = [20, 45, 90, 150]
        notified = 0
        try:
            while True:
                try:
                    kind, payload = q.get(timeout=2.0)
                except queue.Empty:
                    now = time.time()
                    # heartbeat keeps the SSE stream warm and feeds the client's
                    # elapsed timer so a long step never looks like a dead app
                    yield _sse({"type": "ping", "elapsed": int(now - start)})
                    silent = now - last_real
                    if notified < len(thresholds) and silent >= thresholds[notified]:
                        notified += 1
                        yield _sse({"type": "status",
                                    "phase": f"still working… ({int(silent)}s on this step)"})
                    continue
                last_real = time.time()
                notified = 0                 # progress resumed: reset stall clock
                if kind == "token":
                    yield _sse({"type": "token", "text": payload})
                elif kind == "status":
                    yield _sse({"type": "status", "phase": payload})
                elif kind == "done":
                    break
            if state["error"]:
                yield _sse({"type": "error", "message": state["error"]})
            else:
                yield _sse({"type": "done", "reply": state["reply"],
                            "engine": turn_info.get("engine"),
                            "model": turn_info.get("model"),
                            "escalations": turn_info.get("escalations"),
                            "usage": turn_info.get("usage")})
        finally:
            with _cancel_lock:
                _cancel_flags.pop(cid, None)

    return StreamingResponse(stream(), media_type="text/event-stream")


class CancelBody(BaseModel):
    conversation_id: str = ""


@app.post("/api/chat/cancel")
def chat_cancel(body: CancelBody):
    """Signal an in-flight turn to stop at its next checkpoint (between model
    steps / tool calls). The turn ends with a '(Stopped.)' note."""
    cid = (body.conversation_id or "").strip()
    if cid:
        with _cancel_lock:
            _cancel_flags[cid] = True
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def clear_conversation(cid: str):
    _ensure_web_tables()
    with _conv_lock:
        _conversations.pop(cid, None)
    try:
        memory.delete_session(cid)
    except Exception:
        pass
    memory.conn.execute("DELETE FROM session_meta WHERE session_id = ?", (cid,))
    memory.conn.commit()
    return {"ok": True}


# --- persisted conversations & projects ------------------------------------- #
class ProjectBody(BaseModel):
    name: str


class AssignBody(BaseModel):
    project_id: int | None = None


class RenameBody(BaseModel):
    title: str


@app.get("/api/projects")
def projects_list():
    _ensure_web_tables()
    rows = memory.conn.execute(
        "SELECT p.id, p.name, p.created_at, "
        "(SELECT COUNT(*) FROM session_meta s WHERE s.project_id = p.id) AS count "
        "FROM projects p ORDER BY p.name COLLATE NOCASE").fetchall()
    return {"projects": [dict(r) for r in rows]}


@app.post("/api/projects")
def project_create(body: ProjectBody):
    _ensure_web_tables()
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="A project name is required.")
    cur = memory.conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?)",
        (body.name.strip()[:80], time.time()))
    memory.conn.commit()
    return {"ok": True, "id": cur.lastrowid, **projects_list()}


@app.delete("/api/projects/{pid}")
def project_delete(pid: int):
    _ensure_web_tables()
    memory.conn.execute(
        "UPDATE session_meta SET project_id = NULL WHERE project_id = ?", (pid,))
    cur = memory.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    memory.conn.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"ok": True, **projects_list()}


@app.get("/api/conversations")
def conversations_list(project: str = "all"):
    return {"conversations": _conversation_list(project)}


@app.get("/api/conversations/search")
def conversations_search(q: str, project: str = "all", limit: int = 40):
    """Find a conversation by what was said in it, not just its title.

    Titles are generated and often generic, so searching them alone misses
    the conversation you actually remember — you remember a phrase from it,
    not what it ended up being called.
    """
    _ensure_web_tables()
    term = (q or "").strip()
    if len(term) < 2:
        return {"results": [], "note": "Type at least two characters."}
    like = f"%{term}%"
    where, args = [], []
    if project == "none":
        where.append("sm.project_id IS NULL")
    elif project not in ("all", ""):
        where.append("sm.project_id = ?")
        args.append(int(project))
    clause = (" AND " + " AND ".join(where)) if where else ""

    rows = memory.conn.execute(
        "SELECT sm.session_id, sm.project_id, sm.title, sm.updated_at, "
        "       m.role, m.content "
        "FROM session_meta sm "
        "JOIN messages m ON m.session_id = sm.session_id "
        "WHERE m.content LIKE ?" + clause + " "
        "ORDER BY sm.updated_at DESC",
        (like, *args)).fetchall()

    seen, out = {}, []
    for r in rows:
        sid = r["session_id"]
        if sid in seen:
            seen[sid]["hits"] += 1
            continue
        # the line it was found in, trimmed around the match, so you can
        # recognise the conversation without opening it
        text = r["content"] or ""
        i = text.lower().find(term.lower())
        start = max(0, i - 60)
        snippet = ("…" if start else "") + text[start:i + len(term) + 90].strip()
        entry = {"id": sid, "project_id": r["project_id"],
                 "title": r["title"] or "Conversation",
                 "updated_at": r["updated_at"], "hits": 1,
                 "who": r["role"], "snippet": snippet + ("…" if len(text) > i + len(term) + 90 else "")}
        seen[sid] = entry
        out.append(entry)
        if len(out) >= limit:
            break

    titles = memory.conn.execute(
        "SELECT session_id, project_id, title, updated_at FROM session_meta "
        "WHERE title LIKE ?" + clause.replace("sm.", "") + " "
        "ORDER BY updated_at DESC LIMIT 20", (like, *args)).fetchall()
    for r in titles:
        if r["session_id"] in seen:
            continue
        out.append({"id": r["session_id"], "project_id": r["project_id"],
                    "title": r["title"] or "Conversation",
                    "updated_at": r["updated_at"], "hits": 0,
                    "who": "", "snippet": "(matched the title)"})
    return {"results": out[:limit], "term": term,
            "searched": "all conversations" if project == "all"
                        else "this group"}


@app.get("/api/conversations/{cid}")
def conversation_get(cid: str):
    _ensure_web_tables()
    meta = memory.conn.execute(
        "SELECT project_id, title FROM session_meta WHERE session_id = ?",
        (cid,)).fetchone()
    msgs = [{"role": m["role"], "text": m["content"]}
            for m in memory.get_transcript(cid)
            if m["role"] in ("user", "assistant")]
    return {"id": cid,
            "title": meta["title"] if meta else "Conversation",
            "project_id": meta["project_id"] if meta else None,
            "messages": msgs}


@app.post("/api/conversations/{cid}/project")
def conversation_assign(cid: str, body: AssignBody):
    _touch_conversation(cid)
    memory.conn.execute(
        "UPDATE session_meta SET project_id = ? WHERE session_id = ?",
        (body.project_id, cid))
    memory.conn.commit()
    return {"ok": True}


@app.post("/api/conversations/{cid}/rename")
def conversation_rename(cid: str, body: RenameBody):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="A title is required.")
    _touch_conversation(cid)
    memory.conn.execute(
        "UPDATE session_meta SET title = ? WHERE session_id = ?",
        (body.title.strip()[:80], cid))
    memory.conn.commit()
    return {"ok": True}


# --- static frontend ------------------------------------------------------- #
@app.get("/avatar")
def avatar():
    for p in (PROJECT_ROOT / "agent_avatar.png", STATIC / "agent_avatar.png"):
        if p.exists():
            return FileResponse(p)
    return JSONResponse({"error": "no avatar"}, status_code=404)


@app.get("/")
def index():
    """Serve the shell with build-stamped asset URLs.

    Replacing files on disk doesn't stop a browser serving a cached
    app.js/styles.css — which produced the worst class of bug here: the new
    HTML paints a new button while the OLD script runs, so the button exists
    and does nothing, and every diagnosis chases the wrong thing. Stamping the
    URLs with the build id makes a stale asset impossible: new build, new URL.
    """
    try:
        html = (STATIC / "index.html").read_text("utf-8")
        v = str(getattr(config, "BUILD_ID", "dev")).replace(" ", "").replace(
            ":", "")
        html = html.replace("/static/styles.css", f"/static/styles.css?v={v}")
        html = html.replace("/static/app.js", f"/static/app.js?v={v}")
        return HTMLResponse(html)
    except Exception:
        return FileResponse(STATIC / "index.html")


@app.get("/api/version")
def api_version():
    return {"build": getattr(config, "BUILD_ID", "unknown")}


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
