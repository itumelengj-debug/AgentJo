# Agent Jo

**An AI agent that runs on your own machine.** Not a chat window with an API
behind it — it reads your files, runs commands, drives a browser, applies for
jobs, and keeps working while you're away from the desk.

Conversations, documents, memory and audit trail stay on the machine it's
installed on. Point it at a local model and nothing leaves at all.

*by Symbolic Synapse*

---

## What it actually does

**Works unattended.** Scheduled runs with circuit breakers, a watchdog that
notices when a feature has gone quiet, and a tamper-evident audit trail of
everything it did. It tells you where it stopped and why.

**Applies for jobs, properly.** Reads job boards and the alert emails those
boards send, de-duplicates the same role listed three times, screens out
obvious mismatches with a free local check, scores what's left, drafts an
application — and **holds any draft that claims something your profile can't
support**. Then it learns: which sources actually produce replies, whether its
own fit score predicts anything, whether email beats a portal form.

**Reads what you give it.** PDF, Word, Excel, PowerPoint, OpenDocument, HTML,
email, EPUB. Retrieval over your own documents, locally.

**Runs any model.** Anthropic, OpenAI-compatible endpoints, or Ollama on your
own GPU. Rules-based routing sends light work to a local model and hard work
to the strong one — and tells you which rule fired. It can fine-tune a local
model for a domain, and refuses to register the result unless it beats the
model it started from.

**Maps a codebase.** Point it at a folder: what every module leans on, what
imports what in a circle, what nothing imports at all. Exports to Mermaid,
Graphviz or CSV for Visio.

---

## Checking it

```
python tests/run_tests.py            every check, and a report at the end
python tests/run_tests.py -k phone   only sections whose title matches
python tests/run_tests.py --list     the sections
python tests/run_tests.py --stop     stop at the first failure (the old way)
python tools/check_layout.py         the layout, measured in a real browser
```

A failure no longer stops the run: each section of the suite runs on its
own, and the report lists every failed check, every section that couldn't
run and why (a missing package, say), and which only failed because an
earlier one did — fix those first. It exits non-zero unless everything
passed.

## Updates in one click

Each app checks for, verifies and installs its own updates — and installs
nothing it can't prove you signed.

**Once, on the machine you build releases on:**

1. Make a signing key — Agent Jo: `python tools/sign.py --make-key`;
   Agent Jo Jobs: `python tools/make_release.py --make-key`. Keep
   `signing-key.private` secret and out of git; `signing-key.pub` ships
   with the app.
2. Build a release (`python tools/make_release.py`). It signs exactly the
   files in the zip and writes `dist/latest.json` beside it.
3. Install that first signed release by hand, so your copy carries the key.

**Each new version after that:** build, then upload the zip and
`latest.json` to the same place (a GitHub release works). In the app — Agent
Jo: Settings → Updates; Agent Jo Jobs: Engines → Updates — paste
latest.json's address once, then **Check for updates → Install update**, and
restart. **Roll back** puts the previous version back exactly.

Refused, with the reason: anything not signed by the key in your copy, a
file changed after signing, a checksum mismatch, an older build, a release
carrying a different key, and any file outside the app folder or in its
Python environment. Your data is never touched.

## Keep it running in the background

Schedules, watchers, folder sweeps and auto-resume used to stop the moment
you closed the window. Install Agent Jo as a background service and they
keep going: it starts when you log in and is restarted if it stops.

```
python tools/service.py install      start now, and at every log-in
python tools/service.py status
python tools/service.py uninstall
```

Or **Settings → Run in the background → Install**. Linux uses a systemd user
unit, macOS a launchd agent, Windows a Task Scheduler task. All of them run
as you, so none needs an administrator. It serves on
`http://localhost:8765`. The usual Start Agent Jo shortcut opens that copy
instead of starting a second one.

However many copies are open, **only one runs the schedules**, so a
scheduled email never goes out twice. If that copy stops, another open one
takes the schedules over within 15 seconds.

## It tells you when it needs you

**Settings → Phone notifications.** When something is held for your OK, a
scheduled task fails, a feature is switched off after repeated failures, or a
task is blocked on you, Agent Jo tells you on your phone:

- **ntfy**: free push notifications with no account. Pick a hard-to-guess
  topic, then subscribe to it in the ntfy app.
- **Telegram**: make a bot with @BotFather, paste its token, send it "hi",
  then click **Find my chat**. A held call arrives with **Approve / Refuse**
  buttons that work from anywhere, with no port opened to the internet.
  `/status`, `/pause` and `/resume` work from the chat. Only your own chat is
  listened to.

Only the same one-line summary the approval queue shows leaves the machine,
never an email's body. Repeats are folded together, and there's an hourly
ceiling.

## Goals: outcomes, not just instructions

Tasks & Scheduler → **Goals**, or just ask: *"make it a goal to land five
interviews by 30 November, budget $5"*. A goal has an objective, checkable
"done when" criteria, an optional deadline and an optional budget of its own.
It's worked on a few times a day. Each run picks up from the last checkpoint
and gets a bigger budget than a chat turn (40 tool calls and 20 minutes, set
with `AGENT_GOAL_MAX_ROUNDS` and `AGENT_GOAL_TIME_LIMIT`).

It stops on its own, and tells you, when the goal is met (with evidence), when
it's blocked on you, when it's out of budget or past its deadline, or when
three runs in a row make no progress. Anything leaving the machine still
waits for your OK.

## Triggers: work that starts when something happens

