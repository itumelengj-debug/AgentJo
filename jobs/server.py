"""Agent Jo Jobs — the job search, on its own.

Split out of the main app because it had become a different product living
inside another one. Someone running a job search wants a pipeline, a list of
roles and a stack of drafts; they do not want a code map, a 3D lab and an MCP
panel in the way. And someone using the agent for work does not want their
sidebar carrying a job hunt.

It is a separate application, not a copy. Every module it uses —
`agent.jobscout`, `agent.boards`, `agent.cv`, `agent.portal`,
`agent.outcomes` — is imported from the same place the main app used it, so
there is one implementation of the fabrication check, one auto-apply engine,
one definition of what "held" means. Two copies would drift, and the half
that governs whether an application goes out is not a half to let drift.

Shared data, shared engines, shared audit trail. Different window.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request           # noqa: E402
from fastapi.responses import FileResponse, JSONResponse      # noqa: E402
from fastapi.staticfiles import StaticFiles                   # noqa: E402
from pydantic import BaseModel                                # noqa: E402

from agent import config                                      # noqa: E402
import agent.jobscout as jobscout                             # noqa: E402
import agent.boards as boards                                 # noqa: E402
import agent.jobalerts as jobalerts                           # noqa: E402
import agent.cv as cvmod                                      # noqa: E402
import agent.portal as portal                                 # noqa: E402
import agent.outcomes as outcomes                             # noqa: E402
import agent.brain as brainmod                                # noqa: E402
from agent.brain import make_brain, EngineNotConfigured       # noqa: E402
from agent.memory import MemoryStore                          # noqa: E402
import agent.scheduler as scheduler                           # noqa: E402

APP_NAME = "Agent Jo Jobs"
STATIC = ROOT / "web_jobs"

app = FastAPI(title=APP_NAME)

_brain = None
memory = None
# the same scheduler module the main app uses, over the same store, so a
# daily run set up here is the same daily run — two schedulers over one
# store is how you get a job that fires twice or not at all


class _Sentinel:
    """A marker that can't be confused with any engine name."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"


# These used to be imported from agent.main — and importing agent.main pulls
# in the tool layer and everything behind it: 51 of 63 modules, 25,700 lines,
# including the Blender lab and the fine-tuner, all for two marker values.
# Defining them here takes the Jobs app to 16 modules, every one of which is
# about jobs, which is what lets it stand on its own.
_AUTO = _Sentinel("auto")
_LOCAL_UNAVAILABLE = _Sentinel("local unavailable")


def _force_model(choice: str):
    """An engine name as the brain understands it.

    Copied from the main server rather than imported: importing it would
    make this app depend on the whole web app starting, which is the thing
    the split exists to avoid.
    """
    choice = (choice or "").strip()
    if not choice or choice == "Auto":
        return _AUTO
    if choice in brainmod.custom_engine_names():
        return choice
    if choice == "Claude":
        return "Claude"
    if choice == "Ollama":
        return (config.OLLAMA_MODEL if brainmod.local_available()
                else _LOCAL_UNAVAILABLE)
    return _AUTO


def get_memory():
    global memory
    if memory is None:
        memory = MemoryStore(db_path=config.DB_PATH, check_same_thread=False)
    return memory


def get_brain():
    global _brain
    if _brain is None:
        _brain = make_brain()
    return _brain


@app.exception_handler(EngineNotConfigured)
async def _no_engine(request: Request, exc: EngineNotConfigured):
    return JSONResponse(status_code=503,
                        content={"detail": f"{exc.message} {exc.fix}"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    """What the window needs to render itself."""
    ready, why = jobscout.profile_ready()
    return {
        "app": APP_NAME,
        "build": config.BUILD_ID,
        "brand": getattr(config, "BRAND_NAME", "Symbolic Synapse"),
        "tagline": getattr(config, "BRAND_TAGLINE", ""),
        "profile_ready": ready,
        "profile_note": why,
        "engines": [e["name"] for e in
                    brainmod.load_custom_engines(refresh=True)],
        "default_engine": config.DEFAULT_ENGINE,
    }


# --------------------------------------------------------------------------- #
#  The routes, moved from the main app rather than rewritten — same code,
#  same behaviour, different window.
# --------------------------------------------------------------------------- #
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
    if forced is _LOCAL_UNAVAILABLE:
        return None, (f"'{choice}' is a local engine but no local model is "
                      f"running to serve it. Start Ollama, or pick another "
                      f"engine.")
    if forced is _AUTO:
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
    # Unknown fields are refused. The window once sent {"roles": [...]} here;
    # the field is "items", the extra key was quietly dropped, and the call
    # returned ok with nothing added — every Track click from search looked
    # like it worked and saved nothing. A wrong name must fail loudly.
    model_config = {"extra": "forbid"}
    items: list = []
    restore: bool = False


@app.post("/api/jobs/search/add")
def jobs_search_add(body: JobsAddBody2):
    return jobscout.add_from_search(body.items, restore=body.restore)


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




app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
