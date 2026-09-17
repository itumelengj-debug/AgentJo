"""Tour — a walk through the app that explains what everything is for.

Thirty-odd features have accumulated here, most behind a small icon in a
sidebar group. Someone opening it for the first time — including the person
this gets shared with — has no way to know that ⇪ lets the app rewrite itself
under test, or why there are two different photo-to-3D routes.

So each stop answers the three things a person actually needs, in order:

    what it is        one sentence, no jargon
    why you'd care    the situation where you'd reach for it
    try this          a concrete thing to type or click, right now

The "try this" line matters most. A tour that only describes leaves you
exactly where you started; one that hands you a first move gets you using the
thing. Every stop can open its own panel, so reading and doing are one click
apart.

Ordered as a narrative rather than by menu position: what it is, how to make
it yours, how to let it work unattended, how to keep it honest, and only then
the specialist tools.
"""
from __future__ import annotations

from . import config

# panel: the sidebar button id a stop opens, or "" when there's nothing to open
STOPS = [
    # ---- the basics ----------------------------------------------------
    {"key": "chat", "chapter": "Start here", "title": "Talking to it",
     "what": "An AI agent that runs on this computer and can actually do "
             "things — read files, run commands, search the web, build "
             "documents — rather than only talk about them.",
     "why": "Ask it in plain words. It decides which of its tools to use.",
     "try": "Ask: “what's in my Downloads folder, and is anything worth "
            "deleting?”",
     "panel": ""},
    {"key": "engines", "chapter": "Start here", "title": "Engines",
     "what": "Which model does the thinking. Claude is strongest and costs "
             "money; a local model via Ollama is free and private.",
     "why": "You can pin one engine, or let Auto choose. The dot on the "
            "engine pill is bronze for local and gold for cloud, so you can "
            "always see whether a turn is costing anything.",
     "try": "Open Engines and see what's available. If Ollama is running, "
            "pin it and ask something — that turn is free.",
     "panel": "enginesBtn"},
    {"key": "permissions", "chapter": "Start here",
     "title": "What it's allowed to do",
     "what": "It asks before touching anything outside its own folder. Full "
             "access, in the composer, lets it act without asking.",
     "why": "Leave it off for ordinary chat; switch it on when you want it to "
            "actually build or change something.",
     "try": "Open Permissions to see exactly what has been allowed so far.",
     "panel": "permsBtn"},

    # ---- making it yours ----------------------------------------------
    {"key": "memory", "chapter": "Making it yours", "title": "Memory",
     "what": "It remembers facts about you and your work across "
             "conversations — your stack, your clients, how you like things "
             "done.",
     "why": "You stop re-explaining context every time you open a new chat.",
     "try": "Tell it something true about your work, start a new chat, and "
            "ask it back.",
     "panel": "memoryBtn"},
    {"key": "documents", "chapter": "Making it yours", "title": "Documents",
     "what": "Drop in PDFs, Word files or spreadsheets and it can search "
             "them and answer from their contents.",
     "why": "For anything where the answer is in your files rather than in "
            "the model's training.",
     "try": "Drag a PDF onto the window, then ask a question only that "
            "document could answer.",
     "panel": "documentsBtn"},
    {"key": "skills", "chapter": "Making it yours", "title": "Skills",
     "what": "Procedures you've taught it — your way of reviewing a "
             "dashboard, your checklist before a deliverable goes out.",
     "why": "Once taught, it follows your method instead of inventing one. "
            "The panel shows which skills you actually use.",
     "try": "Open Skills and run one on something. If it's empty, teach one: "
            "“remember how I like BI reviews done: …”",
     "panel": "skillsBtn"},

    # ---- letting it work on its own ------------------------------------
    {"key": "scheduler", "chapter": "Working unattended",
     "title": "Scheduled jobs",
     "what": "Anything you can ask for once can be put on a schedule — a "
             "morning briefing, a weekly report, a site check.",
     "why": "The work happens while you're doing something else.",
     "try": "Schedule one job two minutes from now and watch it run.",
     "panel": "schedBtn"},
    {"key": "watchers", "chapter": "Working unattended",
     "title": "Watching websites",
     "what": "Monitors a page and tells you only what's new — new tenders, "
             "new listings — not the whole page again.",
     "why": "It re-derives its own selectors when a site changes its markup, "
            "which is normally what breaks a scraper.",
     "try": "Add a watcher for a listing page you check by hand.",
     "panel": "schedBtn"},
    {"key": "crew", "chapter": "Working unattended",
     "title": "The crew",
     "what": "Four standing specialists — Delivery, BizDev, Ops, Intel — each "
             "with its own brief, workspace and memory.",
     "why": "Bigger jobs go to the right specialist, and chains hand work "
            "between them: Intel finds an opportunity, BizDev qualifies it, "
            "Delivery scopes it.",
     "try": "Open Crew and run “find three prospects who need BI” on "
            "auto-route.",
     "panel": "crewBtn"},
    {"key": "jobs", "chapter": "Working unattended",
     "title": "Job hunting",
     "what": "Finds remote contract roles, scores them honestly, and drafts "
             "applications grounded only in your real profile.",
     "why": "Auto-apply can send them for you, but only for roles that clear "
            "the gates you set — and never with a claim your profile can't "
            "support.",
     "try": "Ask it to build your job profile from your CV, then open Jobs.",
     "panel": "jobsBtn"},
    {"key": "trends", "chapter": "Working unattended",
     "title": "Trends",
     "what": "Watches what's happening in AI agents and drafts skills you "
             "can adopt with one click.",
     "why": "The app keeps up with its own field without you reading "
            "newsletters.",
     "try": "Open Trends, pick a local engine for the digest, and scan.",
     "panel": "trendsBtn"},
    {"key": "challenges", "chapter": "Working unattended",
     "title": "South African challenges",
     "what": "Scans SA news and government sources for real problems, and "
             "drafts where a small data/AI team could genuinely help.",
     "why": "Turns “AI could help somewhere” into a specific brief with a "
            "first engagement — and the reasons it might fail.",
     "try": "Open Challenges and scan. Send one brief to the crew to "
            "qualify.",
     "panel": "challengesBtn"},

    # ---- keeping it honest ---------------------------------------------
    {"key": "dashboard", "chapter": "Keeping it honest",
     "title": "The dashboard",
     "what": "What needs you, and what's running — tokens, spend against "
             "your ceiling, and anything waiting on a decision.",
     "why": "Every item is a button that opens the panel that resolves it.",
     "try": "Look at the top of this window. Anything listed there is one "
            "click from being dealt with.",
     "panel": ""},
    {"key": "health", "chapter": "Keeping it honest", "title": "Health",
     "what": "Fifteen checks that say what is actually working right now, "
             "each with the specific fix for anything that isn't.",
     "why": "It's the fastest way to find out why something is misbehaving.",
     "try": "Open Health. Anything red is real; the fix is written next to "
            "it.",
     "panel": "healthBtn"},
    {"key": "backup", "chapter": "Keeping it honest", "title": "Backup",
     "what": "One file holding everything it has learned — memories, skills, "
             "settings, the audit trail.",
     "why": "All of that lives on this one disk otherwise. Restore verifies "
            "the archive first and takes a rollback point before replacing "
            "anything.",
     "try": "Back up now, then download it somewhere that isn't this "
            "computer.",
     "panel": "backupBtn"},
    {"key": "undo", "chapter": "Keeping it honest", "title": "Undo",
     "what": "Every file the agent overwrites is snapshotted first.",
     "why": "One badly-worded instruction can't cost you an afternoon's work.",
     "try": "Open Undo after it has edited something, and try Diff.",
     "panel": "undoBtn"},
    {"key": "audit", "chapter": "Keeping it honest", "title": "Audit trail",
     "what": "A tamper-evident log of everything it did — every turn, tool "
             "call, and config change.",
     "why": "When you need to know exactly what happened, and prove the "
            "record wasn't edited afterwards.",
     "try": "Open Audit and press Verify chain.",
     "panel": "auditBtn"},
    {"key": "capabilities", "chapter": "Keeping it honest",
     "title": "Capabilities",
     "what": "Which of the app's features have actually been used on this "
             "machine, and which never have.",
     "why": "A feature you've never run is an untested assumption. Each one "
            "carries a sixty-second test.",
     "try": "Open Capabilities and run the first thing it suggests.",
     "panel": "capsBtn"},

    # ---- the specialist end --------------------------------------------
    {"key": "pipelines", "chapter": "Specialist tools",
     "title": "Data pipelines",
     "what": "Builds Bronze/Silver/Gold pipelines from your files and proves "
             "a new version matches the old one, row by row.",
     "why": "For migrations where “I think it's the same” isn't good enough.",
     "try": "Ask it to build a pipeline from a CSV and run the regression.",
     "panel": ""},
    {"key": "blender", "chapter": "Specialist tools", "title": "3D and photos",
     "what": "Drives Blender to render photoreal product shots, and turns a "
             "photo into a 3D model — procedurally, or with a neural model "
             "if you've installed one.",
     "why": "Product visuals and mockups without a designer.",
     "try": "Full access on, then: “render a chrome sphere on a white floor, "
            "Cycles 64 samples”.",
     "panel": ""},
    {"key": "selfimprove", "chapter": "Specialist tools",
     "title": "Self-improvement",
     "what": "It can write its own next feature: it works in a sandboxed copy "
             "and must pass the app's full test suite before you see a diff.",
     "why": "It proposes; only you apply. Applied files go to Undo first.",
     "try": "Ask: “add a version marker comment to agent/config.py — build it "
            "into yourself”.",
     "panel": "selfBtn"},
    {"key": "mcp", "chapter": "Specialist tools", "title": "MCP servers",
     "what": "Connects external tool servers — a filesystem, a database, a "
             "SaaS product — so the agent can use them directly.",
     "why": "Extends what it can touch without changing the app.",
     "try": "Open MCP and connect a filesystem server to a folder you use.",
     "panel": "mcpBtn"},
    {"key": "turbo", "chapter": "Specialist tools",
     "title": "Turbo and second opinion",
     "what": "Turbo answers locally first and only escalates to the cloud if "
             "the answer fails automatic checks. Second opinion has one "
             "engine review the other's answer.",
     "why": "One saves money, the other catches mistakes. Both report whether "
            "they're actually earning their keep.",
     "try": "Turn Turbo on in Settings and watch the hit-rate tile.",
     "panel": "settingsBtn"},
    # ---- added when a coverage check caught them missing -----------------
    {"key": "issues", "chapter": "When something's wrong",
     "title": "Reporting a problem",
     "what": "A one-click report with the build id, your engine, recent "
             "internal errors and the last few messages — the things anyone "
             "diagnosing it would ask for.",
     "why": "Errors are captured as they happen, so the report has the "
            "detail even when you noticed the problem an hour later.",
     "try": "Open it and press Copy report — that text is what to paste when "
            "asking for help.",
     "panel": "issuesBtn"},
    {"key": "autonomy", "chapter": "Letting it work",
     "title": "How much it may do alone",
     "what": "The line between what it does on its own and what waits for "
             "you. Reads and local work run; anything that leaves this "
             "machine is held with its exact inputs for review.",
     "why": "Prompting on everything teaches you to click through without "
            "reading, which is worse than not asking.",
     "try": "Turn full access on for one task and watch what it holds back.",
     "panel": "autonomyBtn"},
    {"key": "outreach", "chapter": "Letting it work",
     "title": "Email and campaigns",
     "what": "Sending mail — single messages or a campaign — with the same "
             "fabrication check the job applications get.",
     "why": "Nothing is sent that claims something your profile can't "
            "support, and every send is recorded.",
     "try": "Send yourself one test message before trusting it with a list.",
     "panel": "outreachBtn"},
    {"key": "phone", "chapter": "Everyday use", "title": "On your phone",
     "what": "It installs to a phone from the same machine — the app is a "
             "web app, so your phone just needs the address.",
     "why": "Useful for checking on scheduled work while away from the desk.",
     "try": "Open Phone and follow the address shown on your own network.",
     "panel": "phoneBtn"},
    {"key": "guide", "chapter": "Everyday use", "title": "This guide",
     "what": "What every part of the app is for, in order.",
     "why": "A coverage check now fails the build if a panel has no stop "
            "here, so this can't quietly fall behind the app again.",
     "try": "You're doing it.",
     "panel": "tourBtn"},
    {"key": "presenter", "chapter": "Everyday use",
     "title": "Showing it to someone",
     "what": "A narrated run through the real app — actual panels and real "
             "data, not screenshots.",
     "why": "For demonstrating it without clicking around and losing the "
            "thread.",
     "try": "Press Escape at any point to stop; it puts everything back.",
     "panel": "presenterBtn"},
    {"key": "codemap", "chapter": "Specialist tools",
     "title": "Mapping a codebase",
     "what": "Point it at a project folder and it reads the imports: what "
             "every module leans on, which pairs import each other in a "
             "circle, and what nothing imports at all.",
     "why": "Reading a folder tells you what exists. It doesn't tell you "
            "which file you can change safely, or which one everything would "
            "feel. Selecting a module shows exactly what a change would "
            "reach.",
     "try": "Map this app's own folder — it finds one import cycle and shows "
            "that fifty-odd modules depend on config.",
     "panel": "codemapBtn"},
    {"key": "results", "chapter": "Working unattended",
     "title": "Learning from what happened",
     "what": "Which boards produced replies, whether your fit score predicts "
             "anything, and whether email beats a portal form.",
     "why": "Every rate is shown with the range it could really be. Five "
            "applications with two replies is a 40% rate and also entirely "
            "consistent with 8% — a tool that prints 40% there sends you "
            "chasing a board that was merely lucky.",
     "try": "Jobs \u2192 Results. Below a dozen applications it will tell "
            "you plainly that it can't conclude anything yet.",
     "panel": "jobsBtn"},
    {"key": "finetune", "chapter": "Making it yours",
     "title": "Fine-tuning a local model",
     "what": "Ask it to fine-tune a model for a subject and it plans, builds "
             "a dataset, trains on your GPU, measures the result against the "
             "model it started from, and registers it only if it won.",
     "why": "Tuning teaches a model HOW to answer, not what is true. The "
            "plan says that before six hours of GPU, and the comparison "
            "afterwards catches the common case where the tuned model is "
            "worse than the one it came from.",
     "try": "Say: fine-tune a 14B to answer like a Power BI consultant. Read "
            "the plan before agreeing to anything.",
     "panel": "enginesBtn"},
    {"key": "modelbuild", "chapter": "Specialist tools",
     "title": "Building a predictive model",
     "what": "Point it at a spreadsheet and say what to predict. It profiles "
             "the data, looks for columns that already contain the answer, "
             "trains several candidates, and reports the winner against a "
             "do-nothing baseline on data it never saw.",
     "why": "94% accuracy is worthless on a dataset that's 94% one class, so "
            "every score is shown next to what guessing would get. The test "
            "rows are used once, at the end — tune against them and the "
            "number stops predicting anything.",
     "try": "Models \u2192 point it at a spreadsheet, name the column to "
            "predict, press Build a model. Read the leakage note before "
            "believing a high score.",
     "panel": "mlBtn"}
]


