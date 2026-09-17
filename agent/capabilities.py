"""Capabilities — what this app can do, and what you have never actually used.

The honest problem this solves: features here have a habit of existing without
being exercised. A whole backup module sat wired to nothing. Watchers existed
in a weaker form and nobody knew. Neural photo-to-3D was configured and never
run. Every one of those was discovered by accident, months later.

So rather than trusting memory or a README, this reads the evidence the app
already keeps — the audit trail, plus the files and folders each feature
creates when it genuinely runs — and reports three states per capability:

    used            there is proof it ran here
    ready           configured, but never actually exercised
    needs setup     it cannot run yet, and here is the missing piece

Every entry carries a **sixty-second test**: the exact thing to type or click
to prove it works on this machine. A capability you have never run is not a
capability; it is an untested assumption, and this is the list of them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import config

USED, READY, SETUP = "used", "ready", "needs setup"


def _home() -> Path:
    return config.AGENT_HOME


def _exists(*parts) -> bool:
    p = _home().joinpath(*parts)
    try:
        if p.is_dir():
            return any(p.iterdir())
        return p.exists() and p.stat().st_size > 0
    except Exception:
        return False


# Each capability: how to tell it ran, what it needs, and the 60-second test.
# audit_kinds are the strongest evidence — they only appear after a real run.
REGISTRY = [
    {"key": "chat", "name": "Chat & tools", "group": "Core",
     "audit_kinds": ["turn"],
     "test": "Ask it anything — if you're reading this, it works."},
    {"key": "documents", "name": "Documents (RAG)", "group": "Core",
     "evidence": [("documents.db",)],
     "test": "Open 📄 Documents, drop in a PDF, then ask a question about it."},
    {"key": "memory", "name": "Long-term memory", "group": "Core",
     "evidence": [("agent.db",)],
     "test": "Tell it a fact about your work, start a new chat, ask it back."},
    {"key": "scheduler", "name": "Scheduled jobs", "group": "Automation",
     "audit_kinds": ["autonomy"],
     "test": "⏰ Scheduler → add a job for 2 minutes from now and watch it "
             "run."},
    {"key": "watchers", "name": "Site watchers", "group": "Automation",
     "audit_kinds": ["watch"], "evidence": [("watch_state.json",)],
     "test": "Add a watcher for a listing page with an item_selector, then "
             "run it twice — the second run should report no new items."},
    {"key": "trends", "name": "Trend scout", "group": "Automation",
     "audit_kinds": ["trend"], "evidence": [("trendscout",)],
     "test": "📡 Trends → set Digest engine → Scan now."},
    {"key": "jobs", "name": "Job scout", "group": "Automation",
     "audit_kinds": ["jobscout"], "evidence": [("jobscout",)],
     "needs": [("jobscout/profile.json", "your job profile isn't filled in")],
     "test": "Ask: “build my job profile from my CV”, then 🎯 Jobs → Run "
             "auto-apply with rehearsal on."},
    {"key": "crew", "name": "Specialist crew", "group": "Automation",
     "audit_kinds": ["crew"], "evidence": [("crew",)],
     "test": "👥 Crew → “Research 3 JHB manufacturers likely to need BI” → "
             "Auto-route."},
    {"key": "outreach", "name": "Email outreach", "group": "Automation",
     "evidence": [("email.json",), ("email_log.jsonl",)],
     "needs": [("email.json", "no email account configured")],
     "test": "✉ Outreach → send yourself one test message."},
    {"key": "pipelines", "name": "Data pipelines", "group": "Work",
     "audit_kinds": ["pipeline"], "evidence": [("pipelines",)],
     "test": "Ask it to build a pipeline from a CSV and run the AS-IS/TO-BE "
             "regression."},
    {"key": "blender", "name": "Blender 3D lab", "group": "Work",
     "audit_kinds": ["blender"], "evidence": [("blender",)],
     "needs_check": "blender",
     "test": "Full access on → “Render a chrome sphere on a white floor, "
             "Cycles 64 samples”."},
    {"key": "neural3d", "name": "Neural photo-to-3D", "group": "Work",
     "audit_kinds": ["neural3d"], "evidence": [("neural3d",)],
     "needs_check": "neural3d",
     "test": "Full access on → “neural-lift C:\\\\path\\\\to\\\\photo.jpg into "
             "a 3D model”."},
    {"key": "mcp", "name": "MCP servers", "group": "Extend",
     "evidence": [("mcp.json",)],
     "test": "⬡ MCP → add a server → it should report its tool count."},
    {"key": "selfimprove", "name": "Self-improvement", "group": "Extend",
     "audit_kinds": ["selfimprove"], "evidence": [("selfimprove",)],
     "test": "Ask: “add a version marker comment to agent/config.py — build "
             "it into yourself”, then review the diff in ⇪."},
    {"key": "evals", "name": "Engine evals", "group": "Extend",
     "audit_kinds": ["eval"], "evidence": [("evals",)],
     "test": "Run the evals against CustomQWEN and read which features it can "
             "safely take."},
    {"key": "timemachine", "name": "Undo (time machine)", "group": "Safety",
     "audit_kinds": ["undo"], "evidence": [("timemachine",)],
     "test": "Have it overwrite a throwaway file, then ⟲ Undo → Diff → "
             "Restore."},
    {"key": "audit", "name": "Audit trail", "group": "Safety",
     "evidence": [("audit.jsonl",)],
     "test": "▤ Audit → Verify chain."},
    {"key": "backup", "name": "Backup & restore", "group": "Safety",
     "audit_kinds": ["backup"], "evidence": [("backups",)],
     "test": "🛟 Backup → Back up now → Download, and put the file somewhere "
             "else."},
    {"key": "privacy", "name": "Privacy shield", "group": "Safety",
     "test": "Settings → Privacy mode → mask, then mention a fake ID number "
             "and check the footer note."},
    {"key": "health", "name": "Health board", "group": "Safety",
     "test": "⏻ Health → read it. Anything red is a real thing to fix."},
    {"key": "costs", "name": "Cost governor", "group": "Safety",
     "evidence": [("costs.json",)],
     "test": "Set a monthly ceiling in Settings, then check ⏻ Health → "
             "Spend."},
]


def _audit_kinds_seen(limit: int = 4000) -> set:
    try:
        from . import audit
        return {e.get("kind") for e in audit.recent(limit)}
    except Exception:
        return set()


def _setup_gap(cap: dict) -> str:
    """The specific missing piece, or '' when it's ready to run."""
    check = cap.get("needs_check")
    if check == "blender":
        try:
            from . import blenderlab
            if not blenderlab.find_blender():
                return ("Blender isn't installed or found — install it "
                        "(blender.org) or set Settings → Blender path.")
        except Exception:
            return "Could not check for Blender."
    if check == "neural3d":
        cmd = (getattr(config, "NEURAL3D_CMD", "") or "").strip()
        if not cmd:
            return ("No image-to-3D tool configured — Settings → Neural 3D "
                    "command → Detect.")
    for rel, why in cap.get("needs", []):
        if not _exists(*rel.split("/")):
            return why
    return ""


def status() -> dict:
    seen = _audit_kinds_seen()
    out, counts = [], {USED: 0, READY: 0, SETUP: 0}
    for cap in REGISTRY:
        used = any(k in seen for k in cap.get("audit_kinds", []))
        if not used:
            used = any(_exists(*parts) for parts in cap.get("evidence", []))
        gap = "" if used else _setup_gap(cap)
        state = USED if used else (SETUP if gap else READY)
        counts[state] += 1
        out.append({"key": cap["key"], "name": cap["name"],
                    "group": cap["group"], "state": state,
                    "blocker": gap, "test": cap["test"]})
    order = {"Core": 0, "Automation": 1, "Work": 2, "Extend": 3, "Safety": 4}
    out.sort(key=lambda c: (order.get(c["group"], 9), c["name"]))
    never = [c for c in out if c["state"] != USED]
    return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "total": len(out), "counts": counts, "capabilities": out,
            "unused": [c["name"] for c in never],
            "next_test": never[0] if never else None}