Tasks & Scheduler → **Triggers**. Each event runs your instruction, or wakes
a goal so it works on the event straight away:

- **A webhook is called**: `POST /hooks/<id>` from GitHub, Stripe, a form or
  your own script. The secret goes in `Authorization: Bearer …`. For GitHub,
  use it as the webhook secret, and each delivery's signature is checked. To
  reach the webhook from outside this machine, use a tunnel or Tailscale and
  add that name to `AGENT_ALLOWED_HOSTS`.
- **New email arrives**: an IMAP folder, filtered by sender and/or subject.
  Read-only: nothing is marked read, moved or deleted. Use an app password.
- **A file lands in a folder**, optionally matching a pattern like `*.pdf`.

Mail or files already there when you add a trigger never fire. Event content
reaches the agent as untrusted data. Triggers run in safe mode unless you
allow more, each has an hourly ceiling, and anything leaving the machine
still waits for your OK.

## Windows installer (.exe)

**Build it** (on a Windows PC): double-click **Build Windows Installer.bat** in
this folder. It installs Inno Setup with winget if you don't have it, and
writes `dist\AgentJo-Setup-<version>.exe` a minute later. No Windows PC to hand?
Put the folder on GitHub and run **Actions → Windows installer → Run
workflow**: the .exe is attached to the run.

**What the .exe does:** installs into your own user folder
(`%LOCALAPPDATA%\Programs\Agent Jo`) with no administrator prompt; finds
Python 3.10+, or downloads the official one from python.org and installs it
for you alone; sets up the app's packages; and adds Start menu and
desktop shortcuts. Uninstall it from Settings → Apps. Your data, in
`%USERPROFILE%\.local_agent`, is never touched, so reinstalling or upgrading keeps
everything.

**Windows will warn the first time** ("Windows protected your PC"), because
the .exe isn't code-signed: click **More info → Run anyway**. Removing that
warning needs a code-signing certificate, which is a paid, separate step.

## The job search is a separate app

**Agent Jo** is the agent: chat, documents, code map, schedules, the crew.
The job search — roles, held drafts, auto-apply — is **Agent Jo Jobs**, a
separate app with its own download, its own data and its own code. It used to
ship in this folder and share this app's data; since build 155 nothing is
shared. See UPGRADING.md if you used it from here.

**Your career in the chat.** If you've given Agent Jo Jobs your CV, ask
Agent Jo about it — "what did I do at Standard Bank?", "write me a LinkedIn
summary from my CV" — and it looks it up (the `career_lookup` tool): the CV
sections that answer, your job profile, and answers you've given application
forms. It reads Agent Jo Jobs' data folder (`~/.agent_jo_jobs`, or
`AGENT_JOBS_HOME`) and nothing else; it shares no code with the job search,
never writes to it, and leaves out sensitive answers you haven't switched on
for reuse there.

## Install

Windows, one click:

```
1. Download the latest release and unzip it
2. Double-click install.bat
```

It finds Python, **asks before installing it** if it's missing, sets up an
isolated environment, checks the download hasn't been tampered with, and puts
a shortcut on your desktop.

macOS — double-click `install.command`.
Linux — `./install.sh`.

Same as Windows: it finds Python (offering Homebrew on a Mac if you have it,
never installing it for you), sets up an isolated environment, and checks the
app actually loads. `--check` surveys the machine without changing anything.

It opens at `http://127.0.0.1:8765`. First run asks for an API key — or point
it at Ollama and it runs entirely offline.

---

## Three things that make it different

**It says what it doesn't know.** Draft applications claiming a tool or an
employer your profile can't support are held, with the exact claim shown, and
you decide. The same principle runs throughout: rates carry confidence
intervals, a partial document index says it's partial, and a fine-tune that
scores worse than its base is reported as worse.

**Consequence, not permission.** Under full access it doesn't prompt for
everything — being asked constantly teaches you to click through without
reading. Reads run. Anything leaving the machine — mail, form submissions, API
posts — waits with its **exact inputs shown**: not "send_email" but *"to
hiring@acme.com · 'Application — Senior Data Engineer' · 1,400 characters"*.

**It can be checked.** 1,235 tests, a tamper-evident audit chain, signed
releases, and an honest [build log](CHANGELOG.md) recording what broke and
why. Most of what's in there was found by running it on a real machine, not by
planning.

---

## Licence

Free for personal, research, educational and non-profit use under the
[PolyForm Noncommercial License 1.0.0](LICENSE).

**Commercial use needs a licence** — running it inside a business, using it for
client work, or bundling it into something you sell. See
[COMMERCIAL.md](COMMERCIAL.md); I'd rather agree something sensible than have
you guess.

## Verifying a download

This runs commands on your machine, so check it's the real thing:

```
python tools/sign.py --verify
```

See [VERIFYING.md](VERIFYING.md). A valid signature proves the copy is
unaltered — it does not stop anyone copying the code, which is what the
licence is for.

---

## Documentation

| | |
|---|---|
| [UPGRADING.md](UPGRADING.md) | Updating an install you already have |
| [CHANGELOG.md](CHANGELOG.md) | How it was built, and what was learned |
| [COMMERCIAL.md](COMMERCIAL.md) | Using it in a business |
| [VERIFYING.md](VERIFYING.md) | Checking a download |
| [SIGNING.md](SIGNING.md) | Signing your own releases |

Built by **Itumeleng Nthite** — Symbolic Synapse, Johannesburg.