def chapters() -> list:
    seen, out = set(), []
    for s in STOPS:
        if s["chapter"] not in seen:
            seen.add(s["chapter"])
            out.append(s["chapter"])
    return out


def stops(chapter: str = "") -> list:
    items = [s for s in STOPS if not chapter or s["chapter"] == chapter]
    return [dict(s, index=i) for i, s in enumerate(items)]


def state() -> dict:
    return {"chapters": chapters(), "stops": stops(),
            "total": len(STOPS),
            "build": getattr(config, "BUILD_ID", "unknown")}


def coverage() -> dict:
    """Which panels the guide explains, and which it has never heard of.

    A guide falls behind an app silently: nothing breaks, it just quietly
    stops describing the thing in front of you. Measured here so the test
    suite can fail on it — a panel shipped without a stop is a panel nobody
    will find.
    """
    import re
    from pathlib import Path
    html = ""
    for p in (Path(__file__).resolve().parent.parent
              / "web" / "static" / "index.html",):
        try:
            html = p.read_text("utf-8")
            break
        except Exception:
            pass
    panels = []
    for m in re.finditer(r'<button class="foot-btn" id="(\w+)"', html):
        panels.append(m.group(1))
    covered = {s.get("panel") for s in STOPS if s.get("panel")}
    missing = [p for p in panels if p not in covered]
    stale = [s.get("panel") for s in STOPS
             if s.get("panel") and s["panel"] not in panels]
    return {"panels": len(panels), "stops": len(STOPS),
            "covered": len([p for p in panels if p in covered]),
            "missing": missing, "stale": stale,
            "ok": not missing and not stale,
            "note": ("A panel with no stop is a panel nobody finds; a stop "
                     "pointing at a panel that no longer exists sends people "
                     "to a button that isn't there.")}
