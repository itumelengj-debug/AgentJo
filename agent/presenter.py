"""Presenter — a narrated walkthrough that drives the actual app.

The Guide is a reading list: you open it and read about features. That's the
wrong shape for showing someone. In a demo the audience should be looking at
the real thing working, not at a description of it, and the presenter should
not be hunting for the right panel mid-sentence.

So this drives the app. Each scene opens the real panel it's talking about and
puts the narration in a bar along the bottom, leaving the application itself
on screen. Space or → advances; ← goes back; Esc leaves and tidies up. There's
an auto-advance for unattended running — a screen in reception, or a stand —
and speaker notes that only the presenter sees.

Two things it deliberately does not do:

  It doesn't fake anything. Every scene opens a live panel with whatever real
  data is there. A demo that shows invented numbers teaches the audience
  nothing and embarrasses you the moment someone asks to try it.

  It doesn't hide the honest limits. The scenes that cover self-improvement,
  auto-apply and the 3D tools say plainly where the human gate is and what the
  feature can't do. In front of a client that is the difference between a
  credible demo and a sales pitch nobody believes.
"""
from __future__ import annotations

from . import config

# seconds: how long the scene sits before auto-advance moves on. Longer for
# scenes where something actually needs to load or be read.
SCENES = [
    {
        "key": "open",
        "act": "What this is",
        "title": "A private AI agent that runs on this machine",
        "say": "This is Agent Jo. It runs entirely on this computer — the "
               "conversations, the documents, the memory, all of it stays "
               "here. It isn't a chat window with an API behind it: it can "
               "read files, run commands, search the web, build documents and "
               "drive other software.",
        "note": "Set the frame: local-first, and it acts rather than only "
                "answers. Everything after this is an example of that.",
        "panel": "", "seconds": 14,
    },
    {
        "key": "engines",
        "act": "What this is",
        "title": "It chooses between a local model and the cloud",
        "say": "Two kinds of engine. A local model that costs nothing and "
               "never sends your data anywhere, and a cloud model that's "
               "stronger but billed per use. The app tells them apart by "
               "their endpoint, not their name — so it always knows whether "
               "a turn is costing you money.",
        "note": "Point at the dot on the engine pill: bronze is local, gold "
                "is cloud. If asked about cost, this is the honest answer.",
        "panel": "enginesBtn", "seconds": 16,
    },
    {
        "key": "chat",
        "act": "What this is",
        "title": "Ask it to do something, not just to answer",
        "say": "You ask in plain language and it decides which of its tools "
               "to use. It'll ask before it touches anything outside its own "
               "folder — permission is granted per action, and everything it "
               "does is recorded.",
        "note": "Good live moment: ask it something about a real file on this "
                "machine. Let the audience see the permission prompt.",
        "panel": "", "seconds": 15,
    },

    {
        "key": "memory",
        "act": "It learns your context",
        "title": "It remembers across conversations",
        "say": "It keeps what matters about your work — your stack, your "
               "clients, how you like things done — so you stop "
               "re-explaining yourself at the start of every session.",
        "note": "If there are real memories here, read one or two aloud. "
                "Concrete beats abstract.",
        "panel": "memoryBtn", "seconds": 13,
    },
    {
        "key": "documents",
        "act": "It learns your context",
        "title": "It answers from your own documents",
        "say": "Drop in PDFs, spreadsheets, contracts, reports — it indexes "
               "them and answers from their contents rather than from "
               "training data. The files never leave this machine.",
        "note": "For a client audience this is usually the moment they lean "
                "in. Mention that it works with a local model too, so "
                "confidential documents never touch a cloud API.",
        "panel": "documentsBtn", "seconds": 15,
    },
    {
        "key": "skills",
        "act": "It learns your context",
        "title": "You teach it your method once",
        "say": "Skills are procedures you've taught it — your review "
               "checklist, your way of preparing a deliverable. Once taught "
               "it follows your method instead of inventing one, and the "
               "panel shows which skills actually get used.",
        "note": "The usage counts matter: this is the app being honest about "
                "which of its own features earn their keep.",
        "panel": "skillsBtn", "seconds": 15,
    },

    {
        "key": "scheduler",
        "act": "It works while you don't",
        "title": "Anything you can ask for once can run on a schedule",
        "say": "A morning briefing, a weekly report, a site checked every "
               "day. The work happens while you're doing something else, and "
               "the results are waiting for you.",
        "note": "Mention that the app has to be running — it's a desktop "
                "agent, not a server. Better said now than asked later.",
        "panel": "schedBtn", "seconds": 13,
    },
    {
        "key": "crew",
        "act": "It works while you don't",
        "title": "Four specialists, each with its own brief",
        "say": "Delivery, business development, operations and market "
               "intelligence — each with its own standing brief, its own "
               "workspace and its own memory. Work goes to the right one, and "
               "chains hand it between them: intelligence finds an "
               "opportunity, business development qualifies it, delivery "
               "scopes what building it would take.",
        "note": "This is the strongest single idea for a consultancy "
                "audience. If you demo one thing live, demo a chain.",
        "panel": "crewBtn", "seconds": 18,
    },
    {
        "key": "challenges",
        "act": "It works while you don't",
        "title": "It looks for problems worth solving",
        "say": "It scans South African news and government sources for real "
               "problems — municipal billing, water infrastructure, clinic "
               "queues — and drafts where a small data team could actually "
               "help, including what data already exists and who holds it. "
               "Every brief also states what would make the idea fail.",
        "note": "That last part is the credibility point. An opportunity "
                "list with no risks is a wish list, and the room knows it.",
        "panel": "challengesBtn", "seconds": 18,
    },
    {
        "key": "pipelines",
        "act": "It works while you don't",
        "title": "Data work it can prove",
        "say": "It builds bronze, silver and gold pipelines from your files "
               "and then proves the new version matches the old one — row by "
               "row, column by column — before you trust it. For a migration, "
               "'I think it's the same' isn't good enough.",
        "note": "Aimed squarely at BI and data audiences. The regression "
                "proof is the differentiator, not the pipeline.",
        "panel": "", "seconds": 16,
    },

    {
        "key": "honesty",
        "act": "Why you can trust it",
        "title": "Everything it did, provably unaltered",
        "say": "Every turn, every tool call, every configuration change is "
               "written to a hash-chained log. If a line were edited "
               "afterwards, the chain breaks and the app says exactly where.",
        "note": "Press Verify chain live. It takes a second and it lands.",
        "panel": "auditBtn", "seconds": 14,
    },
    {
        "key": "undo",
        "act": "Why you can trust it",
        "title": "Nothing it changes is one-way",
        "say": "Before it overwrites any file, the previous version is saved. "
               "One badly-worded instruction can't cost you an afternoon.",
        "note": "Pairs naturally with the previous scene: one records, the "
                "other reverses.",
        "panel": "undoBtn", "seconds": 12,
    },
    {
        "key": "health",
        "act": "Why you can trust it",
        "title": "It tells you when it isn't working",
        "say": "Twenty checks covering the engines, the tools, the data and "
               "the automation — each one with the specific fix for anything "
               "that's wrong. It reports its own failures rather than hiding "
               "them.",
        "note": "If something is red right now, say so out loud. Showing a "
                "real fault and its fix is more convincing than a green "
                "screen nobody believes.",
        "panel": "healthBtn", "seconds": 15,
    },
    {
        "key": "cost",
        "act": "Why you can trust it",
        "title": "You can see what it costs",
        "say": "Tokens and spend, broken down by which feature spent them, "
               "against a monthly ceiling that actually blocks rather than "
               "warns. Local work shows as free, because it is.",
        "note": "Point at the dashboard tiles rather than opening anything.",
        "panel": "", "seconds": 13,
    },

    {
        "key": "selfimprove",
        "act": "Where it's going",
        "title": "It can build its own next feature",
        "say": "Ask it to add something to itself and it works in a sandboxed "
               "copy of its own source, then has to pass its own eight "
               "hundred tests before you're shown a single line. It proposes; "
               "only a human applies. There is no path where it changes "
               "itself on its own.",
        "note": "Say the gate out loud. In a room with any engineer in it, "
                "the first question is 'what stops it breaking itself' — "
                "answer it before it's asked.",
        "panel": "selfBtn", "seconds": 20,
    },
    {
        "key": "close",
        "act": "Where it's going",
        "title": "Local-first, auditable, and yours",
        "say": "Everything you've seen runs on one machine, keeps a record of "
               "what it did, and can be handed to someone else with none of "
               "your data in it. It isn't a demo of what AI might do — it's a "
               "working tool that happens to be honest about its limits.",
        "note": "Close here. If there's time, take one request from the room "
                "and run it live — the app holding up unrehearsed is worth "
                "more than any slide.",
        "panel": "", "seconds": 16,
    },
    # A demo SELECTS where a guide covers — but it can't skip the largest
    # feature, or the thing that makes the whole idea defensible.
    {
        "key": "jobs",
        "act": "Doing real work",
        "title": "A job search that runs itself",
        "say": "This is the part that runs unattended. It reads job boards "
               "and the alert emails those boards send, de-duplicates the "
               "same role listed three times, screens out the obvious "
               "mismatches for nothing using a local keyword check, scores "
               "what's left, drafts an application, and applies — inside a "
               "daily cap, in rehearsal until you say otherwise.",
        "note": "The pipeline strip is the thing to point at: it shows where "
                "the work has stopped, which is what a status bar should do "
                "and almost never does.",
        "panel": "jobsBtn", "seconds": 20,
    },
    {
        "key": "fabrication",
        "act": "Doing real work",
        "title": "It refuses to say what it can't support",
        "say": "Every draft is checked against the profile before it can be "
               "sent. If it claims a tool, an employer or a number that "
               "isn't in there, it's held — and you're shown exactly which "
               "claim, with the choice to confirm it as true or say you "
               "don't claim it.",
        "note": "This is the honest centre of the whole app. A tool that "
                "sends confident applications you'd have to defend in an "
                "interview is worse than no tool.",
        "panel": "jobsBtn", "seconds": 18,
    },
    {
        "key": "gates",
        "act": "Trusting it",
        "title": "Reads run; anything leaving the machine waits",
        "say": "Under full access it doesn't ask about everything — being "
               "prompted constantly teaches you to click through without "
               "reading. It sorts by consequence: listing a folder runs, "
               "sending an email waits, and it shows you the actual "
               "recipient, subject and size rather than the tool's name.",
        "note": "The distinction, not the prompt, is the feature. Show a "
                "held call and read the summary line aloud.",
        "panel": "autonomyBtn", "seconds": 18,
    }
]


def acts() -> list:
    seen, out = set(), []
    for s in SCENES:
        if s["act"] not in seen:
            seen.add(s["act"])
            out.append(s["act"])
    return out


def scenes() -> list:
    return [dict(s, index=i) for i, s in enumerate(SCENES)]


def runtime_seconds() -> int:
    return sum(int(s.get("seconds") or 0) for s in SCENES)


def state() -> dict:
    return {"scenes": scenes(), "acts": acts(),
            "count": len(SCENES),
            "runtime_seconds": runtime_seconds(),
            "build": getattr(config, "BUILD_ID", "unknown")}
