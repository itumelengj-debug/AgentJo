# Agent Jo — build log

How this was built, in order. Each entry says what was wrong, what was done
about it, and — where it matters — what was learned the hard way.

Kept because the reasoning is often more useful than the result: most of these
were found by running the thing on a real machine and watching it fail, not by
planning.

See [README.md](README.md) for what Agent Jo actually is.

---

# Agent Jo — your local AI agent (web app)

Agent Jo is a private AI assistant that runs entirely on your own machine. It's a
FastAPI backend with a custom browser frontend: you chat with it, and it can use
tools (web search, your documents, the file system, scheduled jobs, email
outreach) to actually get work done. Your conversations, memory, and credentials
stay on your computer.

It can talk to **Claude** (via the Anthropic API), **DeepSeek**, and local
**Ollama** models, and it picks a sensible engine automatically — or you can pin
one.

---

## Quick start

```bash
# 1. install the web app's dependencies
pip install -r requirements-web.txt

# 2. give it at least one model to talk to (any one of these)
setx ANTHROPIC_API_KEY "sk-ant-..."      # Windows (new shell after)
#   export ANTHROPIC_API_KEY=sk-ant-...  # macOS/Linux
#   …or run a local model with Ollama and set AGENT_BACKEND=ollama

# 3. run it
python run_web.py                 # serves http://127.0.0.1:8000 and opens your browser
python run_web.py --port 9000     # pick a port
python run_web.py --host 0.0.0.0  # expose on your local network (see security note)
```

`run_web.py` automatically relaunches itself using the project's virtualenv
(`.venv`) if you started it with a different Python — so `python run_web.py`
always runs from the environment that has the app's dependencies, and optional
extras like voice input work without the "installed but the app can't see it"
trap. (Set `AGENT_NO_REEXEC=1` to disable this if you manage environments
yourself.)

On first launch you'll be asked to set a password; the workspace is
password-protected from then on. Run the command from the project root so
`web.server` is importable.

**Deploying an update:** replace the whole project folder (keep your `.venv`),
restart `python run_web.py`, and hard-refresh the browser (Ctrl+F5) so it picks
up the new frontend. Your data and settings live outside the project folder, so
they survive upgrades.

---

## The workspace

The left sidebar is the map of everything Agent Jo can do:

- **Chat** — the main thread. Streams responses, runs tools as needed, and shows
  progress. Attach files/images with the paperclip; stop a runaway turn with the
  Stop button.
- **Manage engines** — pick the model (Auto, Claude, DeepSeek, an Ollama model, or
  a custom OpenAI-compatible endpoint). Each option shows a live capability score
  and observed speed that **adapt as you use them**. When you add a custom engine
  you can give it an input/output price (USD per million tokens); the running
  **Est. cost** then covers every engine — Claude, DeepSeek, local (free), and your
  custom ones — and hovering the cost figure breaks it down per engine.
  You can also **switch the actual models** in Settings → Models: pick your cloud
  model and fast model (Claude ids), and choose your local model from a dropdown
  populated live from your installed Ollama models (or type the id if Ollama
  isn't running). Changing a model takes effect immediately — the engine
  rebuilds on save.

By default the picker starts on **Auto** (smart routing across engines), but you
can change that in Settings → Engine &amp; models → **Default engine**: pick any
specific engine to always start there instead of Auto. Whichever engine you
select in the top bar is also **remembered** — the app reopens on the engine you
last used rather than resetting each time.
- **Documents** — drop in files to index them; the agent can then answer from your
  own documents (RAG).
- **Memory** — facts and preferences it remembers across chats; review and prune.
  It learns two ways: it saves a memory when you state something durable
  ("remember…", "from now on…"), and after each turn it quietly extracts any
  lasting facts on its own (toggle with `AGENT_AUTO_LEARN`).

Beyond stated facts, Agent Jo **learns from its own work** (the "Learned" KPI
tracks this):

- **Playbooks** — every time it completes a multi-step task, the steps that
  worked (including *how each was verified*) are distilled into a reusable
  playbook. Ask for something similar later and the playbook is injected into
  its context: it reuses the proven route instead of rediscovering it. The more
  real work it finishes, the better it gets at your recurring jobs.
- **Lessons** — when a step blocks on you (a login, a credential, an install) or
  a task fails, the reason is recorded. Next time related work comes up, the
  lesson is surfaced up front ("this needed npm 2FA last time") so it plans
  around the obstacle instead of hitting it again.
- **Cross-conversation recall** — it can search *all* your past conversations,
  not just the current one. Clearly relevant snippets are injected automatically,
  and it has a `recall_conversations` tool for explicit digging when you say
  "what did we decide about…" or "we discussed this last week". Try it: decide
  something in one chat, start a new chat, and ask what was decided.

All three are deterministic and free — no extra model calls, works fully
offline — and they inject only on a genuine match, so ordinary turns stay lean.
When a local Ollama embedding model is available, all three upgrade from
keyword matching to **semantic matching**: a fully reworded request ("push my
site live") still finds the playbook it learned as "deploy hugo blog to
netlify". Without an embedder, behaviour is the deterministic keyword match.

Two more awareness mechanisms round this out:

- **Since-you-were-away briefing** — the first message of a *new* conversation
  briefs the agent on what happened in the interim: autonomous actions that
  fired (auto-pilot sends, watcher hits, resume sweeps) and tasks that changed
  state. It opens the conversation already knowing, instead of you re-explaining.
- **Loop lessons** — if a turn ever dies retrying the same tool without
  progress, that dead-end is recorded as a lesson, so the next related attempt
  starts with "diagnose first, don't just retry" already in context.
- **Permissions** — what the agent may do without asking (full access vs. confirm
  each action), web access, spoken replies.
- **Scheduler** — recurring jobs (run a prompt on a cadence), and the **Tasks** tab
  where multi-step plans live (with **Auto-resume** controls).
- **Outreach** — send email as you, build personalized campaigns, and run
  **Auto-pilot** and **Watchers**.
- **Autonomy** — one dashboard for everything the agent can do unprompted, with a
  master pause.
- **Settings** — model/runtime options that persist across restarts.

---

## Using & testing the automation features

These are the newer, more powerful pieces. Everything here is **off by default**,
acts only inside limits you set, and is logged. Below is how to use each one and,
just as important, how to try it safely first.

> A note on testing delivery: the offline test suite exercises all of this against
> fakes, but a real email send needs your real SMTP account, and real web
> monitoring needs the live internet — so the first real send/fetch from your
> machine is the true end-to-end test. Use the dry-run / observe / preview paths
> below to rehearse without consequences.

### 1. Outreach — send email as yourself

**Use it:** open **Outreach → Setup**, enter your SMTP details (host, port,
username, password/app-password, From address, optional signature). Your password
is encrypted at rest; the status view never shows it back. Tick **Enable sending**
to allow manual sends.

- **Quick send** tab: write a one-off email. **Preview (dry run)** renders and logs
  it *without sending*; **Send** delivers it.
- **Campaign** tab: paste recipients one per line as `email, First Name, Company`,
  write a subject and body using `{first_name}`, `{company}`, `{email}`
  placeholders, then **Render drafts** to see exactly what each person would get
  (bad addresses are flagged and skipped). Sending requires ticking **"I've
  reviewed — really send"**.

**Test it safely:**
1. Fill in Setup but leave **Enable sending** off, click **Save** → status should
   read "configured, sending OFF".
2. In Quick send, write a message and hit **Preview (dry run)** → it confirms the
   render and sends nothing. Check the **Activity** tab; you'll see a `dry_run`
   entry.
3. Tick Enable sending, then **Send test to myself** in Setup → if your SMTP is
   right, it arrives in your own inbox. That's your delivery confirmation.

### 2. Auto-pilot — hands-off sending, inside a fence

**Use it:** **Outreach → Auto-pilot**. Add **approved recipients** and/or
**approved domains**, set a **per-run cap**, then **Arm auto-pilot**. Now a job can
send without per-email approval — but only to approved addresses, capped, and
logged. Paste a job (recipients + subject/body templates) and **Run autonomously
now**, or schedule it (next section).

**Test it safely:**
1. Leave it **disarmed** and click **Dry run (no send)** on a job → it shows what it
   *would* send and delivers nothing.
2. Add just your own address to the allowlist, arm it, set the cap to 1, and **Run
   autonomously now** with one recipient (you). Confirm it arrives and that the
   Activity log tags it `sent` / `auto`.
3. Add an off-list address to the job and run again → it should report it
   **blocked (not approved)** and skip it. That's the fence working.
4. Hit **⏻ Pause all** (or the Autonomy dashboard) to disarm instantly.

### 3. Recurring auto-pilot — run a job on a schedule

**Use it:** in the Auto-pilot tab, under **"Or run it on repeat"**, pick a cadence
(daily / weekdays / hourly / weekly / every-N-min) and **Schedule it**. It becomes
a real recurring schedule (shown with a ✉ prefix in **Scheduler**), and each fire
still passes through the arm switch + allowlist + caps.

**Test it safely:** schedule a job, then in **Scheduler** click **Preview next** on
that row → a full dry run showing how many it *would* send and who's blocked,
delivering nothing. Use **Run now** to fire it immediately if armed.

### 4. Watchers — monitor a source, act on change

**Use it:** **Outreach → Watchers**. Give it a **URL** or a **search query**, a
cadence, and an instruction for what to do when it changes. Choose a **mode**:
**Observe** (the agent may draft, but any send is neutralized to a logged
dry-run — recommended to start) or **Send** (may deliver, still only to approved
recipients). The first check captures a baseline; after that it acts only when the
content actually changes.

**Test it safely:**
1. Create a watcher in **Observe** mode on a page you can edit or that changes
   often.
2. In **Scheduler**, find it (👁 prefix) and click **Preview next** → it fetches and
   compares **without consuming the change**, telling you whether it changed and
   what mode it would act in.
3. Click **Run now** once to set the baseline, change the source, **Run now** again
   → the last-run summary shows "change detected (observed)". Because it's Observe
   mode, nothing was sent — check Activity to confirm any send shows as
   `draft_only`.

### 5. Auto-resume — pick up stalled tasks automatically

**Use it:** **Scheduler → Tasks**. The agent keeps multi-step plans here. Arm
**Auto-resume** to have it periodically sweep for tasks that have *stalled* and
nudge them forward on its own. It's fenced: only tasks idle ≥ your threshold, a
capped number of **no-progress** attempts each (progress resets the counter), never
tasks waiting on you, and at most N per sweep. Whether it may run commands/writes
unattended is a separate **full-access** toggle (off by default).

**Test it safely:**
1. Let the agent create a multi-step task (ask it to do something with 3+ steps),
   or leave one half-finished.
2. Arm Auto-resume with a short idle threshold and **full access off**.
3. Click **Preview** → it lists exactly which stalled tasks a sweep would pick up,
   with no side effects.
4. Click **Run sweep now** and watch the task move. Restarting a task (or touching
   it yourself) resets its attempt budget.

### 6. Autonomy dashboard — see and control all of it

**Use it:** the **Autonomy** button (◉). One card per system (auto-pilot, watchers,
auto-resume) shows whether it's armed, its limits, and a **last-fired / next-fires**
line. Each card has an **arm/disarm toggle**; below them is a merged, time-sorted
**feed of recent autonomous actions** across outreach, watchers, and auto-resume.
The **⏻ Pause all autonomy** button disarms everything and disables every
autonomous schedule in one click.

The dashboard also holds a **spend cap**: set a dollar amount and the running cost
is tracked against it with a progress bar. When the cap is reached, **autonomous
actions pause automatically** — scheduled auto-pilot jobs, watcher actions, and the
auto-resume sweep all hold rather than spend past your limit (they resume if you
raise or clear the cap). Interactive chat is never hard-blocked, only warned (the
**Est. cost** figure turns amber as you near the cap, red once crossed). The cap
persists across restarts; 0 means no limit.

**Test it:** arm one system, open the dashboard, confirm its card shows ARMED with
timing; toggle it off from the card; do an action and confirm it appears in the
feed; then hit **Pause all** and confirm every card flips to off. For the cap, set
a tiny amount (e.g. $0.01), run a chat turn to cross it, and confirm a scheduled
auto-pilot/auto-resume job then records "budget reached" instead of spending.

### 7. Task handling — restart-in-place & blocked steps

When the agent works a multi-step job it now: restarts the **same** task if you say
"start over" (keeping history) instead of spawning a duplicate; marks a step
**blocked** with the exact handoff when it hits something only you can do (a login,
an install, a credential) rather than stalling silently; and won't mark a task
"complete" while a step is blocked. Blocked steps show a `[B]` marker and an amber
"needs you" note in the **Tasks** tab, and there's a **Restart in place** button on
active tasks.

### 8. Issues — log app problems for the developer

When Agent Jo **itself** misbehaves (a wrong answer, a dead button, an error
mid-turn), capture it instead of trying to remember it: open **🐞 Issues** in
the sidebar, describe what went wrong in one line, and hit **Capture report** —
or just tell the agent in chat ("report an issue: the export button did
nothing") and it files one itself.

**Errors are also picked up automatically** — no action needed. The app watches
itself at three layers: every API call a button makes that fails or returns an
error (recorded with the button that was clicked and the server's error
detail), uncaught JavaScript errors in the page, and unhandled server faults
(HTTP 500s, chat-turn and scheduler exceptions). These land in the
**Auto-captured errors** list at the top of the Issues panel and are written to
`errors.jsonl`, so they survive a restart. Click **Report** next to any of them
to promote it to a full report, and every report you capture bundles the recent
auto-captured errors automatically. Duplicates are throttled and intake is
capped per hour so a broken loop can't flood the log.

Each report bundles your note with the
recent conversation excerpt, the engine in use, environment info, and the most
recent auto-captured errors. Everything is saved locally to
`problems.jsonl` in your data folder — nothing is sent anywhere.

To get a fix: **Copy** one report (or **Copy all for the developer**) and paste
it into a session with Claude — that block is a complete bug report. **Clear
all** once they've been addressed. Reports can contain conversation excerpts,
so skim before sharing.

**Test it:** trigger a harmless error — e.g. open **Scheduler → Tasks** and click
**Run sweep now** *without* arming auto-resume (the server refuses with a 400).
Open 🐞 Issues: that refusal should already be listed under Auto-captured
errors, tagged with the button you clicked. Click **Report** on it and confirm a
full report appears below. Then type a note, Capture, Copy, and paste somewhere
to see the full block; ask the agent in chat to "report an issue: test entry"
and confirm another report shows up; Clear all when done.

### 9. Second opinion — local and cloud checking each other

Agent Jo can have **two models collaborate on one answer**, across the
local/cloud divide. Turn on the **Second opinion** toggle in the composer (next
to Full access), or just ask in the message — "…, get a second opinion" or
"double-check this". Then:

1. One engine answers as usual (the solver).
2. A model on the **opposite tier** reviews it — if the answer came from the
   cloud (Claude/DeepSeek), your **local** Ollama model critiques it for free;
   if it came from a local model, the **cloud** model reviews it for higher
   quality. Crossing tiers is the point: it's a genuine independent check, not a
   model grading its own work.
3. If the reviewer is satisfied, the answer stands (a note says who reviewed
   it — no rewrite, minimal extra cost). If the reviewer finds problems, the
   solver revises **once** to fix them, and the reply notes it was revised.

It's **opt-in per request** — off by default, so ordinary turns stay fast and
cheap. It needs two tiers configured (e.g. Ollama running *and* a cloud key);
with only one engine there's no second tier to review from, and the turn runs
normally. The whole pass is fail-safe: if the reviewer or revision hiccups, you
get the original answer unchanged — enabling it can never make a turn worse. The
review call is counted in the cost estimate and respects the spend cap like any
other.

**Test it:** enable Second opinion, ask something a small model tends to get
wrong (a tricky fact, a subtle bug in a code snippet), and watch for the
"Reviewed by …" or "Revised after a second opinion from …" note at the end of
the reply. Ask the same thing with the toggle off to compare.

### 10. Teamwork — local grunt work, cloud reasoning

Flip the **Teamwork** toggle in the composer and Agent Jo splits work across the
cost divide: your cloud engine keeps the judgement-heavy reasoning and final
answers, while the **free local model** picks up the grunt work —

- The cloud engine is coached to hand bulk mechanical jobs (summarising long
  text, extraction, reformatting, simple first drafts) to a `delegate_to_local`
  worker instead of burning paid tokens on them.
- **Sub-agents run on the local model by default**, and if the local worker
  errors or comes back empty, the sub-task **escalates to your primary engine
  automatically** — you get the answer either way, just cheaper when possible.
- The model can pin a tier per sub-task (`tier='cloud'`) when something truly
  needs full capability.

It's persisted like other settings, needs Ollama running to do anything (with
no local model it changes nothing), and pairs naturally with **Second
opinion** — local does the cheap work *and* the free reviewing.

**Test it:** turn Teamwork on, paste a long article and ask for "a summary plus
your assessment of whether the argument holds" — watch the console show
"delegated to local worker" for the summary while the assessment stays on your
engine. Then stop Ollama and confirm behaviour falls back to normal.

Honest caveat: delegation quality is bounded by your local model. The cloud
engine is told to verify worker output, and correctness-critical work stays on
the primary engine, but a weak local model can still produce summaries that
miss nuance — if a delegated result looks off, say so and the agent will redo
it itself.

### 11. MCP servers — plug in the whole tool ecosystem

Agent Jo is an **MCP client**: it can connect to any Model Context Protocol
server and use its tools mid-conversation — GitHub, filesystems, databases,
Slack, browsers, and hundreds more from the open ecosystem. One protocol, huge
surface area.

Open **⬡ MCP servers** in the sidebar to add one:

- **stdio (local command)** — the common case. Give it a name and the command
  that starts the server, e.g.
  `npx -y @modelcontextprotocol/server-filesystem C:\Users\itume\Documents`
  or `npx -y @modelcontextprotocol/server-github` with
  `GITHUB_TOKEN=ghp_...` in the environment box. Agent Jo spawns it, does the
  MCP handshake, and lists its tools. (Windows: `npx`/`uvx` resolve
  automatically; first run of an npx server downloads it, so give it a moment.)
- **http (remote URL)** — for hosted MCP servers: paste the endpoint URL and
  any auth headers.

Connected tools appear to the agent as `mcp_<server>_<tool>` alongside the
built-ins, and it's told when external tools are available. Toggle a server off
to hide its tools instantly; Retry reconnects after a failure (errors land in
🐞 Issues automatically, with the server's stderr tail attached). Config lives
in `mcp.json` in your data folder.

**Trust note, plainly:** adding a server hands the agent every tool that server
offers, running with your local permissions (stdio) or your credentials (http).
Only add servers you trust, and prefer read-only or scoped ones (e.g. point the
filesystem server at one folder, not the whole drive).

**Honest limits:** this is a deliberately minimal, dependency-free client — it
implements the MCP handshake, tool listing and tool calls, which is what makes
servers useful to an agent. It does not implement MCP resources, prompts,
sampling, or change notifications; a server that only offers those won't show
tools. Tools are available to the main agent (not to sub-agents) in this
version. A broken or slow server degrades to an error message in the
conversation — it can never hang or crash the app.

**Test it (offline):** the test suite ships a fake MCP server. Add a server
named `fake` with transport stdio and command
`python tests/fake_mcp_server.py` (from the app folder) — it should connect and
show 4 tools. Then ask the agent: "use your mcp echo tool to say hello" and
watch the `⮑ MCP tool` line in the console.

### 12. Privacy shield — sensitive data stays home

For POPIA-conscious work: Settings → Engine &amp; models → **Privacy shield** has
three modes.

- **off** (default) — behaviour unchanged.
- **mask** — SA ID numbers (checksum-validated), card numbers (Luhn-validated),
  email addresses, SA phone numbers, and API keys/secrets are replaced with
  stable placeholders like `⟦EMAIL_1⟧` **before any engine sees them**, and
  restored in what you read. The swap holds across the whole loop: the model
  works with placeholders; tools receive the real values; anything sensitive a
  tool *returns* is masked again before the model sees it; replies (including
  streamed ones) come back to you with real values. The mapping is stable per
  conversation, so `⟦EMAIL_1⟧` means the same address on turn one and turn
  twenty.
- **local** — a turn containing sensitive data is routed **entirely to the
  local model**; nothing leaves the machine (this overrides even a pinned cloud
  engine — that's the point). With Ollama off, it falls back to masking. Every
  protected turn says so in a footer note, and the console shows what happened.

**Honest limits, plainly:** detection is pattern-based with checksums — it
catches the classic leaks (IDs, cards, contacts, credentials) with very few
false positives, but it does **not** detect names, addresses, or other
free-text PII; treat it as a strong guard, not an anonymity guarantee. Enable
it before pasting sensitive content — earlier raw turns in a conversation
aren't retro-masked. Images bypass masking (there's nothing to rewrite), so use
local mode for sensitive screenshots. The placeholder map lives in memory only
(never written to disk); after an app restart, a placeholder from an old
conversation can't be restored — start a fresh conversation for masked work
after a restart. In mask mode, auto-learned memories may store placeholders
rather than real values.

**Test it:** set mode to **mask**, send "Draft a note to piet@example.co.za
about ID 8001015009087" on a cloud engine, and watch the console print
`privacy: masked 2 sensitive item(s)` while the reply comes back with the real
address — and the footer confirms the shield ran. Flip to **local** with Ollama
running and the same message routes to your local model instead.

### 13. Watched folders — documents flow in by themselves

In **Documents**, add any folder under "Watched folders" and Agent Jo indexes it
immediately, then keeps it fresh on a managed schedule: new files are added,
edited files are re-indexed, unchanged files cost nothing (change detection is
per-file, by modification time). Pause or remove a folder any time; **Scan
now** forces a sweep; sweeps that did work are logged, and errors land in 🐞
Issues. Supported types are the same as manual indexing (text/code files, plus
.pdf/.docx with the optional libraries).

Long-term **memories** also gained the semantic upgrade playbooks already had:
with a local Ollama embedding model running, a fully reworded question ("when
do I push my website online?") still finds the memory saved as "deploys his
hugo blog to netlify" — and without an embedder, behaviour is exactly the
keyword search it was before.

**Test it:** watch a folder, drop a new .md file in, hit Scan now, and ask the
agent something answerable only from that file.

### 14. Audit trail — a tamper-evident record of everything it did

Open **▤ Audit** in the sidebar for an append-only record of the agent's
activity: every **turn** (which engine answered, review/privacy involvement),
every **tool call** (with outcome and timing — including MCP tools and
sub-agent work), every **autonomous action** (each schedule fire with its
result), and **configuration changes** (settings, web access, teamwork, budget,
MCP servers, watched folders).

Entries are **hash-chained**: each carries the SHA-256 of the previous line, so
editing, deleting, or inserting anything breaks the chain — **Verify chain**
walks the whole file and reports the exact line if it does. Filter by kind,
**Copy CSV** for a spreadsheet or **Copy text** for a readable log. The file
rotates at ~2 MB with the old file's final hash recorded in the new chain, so
history stays linkable. Toggle with the **AUDIT** setting (on by default);
recording is fail-safe and can never affect the action being recorded.

**Honest notes:** this is local-only evidence for *you* — tamper-evident, not
tamper-proof (someone with disk access could rewrite the whole chain; true
non-repudiation needs an external anchor). Tool entries include a truncated
copy of tool inputs, which may contain real values — same locality and
sensitivity as `agent.db` itself.

**Test it:** send a message, run a tool, flip a setting — then open ▤ Audit and
watch the entries appear; hit Verify chain; edit a middle line of
`audit.jsonl` in Notepad and Verify again to see it catch the exact line.

### 15. Undo — a time machine for the agent's file changes

The audit trail tells you what the agent did; **⟲ Undo** lets you take it
back. Before every `write_file` mutation the previous version is snapshotted
into a local shadow store — structurally, at the tool boundary, so the model
can't forget to do it. In the panel: **Diff** shows exactly what changed;
**Restore** puts the old version back; undoing a *creation* deletes the file
the agent made. A restore is itself snapshotted first, so you can undo an
undo. The store is bounded (5 MB per file, ~200 MB / 400 entries total, oldest
pruned) and every restore lands in the audit trail as an `undo` entry.

Toggle with the **TIMEMACHINE** setting (on by default).

**Honest scope:** this protects the agent's own file writes. It cannot cover
`run_command` side effects (arbitrary programs) or writes made by external MCP
servers — those remain visible in the audit trail but aren't restorable here.
Files over 5 MB are recorded but their contents aren't stored.

**Test it:** ask the agent to overwrite a file you care nothing about, open
⟲ Undo, Diff it, Restore it, then open ▤ Audit and see the `undo` entry.

### 16. Self-improve — the app builds its own next feature

Ask Agent Jo, in normal chat, to add a feature or fix a bug **in itself** —
"add a word-count under every reply — build it into yourself" — and it runs
the same pipeline a careful engineer would:

1. **Sandbox** — it copies its own source into a workspace and makes ALL edits
   there (writes are auto-approved only inside the sandbox; the live app is
   untouchable to it).
2. **Test gate** — the modified copy must pass the app's own full test suite
   (the same ~500 checks that gate every human change). Fails → it keeps
   fixing; nothing reaches you until green.
3. **Proposal** — the **⇪ Self-improve** panel shows the request, the test
   result, and a per-file diff (changes to safety modules are flagged for
   extra scrutiny).
4. **You apply** — one click copies the changes into the live app, with every
   replaced file snapshotted to **⟲ Undo** first, and the whole thing recorded
   in **▤ Audit**. Restart to activate. Changed your mind? Restore from Undo.

The agent can *propose*; only you can *apply*. There is no autonomous path to
self-modification, deletions are never applied automatically, and the whole
feature has a kill-switch (**SELFIMPROVE** setting).

**Honest limits:** quality of the built feature depends on the model doing the
building (use Claude or Auto for meaningful changes; a small local model will
struggle to pass the gate). The test suite is a strong gate, not a perfect
one — it proves nothing broke that's tested, so review the diff before
applying, especially anything flagged. Big features may take several minutes
(the suite runs inside the sandbox each iteration). And the first restart
after applying is the real proof — which is exactly why every applied file
sits in ⟲ Undo.

**Test it:** ask "add a version marker comment to the top of agent/config.py —
build it into yourself", watch it sandbox → test → propose, review the
one-line diff in ⇪, apply, restart.

### 17. Data pipelines — Medallion builds with AS-IS/TO-BE regression proof

Ask the agent to build a data pipeline from local CSV/JSON files and it acts
as an autonomous data engineer with a deterministic safety harness — the core
of the agentic-data-pipeline design, implemented at local scale:

- **Medallion layers:** Bronze is built mechanically — append-only, with
  `_load_ts` / `_source_file` / `_process_id` lineage columns. The agent
  authors the **Silver** SQL (dedupe via window functions, casting,
  conformance) and **Gold** aggregations, in two variants: **AS-IS** (trusted
  baseline logic) and **TO-BE** (the new logic to prove).
- **Source Aligner:** inputs are frozen (copied + hashed) before a run, so
  both variants provably process identical data — any output difference is
  logic, not timing.
- **Checksum-bisection data diff:** output tables are compared by hashing
  primary-key segments and recursively bisecting only mismatches until the
  exact rows and columns that differ are isolated — with float tolerance so
  engine precision noise doesn't false-flag. A clean match is proven from
  segment hashes alone (zero row-by-row comparison).
- **Execution-grounded self-correction:** on mismatch, the machine-readable
  diff is handed to the model, which rewrites the TO-BE SQL from the
  evidence; the loop retries up to 5 iterations, then **escalates** with a
  report of every attempt. Convergence/escalation is written to memory
  (episodic) and every iteration lands in ▤ Audit as `pipeline` entries.

Chat-driven (tools: `pipeline_create`, `pipeline_regression`,
`pipeline_diff`; status at `/api/pipelines`). **Try it:** "Build a pipeline
from C:\data\orders.csv: silver dedupes by id keeping the latest ts, gold
sums amount by region; make the AS-IS the dedup logic and the TO-BE a version
you then prove equal — run the regression."

**Honest scope vs the full design:** the local engine is SQLite standing in
for the design's DuckDB sandbox (same ephemeral role, zero install); there's
no Snowflake/BigQuery, Dagster/Airflow (the app's own scheduler can run
pipelines on cadence), or SQLGlot transpilation (one dialect locally).
Segment hashes are computed app-side — SQLite has no in-engine MD5 — so
locally the bisection win is work-and-isolation, not network transfer. Row
scale is laptop-scale, not billions. The *mechanics* — frozen inputs,
parallel isolated runs, value-level bisection diffing, capped self-healing
with escalation — are the real ones, verified by the test suite.

### 18. Blender lab — prompt-to-render 3D design

Ask the agent for a 3D scene ("a cobalt-blue perfume bottle on a marble slab,
soft studio lighting, shallow depth of field") and it acts as the designer:
it writes a Blender Python scene — geometry, Principled-BSDF PBR materials,
studio lighting, deliberate camera — and renders it **headless with Cycles**,
Blender's path tracer. You get the image; tell it what to change (or drag the
render back into chat so it can look and self-critique) and it refines
materials, lighting, and framing across iterations — the designer loop.

Setup: install Blender (free, blender.org). The app auto-detects it from PATH
or standard install locations; otherwise set **Settings → Blender path**.
Requires **Full access** (the script is arbitrary code in Blender — same trust
level as run_command). Renders and logs live under your data folder
(`blender/`), viewable via `/api/blender/image` links; every job lands in
▤ Audit. The harness runs `--factory-startup` for reproducibility, hard
timeouts, and self-heals a known quirk (distro Blender builds without the
denoiser get auto-retried without it).

**Honest expectations, plainly:** this is scripted, procedural Blender —
excellent for product shots, abstract compositions, architectural forms, and
anything parametric, where Cycles genuinely reaches photoreal with good
lighting and materials. It is **not** a text-to-3D generative model: it won't
sculpt artist-grade organic characters or guarantee photorealism of arbitrary
subjects from one prompt. Quality tracks the engine authoring the script —
use Claude or Auto; local models will produce crude scenes. Drafts render in
seconds-to-minutes on CPU; finals at high samples take longer.

**Test it:** Full access on, then: "Render a chrome sphere and a rough
wooden cube on an infinite white floor, one large soft key light from the
left, 85mm camera, Cycles 64 samples" — then iterate: "warmer light, lower
the camera, more roughness on the wood."

### 19. Photo to 3D — reconstruct an object from a picture

Drag a photo of an object into chat and ask for a 3D model of it. The agent
**looks at the image** (vision), decomposes the object into shapes with
proportions measured from the photo, rebuilds it in Blender with modifiers
and matched PBR materials and lighting — then **exports a real `.glb` mesh**
(download link in the reply, opens in Blender/any 3D viewer) alongside
turntable renders so you can judge the shape. Refine by dragging its render
back in next to your photo: it compares side-by-side and adjusts proportions
and materials until they match.

Runs on the Blender lab harness (feature 18): needs Blender installed and
**Full access**; jobs, renders and models live under `blender/` in your data
folder and land in ▤ Audit.

**Honest expectations, plainly:** this is *vision-grounded procedural
reconstruction*, not a neural image-to-3D network. A single photo can't show
the back of an object — the agent infers it. It's genuinely good for
**products, packaging, bottles, furniture, architectural forms, logos** —
man-made things describable as shapes — and it is **not** for faces, people,
animals or organic sculpts; no honest local tool does those from one photo
without a GPU-hungry ML stack. If you ever want the neural route (TripoSR-
class models), that needs a CUDA GPU and multi-GB model weights — it can be
wired as an external tool then, but it isn't shipped untested.

**Test it:** Full access on, attach a photo of a simple product (a bottle, a
mug, a speaker) and say "reconstruct this as a 3D model — export the glb and
show me two angles." Then: "the body is taller relative to the cap — fix the
ratio and match the brushed-metal look."

### 20. Neural photo-to-3D — the ML route, for organic subjects

Feature 19 reconstructs man-made objects by modelling shapes; this is its
neural sibling: a locally-installed image-to-3D network (TripoSR-class)
**lifts** the photo into a mesh — which is what handles faces, animals,
plants and sculptural forms. Agent Jo is the harness: give it a photo's path
on disk (Full access required), it runs your configured tool, then
**post-processes in Blender automatically** — normalised scale, studio
lighting, two turntable renders, and a clean `model.glb` — and hands you
download links. Jobs land in ▤ Audit; files live under `neural3d/`.

**Turning it on:** open **Settings → Neural 3D command** and click **Detect** —
the app looks for an installed TripoSR/InstantMesh-class tool and fills in a
ready command (preferring the tool's *own* venv, since these pin torch
versions that fight the system python). **Check** validates it without
running an inference. Save, then attach a photo path and ask for a neural
lift. If nothing is found, install a tool and paste its command manually
using `{image}` and `{out}` placeholders.

**Setup (your side of the deal):**
1. Install a tool, e.g. TripoSR: create a separate Python env, install
   PyTorch (CUDA build if you have an NVIDIA GPU; CPU works but inference
   takes minutes), `git clone` TripoSR and `pip install -r requirements.txt`.
   First run downloads its model weights (~1.5 GB) from Hugging Face.
2. Set **Settings → Neural 3D command** to its command line with the two
   placeholders, e.g.
   `C:\tools\tsr-env\python.exe C:\tools\TripoSR\run.py {image} --output-dir {out}`
3. Ask: "neural-lift C:\Users\itume\Pictures\statue.jpg into a 3D model."

**Honest status, precisely:** the harness — command templating, job
management, timeouts, mesh collection, the Blender post-process chain,
failure surfacing — is fully tested (the post-process ran against real
Blender). The neural inference itself I could not run in the build
environment (no GPU, no Hugging Face access), so your first real lift is the
integration test — failures come back with the tool's own log tail. Neural
meshes are impressive but imperfect: the unseen side is invented by the
network, topology is dense and unoptimised, and quality varies by subject
and tool. Between the two routes: **19 for products/packaging (clean,
editable geometry), 20 for organic subjects (plausible sculpts).**

---

> **Attaching files:** use the paperclip, or just **drag files anywhere onto the app** — an overlay appears, and dropped files are staged on your next message exactly as if you'd used the attach button (with remove chips before sending). Dropping onto the Documents modal's own drop zone still indexes into the knowledge base instead.

> **Sidebar organisation:** the tools are grouped into collapsible sections (click a heading to fold it; the app remembers your choices per group) — **Core** (engines, documents, memory), **Automation** (scheduler, autonomy, outreach), **Extend** (MCP, self-improve), **Safety** (undo, audit, issues, permissions) — with Settings at the bottom.

## Verify the whole thing offline

You don't need an API key or the internet to confirm the app is healthy:

```bash
python tests/run_tests.py
```

This runs an offline suite (**354 checks**) against a mock backend, covering the
engine, routing, memory, tasks, outreach, auto-pilot, watchers, auto-resume, and
the autonomy dashboard — including that sends are blocked off-allowlist, observe
mode never delivers, and pause-all disarms everything.

---

## Configuration

Most options are in the **Settings** panel and persist across restarts. A few are
set via environment variables before launch. The common ones:

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key (for the Claude engine). |
| `AGENT_BACKEND` | `hybrid` (default), `anthropic`, or `ollama`. |
| `AGENT_MODEL` / `AGENT_FAST_MODEL` | Override the Claude / fast model names. |
| `AGENT_OLLAMA_HOST` / `AGENT_OLLAMA_MODEL` | Local Ollama endpoint and model. |
| `AGENT_DEEPSEEK_KEY` / `AGENT_DEEPSEEK_BASE_URL` | DeepSeek (or any OpenAI-compatible) engine. |
| `AGENT_WEB_PASSWORD` | Set the workspace password headlessly (otherwise set it in-browser). |
| `AGENT_WEB` | `on`/`off` — allow the agent to use web search/fetch. |
| `AGENT_AUTO_LEARN` | `1`/`0` — after each turn, quietly extract durable facts into memory (default on). Uses the local Ollama model when available, otherwise the cloud model. |
| `AGENT_PRICE_IN` / `AGENT_PRICE_OUT` | Claude price, USD per million input/output tokens (default 3 / 15). |
| `AGENT_PRICE_DEEPSEEK_IN` / `AGENT_PRICE_DEEPSEEK_OUT` | DeepSeek price, USD per million tokens (default 0.28 / 1.10). |
| `AGENT_BUDGET_USD` | Session spend cap in USD (default 0 = no cap). When reached, autonomous actions pause; chat is only warned. |
| `AGENT_EMAIL_RATE_HOUR` / `AGENT_EMAIL_RATE_DAY` | Email send caps (default 30/h, 200/day). |
| `AGENT_TURN_TIMEOUT` | Max seconds for a single turn. |
| `AGENT_HOME` | Where your data lives (default `~/.local_agent`). |

There are many finer-grained knobs (routing, RAG, dedup, pricing) — they all have
sensible defaults; you only need them if you're tuning.

---

## Your data, privacy & security

Everything lives in your **data folder** (`AGENT_HOME`, default `~/.local_agent`),
separate from the project so it survives upgrades:

- `agent.db` — chats, memory, tasks, schedules.
- `settings.json` — your saved settings; `engine_stats.json` — learned engine scores.
- `secret.key` — the master encryption key (on Windows it's sealed with DPAPI).
- `auth.json` — your hashed workspace password.
- `email.json` — SMTP config (password encrypted); `email_log.jsonl` — send audit trail.
- `watch_state.json` / `watchers_log.jsonl` — watcher snapshots and run history.
- `autoresume.json` / `autoresume_state.json` / `autoresume_log.jsonl` — auto-resume policy, attempts, and log.
- `problems.jsonl` / `errors.jsonl` — your captured issue reports and the auto-captured error log (local only; see 🐞 Issues).
- `web.json` — your saved web-access on/off choice.
- `mcp.json` — your configured MCP servers (⬡ MCP servers).
- `folders.json` / `folderwatch_log.jsonl` — watched folders and their sweep log.
- `audit.jsonl` (+ rotated `audit-*.jsonl`) — the hash-chained audit trail (▤ Audit).
- `timemachine/` — pre-write file snapshots (⟲ Undo).
- `selfimprove/` — the sandbox workspace and current proposal (⇪ Self-improve).
- `pipelines/<name>/` — pipeline specs, frozen sources, AS-IS/TO-BE databases, last regression verdict.
- `blender/<job>/` — Blender lab scripts, renders, and logs.
- `neural3d/<job>/` — neural photo-to-3D meshes, turntables, and logs.
- `trendscout/` — seen-item store, latest trend report, adoption log (📡 Trends).

Protect that folder. Two cautions worth repeating:

- **Don't expose the app to the public internet without HTTPS.** `--host 0.0.0.0`
  is for your trusted local network; the login cookie isn't marked Secure over
  plain HTTP, so put it behind a TLS proxy before anything leaves localhost.
- **Email & outreach:** you send as yourself from your own account — only message
  people who expect to hear from you (anti-spam laws and POPIA apply). On a
  work/managed machine, sending through a personal SMTP setup or running web
  monitors on a timer may breach IT policy or trip endpoint monitoring; check
  before using it there.

---

## Troubleshooting

- **Frontend looks stale after an update** → hard-refresh (Ctrl+F5) or use a private
  window; the browser caches aggressively.
- **"Web access is off"** → turn it on in the Permissions/Chat toggle (or set
  `AGENT_WEB=on`).
- **A scheduled auto-pilot/watcher records "skipped"** → the relevant arm switch is
  off, or the recipient isn't on the allowlist. That's the fence doing its job;
  check the Autonomy dashboard.
- **Auto-resume isn't picking up a task** → it only touches tasks idle past your
  threshold, skips tasks blocked on you, and stops after the no-progress attempt
  cap. Use **Preview** to see what's eligible right now.
- **Replaying GUI actions / unattended sends** need an unlocked, logged-in session
  and live network — the sandboxed offline test suite can't reach a real SMTP
  server or arbitrary sites, by design.


> **Voice in the packaged .exe:** the one-file build now bundles voice
> (faster-whisper + its native backend) when it's installed in the build
> `.venv`. If a packaged `AgentJo.exe` reports voice unavailable, it was built
> without it — rebuild with `build_exe.bat`, or just run from source
> (`.venv\Scripts\python.exe run_web.py`), which uses your installed packages
> directly. The startup line and the 503 message both now say whether you're
> running frozen or from source.

> **Easiest way to run on Windows:** double-click **start_agent_jo.bat** in the app folder (or right-click it -> Send to -> Desktop for a shortcut). It launches from your `.venv`, so voice input works and you always run the latest code - no rebuilding, unlike `dist\AgentJo.exe`. It opens your browser automatically; close the console window to stop the app.

> **Local models (Ollama) not running when selected?** If picking a local model like `qwen3.6` answered with Claude instead, that engine only worked under the hybrid backend and silently fell back otherwise. Fix: open **Manage engines -> Make local models selectable**. This turns your installed Ollama models into first-class, **removable** engines that connect straight to Ollama and run under any backend. A local pick with no local model now refuses clearly (409) instead of quietly using Claude. Requires Ollama running; `ollama pull <model>` first if it's not installed.

> **Local replies still truncating?** Ollama defaults its context window to only 2048 tokens (prompt + reply), which cuts long local replies short no matter how high *Max output tokens* is. Agent Jo now sends a proper context window to Ollama; tune it in **Settings -> Behaviour -> Local context window** (default 8192). Raise it for longer local replies, but keep it within what your model actually supports. Cloud engines are unaffected.

### 21. Trend scout — the app keeps up with the AI-agent space

Ask "what's new in AI agents?" in chat, hit **Scan now** in the 📡 Trends
panel, or switch on the **weekly auto-scan** (Mondays 07:30, off by default).
The scout pulls fresh material from **trending GitHub repos**, **Hacker News**
and **new arXiv papers**, dedupes against everything already seen, and has the
engine cluster it into trends. For each trend it drafts a concrete learnable:

- a **skill** — name + step-by-step instructions the agent can follow with its
  existing tools. Click **Adopt skill** and it enters the app's real skills
  store, immediately usable; or
- a **build request** — click **Use in chat** to drop it into the composer,
  from where it goes through the human-gated ⇪ self-improve pipeline.

**Works on any engine, including small local ones.** The digest runs in
**batches** (8 items by default) rather than one giant request, checkpointed to
disk after *every* batch:

- **Context overflow self-corrects.** A batch too big for the engine's context
  is halved and retried — down to one item — so a small local model still gets
  through the whole list instead of failing.
- **Interruptions resume with precision.** If the engine dies mid-run (Ollama
  stopped, connection dropped, credit exhausted), the panel shows how far it
  got and **Resume** continues from the exact next item — no repeated work,
  nothing skipped. Partial trends stay usable meanwhile.
- **Real errors are shown, not masked.** OpenAI-compatible engines return
  failures as ordinary reply *text*; the scout surfaces the engine's own words
  ("couldn't reach the endpoint…") instead of a generic JSON complaint.

**Digest engine picker:** choose Auto/Claude/any custom engine in the panel.
Pick a local engine and scans cost nothing and need no cloud credit; the
choice sticks and the weekly job uses it too.

**Security stance:** this feature reads the open internet and proposes changes
to the agent's own behaviour, so the digest treats all fetched content as
**untrusted data** (analysed, never obeyed), **nothing is adopted without your
click**, and build requests only become code through the test-gated
self-improve pipeline. Autonomy is opt-in.

**Honest limits:** trend quality tracks the engine doing the digesting — a
small local model gives shallower clusters and looser JSON (the parser
tolerates fenced/prose-wrapped output, but stronger engines give better
trends). GitHub was live-verified during the build; Hacker News and arXiv are
wired identically but unverified from the build environment — a failing source
is noted and the others continue.

Data lives in `trendscout/` (seen-items store, latest report, progress
checkpoint, adoption log).

### 22. Crew — persistent specialist agents (Symbolic Synapse)

Sub-agents are ephemeral: one objective, then gone. A **crew member**
persists — a standing brief, its own engine, its own workspace folder, its
own memory that accumulates across runs, and optionally its own schedule.

**The default roster**, shaped for an AI/BI/data consultancy:

| Specialist | Owns |
|---|---|
| **Delivery** | BI dashboards, data pipelines, QA of client deliverables |
| **BizDev** | Lead research, qualification, proposals, outreach drafts |
| **Ops** | Project status, timesheets, invoicing prep, admin |
| **Intel** | Tenders (SA eTenders), competitors, AI/BI market trends |

**Two ways work reaches a specialist.** *Dispatch* — describe the job in the
👥 Crew panel or in chat ("get BizDev to research three JHB manufacturers")
and the dispatcher routes it by matching against each brief. *Schedule* — give
a member a cadence and it works on its own, filing a report you read later.

**Autonomy is scoped, not absolute.** A member runs with auto-approve for
writes **inside its own workspace folder only** — the permission granted per
run is exactly that path, never broader. It cannot touch the rest of your
disk unattended. Every run is logged per-member and lands in ▤ Audit as a
`crew` entry.

**Memory per specialist.** Each member's findings are stored under its own
category (`crew:bizdev`, …) and the relevant ones are fed back on its next
run — so Intel remembers which tenders it has already reported, and BizDev
remembers which prospects it has already qualified.

**Editing the crew:** briefs, engines and keywords live in
`crew/members.json`; add your own specialists via `/api/crew/member` or by
editing that file. Pin cheap local engines to grinding roles and a cloud
engine to reasoning-heavy ones.

**Honest limits:** routing is keyword-priority with the engine as tie-breaker
— it was accurate on 8/8 realistic consultancy tasks in testing, but an
ambiguous task ("look at the client data") can land in the wrong lane; name
the member explicitly when it matters. A specialist starts each run with a
fresh context (its brief + prior memories + your task), so give it enough
detail to work from — it cannot ask questions mid-run. Scheduled runs cost
tokens on whatever engine that member is pinned to.

### 23. Backup & restore — the thing you only miss once

Everything Agent Jo has learned lives in one folder on one disk: memories,
taught skills, playbooks, the hash-chained audit trail, crew briefs and their
accumulated findings, pipeline specs, schedules, settings, the document index.
There is no cloud copy — by design. **🛟 Backup** (Safety group) makes that
survivable.

- **One file.** Databases are snapshotted through SQLite's *online backup*
  API, so it's safe to run while the app is working — a plain file copy of a
  live database can capture a torn write and restore corrupt.
- **Everything, not just the databases.** Settings, engines, MCP config,
  crew, trends, pipelines and the audit trail all travel too.
- **Verified before it's trusted.** Every file carries a SHA-256 in the
  manifest; **Verify** re-checks them, and restore *refuses* a damaged
  archive rather than replacing good data with bad.
- **Restore takes a rollback point first.** Before anything is overwritten,
  the current state is archived — restoring the wrong file is never the end
  of the story.
- **Nightly, optional.** One toggle; runs 23:30, keeps the 7 most recent.

**About secrets — read once.** `secret.key` is **excluded by default**. The
app seals custom-engine API keys at rest, and that protection holds precisely
*because* the key isn't in the archive: a backup that leaks reveals nothing.
Restoring on the same machine works normally. Moving to a **new machine**
needs the key, so tick *include secret.key* for that — and treat that file as
equivalent to your credentials. Note honestly: `mcp.json` stores server
tokens in **plaintext** (not sealed), so any archive deserves a safe home.

**Honest limits:** renders, neural meshes, Time Machine snapshots and the
self-improve workspace are excluded by default (reproducible, and they'd
balloon the file) — tick *include renders & snapshots* if you want them. The
backup lands in `backups/` inside the same data folder, which is *not* off-site
protection: use **Download** and put a copy somewhere else. That's the step
only you can do.

### 24. Jobs — remote contract pipeline

Finds remote contracting roles, scores them honestly, drafts applications
grounded in your real profile, and tracks the pipeline through to offer.
**🎯 Jobs** (Automation group), or drive it from chat.

- **Profile first.** Skills, technologies, employers, achievements, years,
  rate, voice notes. Drafts may use *nothing else* — ask the agent to build
  it from your CV.
- **Honest scoring.** Every assessment carries its reasons *against* and what
  you demonstrably don't meet. A scout that likes everything is one you stop
  reading.
- **Grounded drafting + a fabrication guard.** After each draft, the app scans
  for specifics it can't source from your profile or the advert — year-claims,
  employers, certifications, and a lexicon of data/BI/AI tools (dbt, Airflow,
  Kubernetes…) that a capitalisation rule alone would miss. Anything
  unsupported is flagged for you to verify.
- **Pipeline tracking** through found → drafted → applied → responded →
  interview → offer → closed, with follow-up detection for applications that
  have gone quiet.
- **Daily scan**, opt-in: finds, scores, drafts. Never sends.

**Auto-apply — fully automated, with gates you set.** Turn it on and the
agent scores, drafts and *sends* on its own. Four gates decide what goes
without you:

| Gate | Default | Why |
|---|---|---|
| Rehearsal mode | **on** | prepares and logs, sends nothing — read a week of what it *would* have sent before trusting it |
| Minimum fit | 75 | below this it waits for you |
| Clean draft only | on | a draft with claims the profile can't source never auto-sends |
| Daily cap | 5 | bounds the blast radius of any mistake |

Anything failing a gate lands in a **held** list with the reason — "fit 40 is
below your threshold", "portal application, needs you", "draft makes 3 claims
I can't source". That *is* the "only involve me where I'm needed" part: you
see the exceptions, not the routine.

Email applications are fully automatable; **portal applications aren't** —
logins, forms and CAPTCHAs mean those are surfaced for you rather than
pretended at.

**Why the send is gated at all, rather than unconditional.** Everything up to the send is automated; the
send is yours. An autonomous sender that gets one fact wrong has
misrepresented you to a real employer, under your name, with no unsend. The
cost is asymmetric, so the human sits exactly there — the same rule as
self-improve and trend adoption.

**On AI detectors:** this deliberately doesn't try to defeat them. Detectors
catch *generic* output; a specific, grounded application in your own voice
doesn't read as machine-written because the substance is genuinely yours.
Engineering evasion of an employer's verification would also mean, at scale,
misrepresentation with your name attached — a bad trade for a contractor whose
reputation is the product.

### 25. Watchers — structured, self-healing site monitoring

Watchers already existed but only diffed whole-page *text*: they could tell you
"the page changed", not "three new tenders". Now a watcher can define **what to
extract**, and it reports items.

Give a watcher an `item_selector` (the repeating card/row) and `fields`
(each a selector, optionally an attribute) in its schedule payload:

```json
{"source": "https://www.property24.com/for-sale/gauteng/1",
 "item_selector": ".p24_tile",
 "fields": {"title": {"selector": ".p24_title"},
            "url":   {"selector": ".p24_title", "attr": "href"},
            "price": {"selector": ".p24_price"}}}
```

- **Per-item change detection.** Each item is keyed, so you get *only the new
  ones* — no re-reading yesterday's list, no false alarm when an advert
  rotates.
- **Selectors that heal themselves.** When a site renames its classes — the
  single reason a scraper has to be rebuilt — the watcher re-derives its
  selectors from the live page, *verifies the new ones actually extract
  something*, and carries on, telling you it healed. A proposal that fixes
  nothing is rejected rather than trusted.
- **Silent failure is treated as failure.** A watcher extracting zero items
  reports "needs a look", instead of the far worse "no changes today" every
  day forever.
- **Stdlib only.** The extractor uses `html.parser`, no bs4 or lxml to install
  or keep in step, matching the rest of the app's infrastructure.

The selector engine supports a deliberate subset of CSS — `tag`, `.class`,
`#id`, `[attr]`, `[attr=value]`, and descendants. Enough for real listing
pages; not a full CSS engine, because a half-right one fails in ways that are
hard to see. Only the healer's prompt sees the page, and it's sent a *tag/class
skeleton* with the text stripped — page content never leaves the machine.

**Honest limits:** no JavaScript execution — pages that render client-side
still need Playwright (which your standalone scrapers use). Cloudflare-style
challenges will still block a plain fetch. Text mode is unchanged for existing
watchers.

### 26. Health — one screen that says what actually works

Twenty-plus features, several of which fail *quietly*: a stale deploy still
serving old code, Ollama not running, a neural-3D command pointing at a python
that no longer exists, an audit chain broken by an edit, a backup that hasn't
run in three weeks. Every one of those has cost a debugging session that began
from the wrong assumption. **⏻ Health** (Safety group) checks all of it in
under a second.

Fifteen checks across five groups — **Deploy** (build stamp, interpreter),
**Engines** (cloud key, Ollama and its models, voice), **Tools** (Blender,
neural 3D, MCP servers), **Safety** (database integrity, audit hash chain,
backup age, disk space), **Automation** (overdue schedules, watchers needing
attention, recent error volume).

Each result carries three things: the **state**, what is **actually true right
now**, and the **specific next action** — "Run: ollama pull qwen3.6", "🛟 Backup
→ tick Nightly", not "check your configuration". **Copy report** puts the whole
board on the clipboard in a format built for pasting into Claude.

Three rules keep it honest: a check that breaks reports *itself* as failed
rather than taking the board down; nothing does slow work, so the board always
returns; and a check that can't determine the answer says **unknown** rather
than guessing green. A dashboard that lies is worse than no dashboard — if
something reads green here and still misbehaves, that's a bug worth reporting.

### 27. Circuit breakers — a feature that keeps failing stops trying

Straight from this app's own error log: the trend scan failed **four times in
a row**, and every attempt fetched sources and called a paid engine before
dying at the same point. Nobody was watching, so it just kept costing money to
fail identically. A breaker now sits in front of every scheduled action.

- **CLOSED** — normal; consecutive failures are counted (any success resets
  the count, because a feature that fails once a week isn't broken).
- **OPEN** — after 3 failures in a row it refuses immediately and cheaply,
  preserving the *original* error so the reason is still visible days later.
- **HALF-OPEN** — after the cooldown one trial run is allowed. Success closes
  it; failure re-opens with double the cooldown (capped at a day), so a
  persistently broken thing backs off instead of hammering.

**Refusing is never silent.** A tripped breaker shows on **⏻ Health** as a
failure, with the error that opened it, how long until it retries, and a reset
button — plus an audit entry for every open and close. A breaker that quietly
disabled a feature would be worse than the bleeding it prevents.

### 28. Cost governor — where the money goes, and a ceiling that holds

The app already priced tokens per engine, but the tally lived **in memory**: it
reset on every restart, so a monthly cap could never really mean anything. And
it grouped by engine, which tells you Claude cost $4 without telling you the
trend digest spent it while you were asleep.

- **Persistent ledger** — month → feature → engine → tokens, on disk. A
  monthly ceiling now survives restarts and genuinely blocks.
- **Spend by feature**, ranked. The only view that tells you what to move onto
  a local model.
- **Per-feature engine pinning** — grinding work on Ollama, cloud only where
  reasoning quality actually matters.
- **On ⏻ Health** as a Spend check: month-to-date, percentage of ceiling, and
  the biggest spender named.

**One bug worth recording.** The price table keys on engine *labels*
("Claude"), but calls are often recorded under the *model string*
("claude-sonnet-4-6") — which the table didn't recognise and priced at **zero**.
Real cloud spend would have registered as free: a ledger that under-reports
exactly what it exists to show. Labels are now normalised before pricing, and
an engine whose price genuinely isn't known is **flagged as unpriced** rather
than silently counted as $0, so the total is honest about being incomplete.

### 29. Engine evals — can the cheap engine actually do this job?

Feature 28 lets you pin any feature to any engine. That is only useful if you
know which features a small local model can genuinely handle — and until now
that was a guess, expensive in both directions: pin too eagerly and the trend
digest returns garbage, pin too little and you pay cloud rates to reformat
JSON.

`POST /api/evals/run` measures it against the contracts this app actually
depends on: the trend digest's strict JSON, the job scorer's reasons-against
schema, the watcher healer's CSS-selector reply, the pipeline healer's SQL
inside JSON, and a plain instruction-following check. **Every one of these
failed on a real engine at some point in this app's life**, and each failure
looked like a different bug until the cause turned out to be "this model can't
hold a JSON contract".

The output is the sentence you actually want:

> CustomQWEN handled: pipelines, trends — safe to pin those to it.
> CustomQWEN failed: chat, jobs, watchers — keep those on a stronger engine.

**⏻ Health** then closes the loop: a feature pinned to an engine that *failed*
that feature's contract is reported as a failure, and one pinned to an engine
never measured is a warning. Pinning without evidence stops being invisible.

Grading is deterministic — shape, keys, parseability, substrings. No model
grades another model: an LLM judge would add cost, latency and its own failure
modes to the very thing meant to measure failure. The trade is that this
measures *contract compliance* rather than eloquence, which happens to be
exactly what the routing decision hinges on. A full run takes minutes on a
slow local model, not seconds.

### 30. Capabilities — what you've actually used

The honest problem: features here have a habit of existing without ever being
exercised. A whole backup module sat wired to nothing. Watchers existed in a
weaker form and went unnoticed. Neural photo-to-3D was configured and never
run. Each was found by accident, months later.

**◈ Capabilities** reads the evidence the app already keeps — the audit trail,
plus the files and folders each feature creates *when it genuinely runs* — and
reports three states:

- **used** — there is proof it ran on this machine
- **ready** — configured, never exercised
- **needs setup** — it can't run yet, and the specific missing piece is named

Every entry carries a **sixty-second test**: the exact thing to type or click
to prove it works here. ⏻ Health carries the summary line and suggests the
next untried one.

Evidence is deliberately strict — an empty `blender/` folder does **not** count
as used, because a directory the app created on boot proves nothing. Audit
entries are the strongest signal, since they only appear after a real run.

A capability you have never run isn't a capability yet; it's an untested
assumption. This is the list of them.

### 31. Crew handoffs — specialists that pass work to each other

Four specialists working alone are four assistants. The value of a crew is the
**handoff**. Three chains ship ready to use:

| Chain | Flow |
|---|---|
| **opportunity** | Intel finds → BizDev qualifies honestly and drafts an approach → Delivery scopes what building it takes |
| **pursue** | BizDev qualifies one specific opportunity → Delivery scopes it |
| **review** | Delivery does the technical work → Ops turns it into status and next actions |

Pick a chain in the 👥 Crew panel instead of a single specialist, or use the
`crew_chain` tool from chat. Each step receives the original request, its own
instruction, and **what the previous specialist actually produced** — so the
final report is grounded in real prior work rather than a fresh guess.

**Two guardrails, because chained autonomy is where multi-agent systems go
wrong.** A chain is a fixed list and **a member may appear only once**, so
cycles are impossible by construction — there is no dynamic "hand off to
whoever next" that could loop two specialists into each other until a budget
stops them. And each step receives the previous report **truncated**, so step
four doesn't carry the full transcript of one, two and three and grow the
context (and the bill) quadratically.

**A failed step stops the chain.** Passing a failure downstream would have the
next specialist confidently building on nothing — the run is logged with which
step stopped it and why.

### 32. Native look — Fluent, in light and dark

The app now matches Windows 11 rather than having its own opinion. Settings →
Theme offers five choices:

| Choice | Behaviour |
|---|---|
| **System** (default) | Follows Windows, and switches **live** if you flip the OS to dark while the app is open |
| **Light** / **Dark** | Pins Fluent light or dark |
| **Instruments** / **Midnight** | The two earlier dark-only palettes, kept |

**These are Windows' own numbers, not an impression of them** — the #F3F3F3
mica base and #202020 dark base, the 0.0578-alpha strokes, the #005FB8 and
#4CC2FF accents, 32px control heights, and the 4px control / 7px flyout / 8px
window radii. `color-scheme` is declared so native scrollbars, form controls
and focus rings follow the theme too.

**Icons.** Nineteen emoji became nineteen distinct SVG line icons. Emoji
rendered differently on every platform, couldn't inherit colour, and two pairs
were *duplicates* — Capabilities and Engines both showed ◈, Documents and Audit
both showed ▤. The line icons scale cleanly and tint with the accent on hover.

**Light mode is real, not inverted.** Getting there meant converting 17 lines
of hardcoded status colours into tokens first — a light theme exposes every
place a dark background was assumed. Contrast is verified in both themes: all
text clears AA, most clears AAA, and the light-mode secondary label was nudged
from 4.46:1 to 4.9:1 to get over the line.

Responsive scaling has real breakpoints — the sidebar narrows at 1100px, the
thread goes full-width at 900px, and modals, toolbars and settings rows stack
at 620px.

> **Fixed after the Fluent theme release:** that edit deleted four whole panel
> sections from `app.js` (Jobs, Backup, Health, Capabilities). `wireEvents()`
> threw partway through, so every listener registered after that point never
> attached — modals wouldn't close, sidebar buttons did nothing, and issue
> capture was dead. `node --check` passed the whole time, because the file
> still *parsed*. The suite now runs the UI in a DOM stub (`tests/dom_smoke.js`)
> and asserts `wireEvents()` completes, that every named handler exists, and
> that every modal has a close function. Parsing is not running.

### 33. Dashboard — what needs you, not how many memories you have

The old strip counted memories, skills, chats and documents. Those numbers go
up on their own and never ask anything of you.

Meanwhile the app deliberately holds work at human gates — a self-improvement
proposal that passed its tests, trends drafted but not adopted, a job
application held back because the draft made a claim your profile couldn't
support, a tripped breaker, a backup that hasn't run in a fortnight. All of it
was invisible unless you opened the right panel and remembered to look.

The dashboard now answers two questions in order:

**Needs you** — everything blocked on a human decision, ordered by urgency
(act → review → note), each one a **button that opens the panel that resolves
it**. Nothing pending? A single calm "All clear" line rather than a wall of
zeroes.

**Running** — engine, month-to-date spend against your ceiling (red when
reached), the biggest-spending feature, and when the next scheduled job fires.

**Tiles** — eleven of them, and each carries the thing that makes its number
mean something rather than the number alone:

| Tile | What it compares against |
|---|---|
| **Tokens this month** | in / out / cached split, plus a 14-day sparkline |
| **Tokens today** | against the last seven days |
| **Spend** | against your ceiling, with a bar that turns red when reached |
| **Biggest spender** | its share of all tokens — the one to move to a local engine |
| **Requests** | engine calls this month |
| **Capabilities used** | 3/21 exercised here, as a ratio and a bar |
| Memories, Skills, Documents, Learned, Scheduled jobs | one click to their panel |

Tokens come from the cost ledger, which already recorded them per feature and
per engine and simply never showed them. Daily roll-ups were added so the
sparkline shows a real fortnight rather than a flat line, and the series fills
gaps so quiet days read as zero instead of vanishing.

**The grid fills the pane.** Column count is chosen from the width *and* the
tile count, so rows come out even rather than `auto-fit`'s 5 / 5 / 1. Whatever
still doesn't divide — and 13 tiles never divides evenly at any sensible width —
is absorbed by widening the last row's tiles to take the leftover columns, so
every row ends up completely full. Rows share one height, and bars and
sparklines sit flush at the bottom so tiles line up regardless of how much text
each carries. Recomputed on resize.

The old KPI strip is retired: it carried the same counts with none of the
context, and two rows of numbers competing for one glance is worse than one row
that means something. Every gatherer is wrapped, so
one broken feature degrades its own row rather than blanking the board, and
nothing here touches the network — it refreshes while you type, so it has to
stay cheap.

> **Fixed after the tiles release:** the dashboard renderer was spliced into
> the Teamwork toggle instead of the stats poll, so it only ever ran if you
> flipped Teamwork — otherwise the pane stayed empty and "Show dashboard"
> revealed nothing. It now refreshes wherever the stats do (boot, the 45-second
> poll, and after schedule or MCP changes), fills immediately when you un-hide
> it rather than waiting for the next poll, and no longer fights the toggle for
> control of visibility. The smoke test now *renders* the dashboard against a
> realistic payload and asserts the poll actually calls it — both of the
> dashboard bugs so far were code that existed but nothing reachable ever
> called, which no amount of syntax checking can catch.

### 34. Sharing it — a one-time install for someone else's PC

Two things stood between this app and someone else's computer: it had no way
to be given an engine except an environment variable set before launch, and a
zip of the working folder would have carried the API key, the memories, the
audit trail and the job profile built from a CV.

**First-run setup.** On a machine with no engine the app opens straight to a
screen offering **Anthropic** (paste a key — sealed with the machine's
`secret.key`, same as custom engines) or **Ollama** (free, local, nothing
leaves the computer), with a *Check again* button for once Ollama is running.
The key takes effect immediately rather than after a restart, and rejections
say what's actually wrong — a pasted space, a truncated key, another
provider's key — instead of failing later as a confusing 401.

**Building a copy to share.**

```
python tools/make_release.py     ->  dist/AgentJo-<date>.zip
```

It copies only from an **allowlist** — a denylist silently ships whatever it
forgot — then re-opens the finished archive and scans it for API keys (Anthropic,
OpenAI, GitHub, AWS, Slack) and personal-data files. **If it finds anything the
build fails and deletes the archive**, rather than warning and shipping anyway.

**On the other PC:** unzip, double-click **install.bat**. It finds Python
3.10+ (and says exactly where to get it if missing, including the *Add to PATH*
tick that catches everyone), builds a private environment, installs
dependencies, puts an **Agent Jo** shortcut on the desktop, and launches. The
included *READ ME FIRST* explains the engine choice and where their data lives.

They supply their own engine. Nothing of yours travels.

**Honest limits:** this is a Python install, not a signed `.exe` — Python 3.10+
is a prerequisite, and the installer is explicit about it rather than failing
oddly. A true single-file installer needs PyInstaller plus Inno Setup and a
Windows machine to build on; the existing `agent-jo-web.spec` is the starting
point if you want to go that way. Voice input and the 3D tools remain optional
extras the recipient installs only if they want them.

> **Why the dashboard was invisible.** It reused `aj_kpi_collapsed` — the
> localStorage key belonging to the *old* KPI strip. Anyone who had collapsed
> those vanity counters months earlier inherited "hidden" for a feature that
> didn't exist when they set that preference, and no amount of clicking the
> toggle changed the fact that it started off. Fixed four ways: it has its own
> key now (`aj_dash_hidden`, defaulting to visible), it is no longer `hidden`
> in the markup so a slow or partly-failed script can't leave it invisible, the
> endpoint can't 500 on a bad tile, and a slow fetch shows "Checking…" then an
> explicit failure line rather than a blank pane. All three startup states —
> fresh install, upgrading user with the stale preference, and an explicit
> hide — are asserted in the suite.

### 35. Persistent tiles — history, memory of your layout, instant paint

Three things the tiles were missing, all now persisted to disk:

**History for every tile.** Each measurable tile records one value per day, so
Memories, Skills, Documents, Learned, Requests, Spend and Capabilities all get
the sparkline that only Tokens had. Kept to 30 days, and hung off a stable
tile **key** rather than its label, so rewording a tile doesn't orphan its
history.

Two gap policies, on purpose: a **counter** (Memories) carries its last value
forward, because a day nobody looked at it isn't a day it fell to zero; a
**token** series keeps genuine zeros, because a day with no usage really was
zero. Getting that backwards would either invent usage or erase a collection.

**Your layout is remembered.** *Customise tiles* under the status row lets you
untick tiles and move them up or down; the choice is stored server-side, so it
follows the app rather than one browser. A tile added in a later version still
appears for someone with a saved order rather than being silently swallowed by
it.

**Instant paint.** The last board is saved and served from
`/api/dashboard?cached=1`, so a restart shows the previous numbers immediately
and replaces them when the live ones arrive — no empty pane, no flash of
"Checking…".

### 36. Local and cloud — told apart by fact, and covering for each other

**The old answer to "is this local?" was the engine's name** — a substring list
of ollama, qwen, llama, mistral. That is wrong in both directions and silently
so: a qwen model served from a hosted endpoint is cloud and costs real money
but read as free, while a local proxy someone called `claude-local` was billed
at Anthropic rates. Cost totals, routing and the engine indicator were all
built on that guess.

Classification now uses evidence, in order:

1. **the engine's endpoint** — loopback, `.local`, or a private address is
   local; a known provider host (Anthropic, OpenAI, Together, Groq,
   OpenRouter, Mistral, Fireworks, Google, xAI…) is cloud
2. **the built-ins**, whose nature isn't in doubt
3. **Ollama actually having the model** — the configured local model, or a tag
   the local server reports as installed
4. only then a name hint — and when that's all there is, the answer is
   **unknown**, not a confident wrong one

Unknown matters: an engine that can't be classified is never assumed free,
because an unpriced cloud engine reporting $0 is the worst of the three
outcomes. ⏻ Health lists any unclassified engine and asks for its URL.

**Covering for each other.** If the chosen engine fails for a reason another
engine could survive — no credit, rate limited, unreachable — the turn is
retried on the other kind instead of returning an error, and the reply says
which engine answered and why: *"Claude isn't answering (no credit), so this
ran on qwen3.6 — local and free."* Failures that would simply repeat
elsewhere, like a malformed request, are **not** retried; spending a second
engine to fail identically helps nobody.

### 37. Turbo — local drafts, cloud finishes only when it must

Most turns don't need a frontier model: summarising, extracting, reformatting,
answering from retrieved context, routine tool driving. Paying cloud rates for
all of them to cover the minority that do is how the bill gets large.

With **Settings → Turbo** on, the local model answers first. A **deterministic**
gate reads that answer, and only a failed gate escalates — and when it does,
the cloud engine receives the local draft to improve rather than a blank page.

The gate never asks a model whether the answer was good; that would spend a
call to save a call, and self-assessment isn't reliable enough to bet money on.
It checks only things that are *observably* wrong:

| Check | Catches |
|---|---|
| empty / too short | the model produced nothing usable |
| refusal language | "I cannot…", "As an AI…" |
| broken JSON when JSON was required | the failure that breaks trends, jobs and watchers |
| repetition | a small model that has lost the thread |
| stops mid-sentence | truncation |
| leftover placeholder | `[INSERT NAME]` reaching the user |

**Honest about the trade.** Escalating costs *more* than going straight to the
cloud — you pay the local pass plus a cloud pass that now carries the draft. So
the dashboard shows a **Turbo hit rate** tile, and when fewer than roughly a
third of turns finish locally it says so plainly: *"most turns are paying for
both passes — turn it off, or use a stronger local model."* The feature reports
the number that would condemn it.

Turbo needs both a local and a cloud engine, stays off for images and second
opinions, and is off by default.

### 38. Second opinion, made efficient

**How it works.** Tick it in the composer and the answer is checked by an
engine on the *other* side of the local/cloud divide — a model doesn't grade
its own work. The reviewer only critiques; if it approves, nothing else
happens. If it objects, the original engine revises once. Any failure keeps
the original, so switching it on can't make a turn worse.

That was sound but wasteful in four ways, all now fixed:

**It reviewed everything.** Every turn got a review call, including "thanks"
and bare acknowledgements. It now skips turns with nothing to check — a
pleasantry, an empty answer, an acknowledgement like "Saved." that asserts
nothing. Note what it does *not* skip: a **short** answer. The first cut
skipped anything under 180 characters, until the test suite pointed out that
"The capital of Australia is Sydney." is 35 characters and wrong. Length was
the wrong criterion; whether the answer makes a checkable claim is the right
one.

**Style notes triggered full rewrites.** "Consider a warmer tone" cost a whole
generation and risked losing correct content. A critique now has to name a
concrete problem — wrong, missing, unsupported, contradicts — before a rewrite
is paid for.

**Any revision was accepted.** A reviewer's complaint can push a model into
truncating, refusing, or dropping half a correct answer. Revisions are now
validated against the same deterministic gate Turbo uses, plus a relative
length check — and judged against the *draft*, not an absolute size, so
correcting "Sydney" to "Canberra" is still accepted.

**It kept no account of itself.** Approvals, ignored nitpicks, rejected
revisions and real improvements are all counted, and when ten reviews have
changed almost nothing it says so: *"the reviewer is mostly agreeing — you may
be paying for reassurance."*

### 39. Skills — run the ones you've adopted, and see which earn their place

Adopted skills already reached the model: every one is pasted into the system
prompt on every turn. That works, but it's entirely passive — you couldn't see
what you had, couldn't ask for one by name, and nothing recorded whether a
skill had ever been used. The `times_used` column had existed since the
beginning and was **never incremented once**.

The new **Skills** panel (Core group) gives them a front door:

- **See what each one does** — its description and its steps, parsed out of
  the instructions so you know what will happen before you run it.
- **Run one deliberately** — type what it should work on and press Run. The
  skill's steps go in front of an ordinary chat turn, so tools, permissions,
  memory and the audit trail behave exactly as they do for any other turn
  rather than a second execution path with its own gaps.
- **See which are working** — every run is counted, dated and audited. Skills
  sort most-used first, and the ones that have never run are called out. A
  skill adopted from a trend six weeks ago and never used is now visible
  instead of quietly riding along in every prompt.

The agent can also invoke them itself: `run_skill` lets it follow a saved
skill when you name one ("use my BI review skill on this"), and calling it
without a name lists what's available. Running with no input makes it ask for
the one thing it needs rather than guessing.

> **Sidebar grouping made legible.** The Fluent pass had set the group
> headings to 12px semibold body text — near-identical to the 13px buttons
> beneath them — so **Core / Automation / Extend / Safety** stopped reading as
> headings and the expanded list looked flat. A heading now differs from its
> items on four axes at once (size, case, letter-spacing, colour), each group's
> items sit indented behind a **vertical spine** so membership is geometry
> rather than memory, a hairline rule separates one group from the next, and
> Settings — which belongs to no group — is separated so it can't be mistaken
> for a member of the last one. Contrast was re-checked at the smaller heading
> size: AA in all three themes. The suite now asserts the *invariant* rather
> than the numbers — whatever the sizes become, a heading has to stay smaller
> and quieter than the items it introduces.

### 40. Challenges — what South Africa is struggling with, and where AI fits

Trend Scout watches what the AI world is building. This watches the other end:
what is actually going wrong here — municipal billing, water infrastructure,
clinic queues, load-shedding, unemployment — and asks which of those a small
data/AI team could realistically take on.

Sources are **RSS feeds you control** (SA government, BusinessTech, Daily
Maverick, Engineering News, IOL by default), parsed with the standard library.
Items are filtered for *problem* language before the engine ever sees them, so
a shopping-centre opening doesn't become an opportunity brief.

Each brief states the problem locally and specifically, who feels it, what data
already exists and who holds it, a first engagement deliverable in weeks, and —
**required** — what would make it fail. A brief that names no downside is
automatically marked low confidence and flagged unvetted, because an
opportunity list without risks is a wish list. Every brief keeps its source
links so a claim can be checked, and **Send to BizDev** hands it to the crew to
qualify honestly rather than treating it as a decision.

### 41. Guide — what every part of this app is for

Thirty-plus features, most behind a small icon. The **Guide** (Core group)
walks through all of them in five chapters — Start here, Making it yours,
Working unattended, Keeping it honest, Specialist tools — and each stop answers
the three things that actually matter, in order:

- **what it is**, in one sentence without jargon
- **why you'd care** — the situation where you'd reach for it
- **try this** — a concrete thing to type or click right now, with an
  **Open it** button that takes you straight there

The "try this" line is the point: a tour that only describes leaves you where
you started. Ordered as a narrative rather than by menu position, so it reads
as an introduction rather than an inventory — which also makes it the thing to
point someone at when you share the app.

> **Why Health did nothing.** Two routes were registered at `/api/health` — a
> liveness probe added long ago, and the health board. FastAPI answers with the
> first match, so the board never ran: the panel received the probe's
> `{status, app, configured}` payload, found no checks in it, and rendered an
> empty list. Nothing errored. The board now lives at `/api/health/board`, the
> probe keeps its path, and the suite asserts **no route is registered twice**
> and that **every sidebar button does something when clicked** — the two
> checks that would have caught this in seconds.
>
> A second bug surfaced while fixing it: the probe's function was named
> `health`, shadowing the imported `health` module for every later reference.

### 42. Challenges — nothing is lost because of when it arrived

A plain scan only sees what a feed carried that day, and only keeps what the
keyword filter liked. Both lose things: a real problem whose headline used none
of the filter's words, or one that arrived in a batch that clustered around
something else.

So every item ever fetched is kept — **including the ones the filter
rejected** — with a note of whether it was ever actually used in a brief.
**Deep scan** reconsiders that entire backlog. The panel shows how many items
have been seen but never briefed, and when the backlog is empty it says so
rather than pretending to work.

In testing, a "learners still waiting for textbooks" story that the keyword
filter threw away was recovered by the deep scan and became a brief — which is
exactly the kind of thing a keyword list is bad at and worth catching.

### 43. Presenter mode — a narrated demo that drives the real app

The Guide is something you read. This is something you *show*. Press
**Presenter** and the app runs a scripted walkthrough: each scene opens the
real panel it's describing and puts the narration in a bar along the bottom,
so the audience is looking at the working application rather than a
description of it.

- **Driven by keyboard** — space or → advances, ← goes back, Esc exits. No
  hunting for the right panel mid-sentence.
- **Speaker notes** you can toggle on, with the thing worth saying out loud at
  that moment — including which live action lands best.
- **Auto-advance** for unattended running, paced per scene.
- **Cleans up after itself** — exiting closes every panel it opened and
  unbinds its keys, so the app is exactly as you left it.

Sixteen scenes in four acts: what it is, how it learns your context, how it
works unattended, why you can trust it, and where it's going.

**Two things it deliberately doesn't do.** It never fakes data — every scene
opens a live panel with whatever is really there, because a demo with invented
numbers collapses the moment someone asks to try it. And it states the limits
out loud: the self-improvement scene says plainly that only a human applies a
change, which in a room with any engineer in it answers the first question
before it's asked.

> **Crew was broken from the UI the whole time.** Every crew call site in
> `web/server.py` — Send to BizDev, Run, and chains — passed a `console` that
> was never defined in that file, so each one raised `NameError` and returned
> a 500 the moment it was used from a panel. A scheduled crew job had the same
> shape, reading a bare `payload`. The unit tests never caught any of it
> because they call `crew.run()` directly and pass their own Console; nothing
> exercised the actual endpoint.
>
> Fixed by giving the web process one shared console, and by correcting the
> payload read. Two new guards close the class rather than the instance: the
> suite now runs **pyflakes over the server and the whole agent package and
> fails on any undefined name**, and it calls the crew endpoints **through the
> web layer** — challenge-to-crew, crew run, and a chain — so "works when
> called directly" can never again be mistaken for "works".

> **A picked engine now reaches the turn, everywhere.** Choosing an engine in
> Challenges and pressing *Send to BizDev* still billed Anthropic: the picker
> was sent to the scan and nowhere else, so qualifying a brief fell back to the
> crew member's default. The Crew panel had the same gap and no picker at all.
> Both now pass the choice through to every call that runs a turn, and the Crew
> panel has its own engine selector.
>
> Underneath was something worse. An engine name the app didn't recognise
> resolved to the **AUTO sentinel** — indistinguishable from the user actually
> choosing Auto — so a typo, a renamed engine or a stale setting quietly became
> "use the default", which is the paid cloud engine. Unrecognised names are now
> refused with a reason and a list of what's available, and the suite asserts
> that a picked engine reaches the specialist on all three routes, that blank
> still means the member's own, and that an unknown name is **refused rather
> than swapped**.

### 44. Material and motion

The app's subject is measurement — hash chains, token meters, tiered engines,
health checks — so surfaces are now treated as instrument panels rather than
boxes: a hairline highlight along the top edge where light would catch, a
barely-there gradient down the face, and depth from stacked shadow instead of
heavier borders. Modals sit on real acrylic (blur + saturation) and arrive with
a short rise rather than appearing.

**The boldness is spent in one place: the dashboard tiles.** Numbers are set in
tabular figures with tight optical tracking so a reading doesn't jitter as it
updates; the meter under a capped value reads as a gauge, filling with a
gradient; and the sparkline became a proper instrument — a gradient body so the
trend has weight at 24 pixels tall, and **a marker on the latest point** so
"where am I now" doesn't need squinting at the right-hand edge. It takes its
colour from the tile, so a warning tile's trend is amber and a failing one's is
red. Tiles stagger in, so the board assembles rather than blinking into place.

Everything else stays deliberately quiet: rows fade up as a list fills, the
engine dot breathes only while a turn is running, and selection and focus use
the theme accent instead of the browser's blue.

**Held to the same standard as any data path.** SVG ids are global, so two
tiles sharing a gradient id would silently paint one with the other's fill —
each instance now generates its own, and the suite asserts it. Degenerate
series (all-zero, a single point, negative values) are tested for NaN
coordinates, contrast was re-checked on the new card surfaces (AA or AAA
throughout), and every animation is switched off under
`prefers-reduced-motion`.

> **Fixed: the tiles jittered on every refresh.** The board repaints every 45
> seconds, and it was rebuilding its DOM each time — which replayed the
> entrance animation and re-ran the grid layout, so tiles visibly jumped even
> when not a single number had changed. The stagger I'd added for first paint
> made it worse, not better.
>
> All three regions — tiles, status chips, and the needs-you list — now update
> **in place** and are skipped entirely when their content is identical.
> Measured: five identical refreshes create **zero** DOM nodes and clear
> nothing; a changed value creates **zero** and rewrites only that value; new
> sparkline history redraws only the drawing; and a genuinely different set of
> tiles still rebuilds properly. The entrance animation is first-paint only,
> and the sparkline's space is reserved whether or not there's history to draw,
> so a tile that gains a trend later doesn't grow and shove its neighbours.

### 45. On your phone — one tap, no app store

**A native App Store / Play Store app isn't possible, and wouldn't help.**
This agent runs shell commands, reads your disk, drives Blender and talks to
Ollama. iOS forbids all of that outright, and on Android it would be pointless
because those capabilities live on your PC, not in your pocket.

What does work, on both platforms, is installing the app itself:

1. Open **Phone** in the app. It shows this machine's address on your network.
2. On the phone, open that address in **Chrome** or **Safari**, and sign in.
3. Chrome offers **Install app**; Safari is **Share → Add to Home Screen**.

You get a real icon, its own window with no browser bar, and a splash screen.
No store, no build toolchain, no Apple developer account.

**Built properly rather than bolted on:** a web manifest with maskable icons
(so Android launchers crop the padding rather than the artwork), the iOS meta
tags Safari needs since it ignores the manifest for those, `viewport-fit=cover`
plus `env(safe-area-inset-*)` so the app paints under the notch and home bar,
44px touch targets, 16px inputs so iOS doesn't zoom when you tap the composer,
and modals that rise as sheets from the bottom. The sidebar becomes a drawer.

**The service worker deliberately caches nothing live.** It holds the shell so
the app opens instantly, and sends every `/api` call to the network every
time. A cached dashboard would show yesterday's token count, a cached health
board would show green for a service that had since fallen over, and a cached
"needs you" list would hide a decision waiting on you. When the PC is
unreachable it says so plainly instead.

**Honest limits:** the PC must be switched on, awake and on the same network —
the phone is a remote control, not a copy. Anyone on your network with your
password can reach it, so don't forward the port to the internet; use
something like Tailscale if you need it away from home.

### 46. Auto-apply, end to end

Feature 24 already scored roles, drafted grounded applications, gated them and
sent them. What it could not do was **find** roles — someone had to add them —
so "apply to many without intervention" failed for a reason that had nothing
to do with applying.

**Sourcing.** Roles are now pulled from boards that publish their listings
openly for this purpose (Remotive, RemoteOK, We Work Remotely by default;
editable in `jobscout/sources.json`). Listings are filtered against your
profile *before* the expensive scoring step, deduped, and an application
address is extracted where the advert contains one.

**One unattended pass** — the daily schedule now runs find → score → draft →
send, and reports what it found, what it sent and what it held.

**Per-role records and follow-ups.** Every letter can be written to
`jobscout/applications/` to attach, paste into a portal, or keep as a record
of what actually went out. Applications that go quiet get a drafted follow-up
— short, adding one concrete thing, easy to say no to — and it goes through
the same fabrication check as the original.

**Nothing here weakened the guards.** Every send still needs a fit score above
your threshold, a draft with zero unsourceable claims, a real application
address, and room under the daily cap. Rehearsal mode is still the default.

**Deliberately not built: LinkedIn Easy Apply automation.** It breaks their
terms and gets accounts restricted, which is a poor trade when your profile is
a business asset. Portal applications generally — Workday, Greenhouse, and the
rest — are tracked and drafted but submitted by you; the address extractor
distinguishes a real application address from a `no-reply` or the job board's
own, so it never sends an application into a void.

**A word on volume.** The machinery will send as many as you allow, but a
hundred generic applications perform worse than ten specific ones, and the
fabrication guard exists because a claim your profile can't support gets found
out in the interview. The daily cap defaults to five for that reason, not a
technical one.

> **Fixed: auto-apply ignored your engine, and hid why it failed.** A real run
> found twelve genuine roles and failed all twelve with the same truncated
> line — `BadRequestError: Error code: 400 - {'type': 'error', 'error':
> {'type': 'invalid_` — which said nothing at all.
>
> Two faults. `/api/jobs/auto/run` **took no engine parameter**, so every
> scoring and drafting call went to the paid default no matter what was
> selected elsewhere; the Jobs panel had no picker either. And errors were cut
> to 80 characters, exactly where the reason begins.
>
> The Jobs panel now has its own engine selector that reaches scoring and
> drafting, unknown names are refused rather than silently swapped, and error
> messages are translated into something actionable — *"that engine has no
> credit. Pick a local engine in the Jobs panel"*, *"the API key was
> rejected"*, *"check Ollama is running"*. When every role fails the same way,
> it says so once instead of printing the same line twelve times.

### 47. Portal applications — it fills the form, you sign in

Most adverts don't give an address; they give a Greenhouse, Lever, Workable,
Ashby or Workday form. Those were tracked and drafted but left entirely to
you. Now **Apply on portal** opens the advert in a real browser and fills it:
name, contact details, location, LinkedIn, CV upload, and the cover letter
already drafted for that role.

**Why you're only asked to sign in once.** The browser uses a persistent
profile, so a login to Greenhouse is remembered and every later Greenhouse
application reuses that session. Without that, "only involve me for login"
would mean being asked on every single form.

**It hands over rather than guessing.** A login wall, a CAPTCHA, a required
question your profile can't answer, or a field it can't identify — it stops,
leaves the browser open on that page, and says what it needs. It types
nothing into a login form, never attempts a CAPTCHA, and **leaves demographic
questions blank on purpose** (gender, disability, ethnicity, criminal history,
salary history) rather than answering them for you.

**The browser is visible, not headless.** Headless would be faster and would
also mean you couldn't see what was typed into a form submitted under your
name. You can watch it and take over mid-way.

**Submission is a separate gate.** By default it fills everything and stops so
you read it once and press submit yourself. `submit=true` makes it submit
unattended — same shape as the email path, for the same reason: an application
can't be recalled.

**Needs from you:** Playwright (`pip install playwright` then
`playwright install chromium`), and four new profile fields portal forms ask
for every time — full name, phone, location, and a path to your CV file. The
panel tells you which are missing before it opens anything.

**Honest limits:** the browser opens on the computer running Agent Jo, not on
a phone connected to it. Workday often requires an account before showing the
form, so expect a login hand-over there. LinkedIn Easy Apply is still
deliberately excluded — automating it breaks their terms and gets accounts
restricted. And the fill sequence is verified against a substituted driver
here rather than a live browser, since this build environment has neither
Chromium nor a display: your first real form is its first real test.

### 48. Job search — what to look for, and where

Discovery took whatever the boards happened to list and kept anything that
loosely matched your profile. Fine for a daily trickle, useless when you want
contract BI work in a particular stack, or want rid of the agencies that
repost the same role nine times.

**Search.** Type terms into the Jobs panel and it looks across your enabled
boards *right now* and shows a preview — nothing is recorded until you press
**Track this** or **Track all**. A preview matters: forty unwanted roles are
tedious to undo, and the scoring step that follows costs money. Where a board
supports searching (Remotive), the query is pushed into its API rather than
filtered afterwards.

**Where to search.** Switch the built-in boards on or off, and add your own —
any RSS feed or a Remotive/RemoteOK-style API. Useful for SA-specific boards
the defaults don't cover.

**What to skip.** Words to exclude (checked *before* your search terms, so a
banned "agency" beats a wanted "senior"), locations to allow, remote-only, and
an option to keep only roles the email path can actually reach. All of it
applies to the unattended daily run too, and every skipped role is counted with
its reason — *"3 excluded by 'agency' · 2 no search term matched"* — so a
search returning nothing tells you why.

**One flaw the tests caught:** a Johannesburg role was being thrown out by
"remote only". *Remote only* should mean "not onsite somewhere I can't be", not
"reject my own country" — it now honours the `locations_ok` already in your
profile.

### 49. Search anywhere, and a Jobs panel you can actually read

**Three boards was not "anywhere".** Most careers pages aren't RSS and have no
API — they're a list of links on a page. It now reads those, so **any URL
works**: a company's own careers page, a niche SA board, a Greenhouse listing,
a filtered search you already set up on someone else's site. Paste it into
*Search this page*, or add it permanently as a source of kind **Any web page**.

Extraction works on structure rather than one site's markup: a link is a role
if its address or its wording looks like one and it isn't the site's
furniture. Tested against a realistic page, it found the three real roles and
skipped Home, About us, Sign in, Privacy, "All jobs" and a blog post — and it
resolves relative links against the page they came from.

It's honest when it can't help: a page whose list is built in the browser has
nothing to read, and a site that blocks automated readers says so, rather than
returning an empty list that looks like "no jobs".

**The panel is now four tabs** — it had grown into a wall of controls:

| Tab | What's there |
|---|---|
| **Find** | search your boards, or any page you paste; results preview with Track / Track all |
| **Tracked** | the roles you've kept, with score, draft, portal apply and stage |
| **Auto-apply** | the gates, the engine, and Run now |
| **Where & filters** | which boards are on, add your own, exclusions, locations |

**A bug the harness caught during the rebuild:** the restructure removed a
button whose listener was still registered, so `wireEvents()` threw and every
listener after it silently failed — the same fault that broke the whole app
once before. The DOM smoke test caught it immediately this time, which is
exactly why it exists.

### 50. Jobs as a portal — full size, no default sites

**No sites are shipped.** Which boards are worth watching is a personal
decision, and three defaults quietly framed the feature as "these, plus
whatever you add". You start empty and add what you use — any careers page,
any board, any feed, any filtered search you already rely on. Six
**suggestions** are offered (three worldwide-remote, three South African) but
nothing is added unless you pick it, and a search with no sites says so
instead of returning a silent zero.

**It's a workspace now, not a popup.** The panel fills the window (96vw ×
92vh) and every tab is a **list on the left, detail on the right** — so
choosing a role shows it beside the list instead of pushing everything down
the page.

| Tab | What you do there |
|---|---|
| **Find** | search your sites, or paste any job page; pick a result to read it, Track it, or open the advert |
| **Tracked** | filter by stage; select a role to score it, draft the letter, apply on the portal, and read the fabrication check |
| **Auto-apply** | the gates, the engine, Run now |
| **Sites & filters** | your sites, add any URL, suggestions, exclusions and locations |

Rows carry their state at a glance — *can email* / *portal*, the fit score
coloured by whether it clears your threshold, and the pipeline stage — so the
list is scannable rather than a wall of titles.

> **Fixed: sites couldn't be removed.** Deleting a board 404'd and it stayed
> in the list. The identifier was a **path parameter** — `/api/jobs/sources/
> {name}` — so any source whose name was a URL (which is what you get when a
> URL is pasted into the name box) produced a path with extra segments once
> the encoded `%2F` was decoded back to `/`, matching no route at all.
>
> The identifier now goes in the request body, and both name and URL are
> accepted, since the URL is the stable one. Toggling a site on or off had the
> same weakness and got the same fix. And the cause is now prevented as well
> as handled: a URL pasted as a name is turned into a readable label —
> `https://www.turing.com/` becomes `turing.com` — and a source can be added
> with just a URL and no name at all.

> **Fixed: the Jobs tabs did nothing.** The JavaScript was setting `hidden` on
> every pane correctly the whole time — clicking a tab really did switch the
> state. The fault was one line of CSS: `.modal-portal .tabpane { display:
> flex }` had been appended *after* `.tabpane[hidden] { display: none }`. Equal
> specificity, later rule wins, so all four panes rendered at once and the tabs
> looked broken.
>
> `[hidden]` is a statement about whether an element exists for the user, not a
> styling suggestion, so it is now enforced globally and placed last in the
> stylesheet where nothing appended later can outrank it. I checked all 24
> places that show an element with an inline `display` — every one already
> clears the attribute first, so nothing gets trapped by the `!important`.
>
> Two tests now guard it: one asserts the rule stays last in the cascade, and
> one **clicks each tab** and asserts exactly one pane is visible afterwards.

> **Fixed: six sites, four different failures.** A real run produced
> `403 Forbidden` (Careers24, Toptal, Catalant), a dropped connection (PNet),
> and `missing an 'http://' protocol` (Jobbers, Lemon.io). Each had its own
> cause:
>
> **No scheme.** Two sources were saved as bare hostnames, because that's what
> the address bar shows. Addresses are now normalised on the way in —
> `careers24.com` becomes `https://careers24.com` — and sources already saved
> wrong are **repaired automatically** when the panel opens, so an old mistake
> stops failing forever.
>
> **403s.** The fetcher announced itself as a script. It now sends the same
> headers any browser would, which is not a trick — it's what a person
> visiting the page sends — and it clears most refusals on its own.
>
> **The rest.** Sites that still refuse, and boards that build their listing in
> JavaScript (most modern ones, PNet and Careers24 included), have nothing for
> a plain fetch to read. So a refusal or an empty page now **escalates
> automatically to the real browser** — the same Playwright session used for
> portal applications — and there's a **Use browser** button to force it.
>
> **And the errors now say what to do:** *"blocks automated readers (403). Try
> 'Use browser'"*, *"dropped the connection, usually a bot check"*, *"that page
> isn't there any more (404)"* — instead of a raw exception class.

### 51. Held drafts — a handle on the guard's door

*"9 draft(s) make a claim I couldn't source from your profile"* was correct and
useless. The guard was right to hold them — a claim your profile can't support
is one you'd have to defend in an interview — but it named no claim and offered
no way forward.

The new **Held drafts** tab lists every flagged claim, grouped by the thing
claimed and ordered by how many drafts depend on it. For each one:

- **Yes — add to my profile.** Nearly always the claim is true and simply was
  never written down. Say where it belongs (a tool, a skill, something you did,
  somewhere you worked) and it's recorded — then **every held draft is
  re-checked immediately**, locally, at no engine cost. Drafts that clear can be
  sent straight away.
- **No — don't claim this.** It's remembered, and future drafts won't use it.

Confirming is not loosening the guard: it's giving the guard the evidence it
asked for. The check itself is unchanged, and nothing is sent that the profile
can't support.

Case duplicates are merged — the proper-noun check and the tool lexicon both
flag the same word, and being asked twice about *Databricks* and *databricks*
is noise. The dashboard card now points at this tab instead of just reporting
the number.

> **Fixed: dismissing a claim did nothing visible.** Saying "no, I don't claim
> this" recorded the decision and stopped there — the claim stayed in the list
> and the draft stayed held, because the draft still contains the word.
> Recording a decision isn't acting on it.
>
> Now a dismissal **removes the claim from the list** (you've answered), **flags
> every draft that still says it** as needing a rewrite with the reason
> attached, and offers a **Redraft** button for exactly those — because
> confirming something else will never unblock a draft whose problem is a
> phrase you've rejected. The drafting prompt is also told what you don't
> claim, so the next draft can't reintroduce it and ask you again. Dismissed
> claims are listed at the bottom of the tab, so a decision can be revisited.

### 52. Removing tracked roles

**Not interested** on any selected role stops tracking it — and *remembers*,
because deleting a role you've decided against is pointless if the next scan
finds it again tomorrow. Only the key is kept, never the advert, and the list
is bounded.

**Clear stage** removes everything at the stage you've filtered to — closed
roles, say. That one deliberately does **not** remember: clearing out old
entries is housekeeping, not a judgement about the role, so those can be found
again. It also refuses to run with no stage selected, so there's no single
click that wipes the whole list.

A removal can be undone, and roles can be removed in bulk. Every removal is
audited.

> **Fixed: "Applications need checking" wouldn't clear.** The dashboard counted
> drafts whose fabrication *check* had failed, but dismissing a claim doesn't
> change that check — so the card kept reporting drafts whose every claim had
> already been decided, and told you to confirm claims that were no longer
> there to confirm. The panel and the dashboard were reading different things.
>
> Both now read the same source, and the card says what's actually true:
> claims still awaiting a decision → *"confirm the true ones"*; drafts that
> still say something you dismissed → a separate **"Drafts to rewrite"** card
> pointing at the Redraft button, because confirming can never fix those. When
> everything is resolved, both disappear.
>
> Two follow-ons: the dashboard now refreshes the moment you confirm, dismiss,
> redraft or remove, instead of leaving a stale card for up to 45 seconds. And
> a draft held by a problem that names no specific term — which produced zero
> listed claims but a held draft — is now reported rather than being silently
> invisible, which was the same fault in a different disguise.

> **Fixed: "9 draft(s) held · 0 claim(s) to decide".** Two faults, one on top
> of the other.
>
> **The contradiction** was a regex bug of mine. The flag messages use curly
> quotes, and the pattern that pulls the claimed term out of them was written
> with `\u` escapes inside a *raw* string — so it matched a literal backslash
> and every term came back empty. Nine drafts were held and not one claim could
> be named, so the tab said "nothing held" while the counter said nine.
>
> **The real cause** was underneath: the profile is empty. With nothing to
> check against, every name and tool in every draft reads as unsourced, so all
> nine were held. Listing forty terms to approve one at a time would have been
> the wrong instruction — the fix is one action, not forty. The tab and the
> dashboard now say so directly: *"Your profile is empty. All 9 drafts are held
> because there's nothing to check them against. Ask in chat: build my job
> profile from my CV."*
>
> Once a profile exists, the claim list works as intended and names real terms.

### 53. Self-improvement — three things it was missing

The machinery was already right: a sandboxed copy of its own source, the whole
test suite must pass, only a human applies, every replaced file snapshotted.
What was thin was everything around it.

**It didn't know what was worth fixing.** It only built what you thought to
ask for, while the app was sitting on a detailed record of its own faults. The
**Worth fixing** tab now reads that record — errors that keep repeating (with
how many times), health checks failing right now, breakers that keep tripping,
capabilities that have never worked on this machine — and each suggestion
carries the evidence behind it and a one-click "Ask it to build this". Nothing
is invented: asking a model for improvement ideas produces plausible noise,
whereas the error log produces things that have actually gone wrong.

**Applied changes vanished.** There was no record of what had been built and
no way to undo one improvement without hunting through the Time Machine for
the right files. The **History** tab lists every applied improvement with its
files, and **Roll this back** restores exactly that set — newest-first, so a
file touched twice ends up where it was before *that* improvement. This needed
a fix underneath: `snapshot()` returned nothing, so there was no way to
identify which snapshots belonged to which change.

**The proposal was one undifferentiated blob.** It's now a full-size portal:
files on the left, that file's diff on the right, with the test result and the
state ("Tested and waiting for you", "Applied — restart to activate") in the
header.

> **Fixed: the search summary, and the bug it was hiding.** A real run
> produced eighty lines of *"1 not remote (Dunfermline, )"* — but the mess was
> the smaller half of the problem.
>
> **Region lists were being read as office addresses.** *"LATAM, Europe, USA,
> Canada, APAC"* and *"Americas, Europe, Asia, Africa, Oceania"* were rejected
> as "not remote". On a remote job board that field says **where the candidate
> may live**, so the filter was throwing away exactly the roles those boards
> exist to list. A region list is now recognised as one, and kept when it
> covers somewhere you can work — *"Americas, Europe, Asia, Africa, Oceania"*
> includes you; *"USA"* alone doesn't, and says **"region excludes you"**.
>
> **Reasons are categories now, not values.** Eighty cities produce one line:
> *"254 skipped — 251 no search term matched, 3 not remote"*, with the full
> breakdown on hover. Trailing commas are stripped at the source, so
> *"Hobart, "* is just *"Hobart"*.
>
> **Encoding fixed.** *"CanÃ³vanas"* and *"St Johnâ€™s"* came from trusting a
> wrongly-declared charset; responses are decoded as UTF-8 first.
>
> Two bugs of my own surfaced while making the region change, both caught by
> the suite: the region check first returned early, letting a worldwide role
> match **every** search term; and the location gate then re-tested a region
> as if it were a city, rejecting roles it had just approved.

> **Fixed: "chain BROKEN" on the audit trail — and it wasn't tampering.**
> The lock guarding the hash chain was a **thread** lock, but the web server,
> the scheduler, and any script that imports these modules are separate
> **processes**. Two of them would read the same tail, both claim it as their
> predecessor, and the result is byte-for-byte indistinguishable from someone
> editing the log. Reproduced with four concurrent writers: the chain broke at
> line 72.
>
> The critical section — read the tail, append, rotate — now runs under a lock
> the operating system enforces across processes (`fcntl` on POSIX, `msvcrt`
> on Windows), and the cached tail hash is re-read on every write rather than
> trusted. Verified with five concurrent processes writing 300 entries: chain
> intact, and intact again with rotation firing under that load. **Tampering is
> still caught** — the lock fixes races, not forgery.
>
> A deadlock of my own surfaced while building it: `record()` held the lock and
> then called rotation, which called `record()` again and waited forever on a
> lock that is not re-entrant. Rotation now writes its entry inline.
>
> **For a trail that is already broken:** Audit → **Reseal**. It archives the
> old file untouched as `audit-unverified-<date>.jsonl` and opens a fresh chain
> that names it, where it broke, and how many entries it held. It deliberately
> does **not** re-hash the old lines: a tool that quietly rewrites the log so it
> verifies again is precisely the attack the chain exists to detect.

### 54. Clearing the audit trail — allowed, but never silent

**Audit → Tidy up…** clears the trail. Two ways, because they're different
needs:

- **Clear older** — keep the last N days and drop what's behind it. Usually
  what's actually wanted: months of scheduler noise, not this morning's work.
  Recent entries are carried into the new chain and marked as carried over.
- **Clear everything** — start completely fresh.

**The clearing is itself recorded.** The new trail opens with an entry stating
how many entries went, the dates they covered, and where they were put. A log
that can be silently emptied isn't tamper-evident at all, and that entry is the
difference between housekeeping and a cover-up.

**Nothing is destroyed by default.** The old trail is archived as
`audit-cleared-<date>.jsonl` and stays readable. Archived trails are listed
with their sizes, and deleting them permanently is a separate, explicitly
confirmed action — which is also recorded.

Carried-over entries can't keep their original hashes without lying about
their predecessors, so they're re-chained and flagged `carried_over`, with the
untouched originals in the archive. The new chain verifies honestly rather
than pretending the old one continued.

### 55. Two failures that repeated forever

Both were sitting in a real audit trail, failing on a schedule, and neither
said anything useful.

**A retired model.** `deepseek-v4-pro` reached end of life on 7 August; the
Morning brief called it every day afterwards and got a 410. It read as a
generic error, so nothing named the cause and nothing fell back. Retirement is
now recognised as **permanent** and distinct: the engine's own model is named,
⏻ Health raises it as a failure naming the job and the model, and — since it's
exactly the kind of failure a different engine survives — it now triggers the
fallback that credit and rate-limit errors already did.

**A watcher whose site had gone.** *Tandem Create* failed hourly for two days
with `Could not resolve host`, and the only thing that noticed was the circuit
breaker — which then suppressed **every other watcher** on that schedule.
Identical failures are now counted: a permanent-looking fault (DNS, 404) pauses
the watcher after three, anything transient gets five, and the schedule is
disabled with a message saying what happened and how to re-enable it. A single
success clears the count, and a *different* error resets it — a site that
breaks in a new way each time is having a bad day, not a terminal one.

> **Fixed: category links were being tracked as vacancies.** The trail showed
> entries like `Data analysts @ —`, `Data analyst jobs @ —` and `Power BI
> specialists @ —`. Those are links to *more listings*, which the HTML
> extractor read as adverts — and each one then had an application drafted
> against it, addressed to nobody.
>
> Titles that describe a category rather than a post are now rejected: a
> trailing "jobs", "vacancies", "opportunities", a leading "all/browse/latest",
> a trailing "in <Place>", and a bare plural with no company ("Data analysts").
> Real adverts are unaffected — *Senior Data Engineer*, *Data Engineer III*,
> *Analytics Engineer (Contract)*, *Data Analyst, Reporting Partnerships* all
> pass. Anything with neither a company nor a link is refused outright, since
> there is nothing there to apply to.
>
> For the ones already tracked: **Jobs → Tracked → Remove non-jobs**.

### 56. Jobs, without the friction

The panel looked right and worked against you. Four things, all about not
losing your place:

**Actions no longer throw away what you were reading.** Every action —
score, draft, apply on portal, remove — reloaded the entire list, which reset
the detail pane. You'd click *Draft*, wait, and find the role you were looking
at gone. The selection is now kept across every refresh, restored with the
pane still open and scrolled into view.

**Work shows where you asked for it.** The row itself dims and spins while its
role is being scored or drafted, instead of a status line at the foot of a
full-height panel that you may not be looking at.

**Counts without switching tabs.** *Tracked* and *Held drafts* carry badges,
loaded when the panel opens — so you can see there are seven held drafts
without going to look.

**Keyboard.** ↑ and ↓ move through whichever list is in front of you, in Find
or in Tracked, scrolling as they go. Forty roles is a lot of clicking.

One fragility fixed on the way: a row's click handler assumed it was attached
to the page, and threw if it wasn't. Selecting now works whether or not the
row has a parent.

### 57. Tables in chat

The chat renderer handled headings, lists, code and links but had no table
support, so anything tabular arrived as rows of pipes for you to read
sideways. Markdown tables now render as tables.

- **Alignment is honoured** — `---:` right-aligns, `:---:` centres.
- **Figures line up.** A column of numbers gets tabular figures and right
  alignment automatically, because a ragged column of costs is hard to compare.
- **Optional outer pipes**, ragged rows padded rather than dropped, escaped
  `\|` preserved.
- **A table inside a code fence stays code**, and prose that merely contains a
  pipe stays prose — what makes a table is the row of dashes underneath, not
  the pipes.
- **Cell contents are escaped**, so a model quoting HTML can't inject markup.
- Wide tables scroll horizontally instead of breaking the message column.

One bug found while testing: single-column tables (`| A |`) were rejected
because the detector demanded an *internal* pipe. The divider row does the real
work of identifying a table, so that rule was both unnecessary and wrong.

### 58. The three pieces the commercial tools charge for

Sourcing, matching, cover letters, auto-apply, portal filling and tracking
were already here. Comparing against what AIApply and similar actually sell,
three things were missing — all now on the selected role in **Tracked**:

**Match** — keyword coverage against the advert, and deliberately *not* an
engine call. Applicant tracking systems filter on terms, so the honest way to
predict that score is the same comparison, run locally: it pulls the tools and
years out of the advert, checks them against your profile, and lists exactly
what isn't evidenced. Instant, free, and it can't hallucinate a match that
isn't there.

**Tailor CV** — a CV reordered and re-emphasised for that advert, written to
`jobscout/cv/` ready to attach. It may reorder, select and rephrase; it may
**not** add a skill, employer, year or qualification your profile doesn't
contain. It also reports what it **left out** — the things the advert wanted
that you can't support — because that list is what tells you whether to apply
at all. The same fabrication check the cover letters get runs over the CV,
and it is the strictest reading, since a CV is the document most likely to be
checked against you.

**Interview prep** — the questions this advert makes likely, each with the
strongest answer *available in your profile* and a named **gap** where there
isn't one. Those gaps are the questions to think about beforehand. Plus three
worth asking them.

Tested against a CV claiming Kubernetes, Snowflake, Google and "10 years of
Airflow" for a profile containing none of them: every one caught.

### 59. Finding anything — Ctrl+K

Twenty-four features behind twenty-four icons meant using one required
remembering which panel it lived in. **Ctrl+K** (⌘K) opens a palette that
searches all of them.

It searches by **what a panel is for**, not just its name — so "spending cap"
finds Settings, "cv" and "interview" find Jobs, "what is broken" finds Health,
"restore a file" finds Undo, and "tender" finds Challenges and Crew. Every word
you type has to match, a hit in the name outranks one in the description, and
↑↓ then ↵ drives it without the mouse. Common actions are in there too — run
auto-apply, verify the audit chain, back up now.

The list is built **from the sidebar itself**, so a panel added later appears
without anyone remembering to register it.

There's a visible **Search everything · Ctrl K** button above the sidebar
groups, because a shortcut nobody knows about helps nobody.

**Toasts** replace the per-panel status lines for anything that finishes
elsewhere: an action started in one panel that reported into a box in another
was effectively silent. They stack, cap at four, colour by outcome, and stay
longer when something failed.

### 60. A design review of the whole app

Rather than restyle, I reviewed what was here against the traits that mark a
generated interface — and this app had three of them.

**Tracked-out ALL-CAPS eyebrows** sat above content in twenty-two places. They
appear in generated interfaces whatever the subject, which is what makes them
chrome rather than design. Headings now carry their rank through size, weight
and colour instead of shouting.

**Meta strings joined with middle dots** — `Johannesburg · Remotive` — string
different kinds of fact together with a character that means nothing. A place
and a source are different things, so they now look different: the place reads
plainly, the source sits in a small outlined chip.

**Monospace used for words.** It was on forty-seven selectors, most of them
labels. Monospace earns its place on figures, identifiers and timestamps, where
the fixed advance stops numbers jittering as they update; on prose it is
costume. It is now reserved for data.

**What replaced them is a system.** A modular type scale with roles —
`--t-micro` through `--t-large` — one spacing scale so panels stop each
inventing their own, and a **68-character cap on prose**. That last one is a
real readability fix rather than a stylistic preference: on a 1400px window a
chat message ran the full width and the eye lost the line between rows.

**The palette stays, deliberately.** Bronze, silver and gold aren't decoration
here — they're the Medallion layers this app builds pipelines in and the engine
cost tiers it bills against. That through-line is the one bold thing; the rest
stays quiet, which is the point.

Re-checked at the new sizes: the lowest contrast anywhere is 4.9:1, so every
theme clears AA. Reduced motion, visible focus and the mobile breakpoints are
unchanged. Six assertions now hold these decisions in place, because a panel
added later could quietly reintroduce any of them.

### 61. Turbo that actually saves money

Turbo tried the local model on **every** turn and reported its saving against
a hardcoded guess. Both had to change, because together they were the one way
this feature could cost more than it saved.

**It now decides before spending anything.** A deterministic look at the
request routes it: summarising, reformatting, extracting, classifying,
translating and short factual questions go to the local model — that is what
small models are genuinely good at. Code, reasoning, comparisons, anything
touching an application or a client, tool use, long pastes and attachments go
straight to the strong engine. Trying those locally burned a pass and then
paid cloud rates anyway.

The routing is a keyword judgement, so ordering matters and it was worth
getting right: *"extract the email addresses"* is extraction, not sending
email, and *"summarise in three bullets"* is summarising, not formatting.

**It learns which categories are worth attempting.** Each category keeps its
own record. After six attempts, anything finishing locally less than a third
of the time stops being tried at all, and says so: *"only 1 of 7 'classify'
turns finished locally, so this now goes straight to the cloud."*

**The saving is measured, not asserted.** It reads your actual chat spend for
the month and divides by the calls that produced it — during testing that gave
$0.045 a turn from real ledger data. Until there are enough cloud turns to
measure, it says *"estimated — not enough cloud turns recorded yet"* rather
than showing a confident number built on a constant.

The dashboard tile now reads *"70% · 14 of 20 attempted turns answered locally
· saved $0.59"*, and "attempted" is doing real work in that sentence: turns
routed straight to the cloud never appear, so the percentage means what it
looks like it means.

### 62. Watchdog — noticing what has quietly stopped

Every fault this app has had in real use was found the same way: the person
using it noticed something looked wrong and reported it. The dashboard was
never wired to its poll. The health board was shadowed by another route and
rendered nothing. Crew raised `NameError` on every call for weeks. A scheduled
brief called a model retired on 7 August and failed every morning after. A
watcher whose host stopped resolving failed hourly for two days.

None of those were subtle in hindsight. What they share is that the app had no
opinion about its own **silence**. Health answers *"is this configured and
reachable right now"*. Capabilities answers *"has this ever been used here"*.
Neither asks the question that would have caught every one of them:

> This is switched on, it was working, and it has produced nothing for days.

The watchdog reads the audit trail backwards — the trail already records every
kind of real work — and compares each enabled feature's last success against
how often it should be producing something. It appears on the dashboard as
something needing you, and on ⏻ Health as **Still producing**.

Two distinctions keep it from becoming noise, because a panel that cries wolf
costs more than the fault it was meant to catch:

- **Never used is not broken.** A feature you've never switched on has no
  expected cadence; that's Capabilities' job.
- **Failing loudly is not silence.** Something erroring hourly is already on
  Health and in the breakers. Errors are explicitly *not* counted as activity —
  counting them would hide exactly the case this exists to find.

Thresholds are deliberately generous: four days for job scout, a fortnight for
the weekly scans, three days for watchers.

> **From a real health report: two fixes.**
>
> **"Chain breaks at line 1101 — something edited or truncated audit.jsonl."**
> That advice was a false accusation. Two of your own processes writing at
> once produces a break that looks identical to an edit, and sending someone
> hunting for an intruder when the answer is "press Reseal" is worse than
> saying nothing. `verify()` now diagnoses the cause: **two entries claiming
> the same predecessor** is a race — nothing altered, entries intact, safe to
> reseal, reported as a **warning**. A line that matches no earlier entry at
> all is a genuine edit and stays a **failure**. A line that isn't valid JSON
> is a truncated write. Each gets its own advice.
>
> **"Blender: not found"** on a machine with Blender installed. A Microsoft
> Store install lives under `WindowsApps`, which is permission-locked and so
> invisible to a plain glob — but Windows puts a readable execution alias in
> the user's own folder. It now looks there.

### 63. Jobs that runs itself, and shows you where it stopped

Your own numbers were the brief: **70 drafts held, 0 sent**, with no way to see
which step was blocking. Every piece existed; none of them joined up.

**The pipeline is now visible.** A row across the top of the panel — found,
scored, drafted, held back, applied, replied — with the blocked stage
highlighted and named: *"drafts are held because they claim things your profile
can't support — Held drafts shows which."* You see the stall instead of
inferring it from two numbers.

**Free screening before paid scoring.** Every found role used to get an engine
call to score it, including ones a keyword check rejects instantly. The local
ATS comparison now runs first and closes anything under 25% overlap, with the
reason recorded. On a scan finding forty roles that's the difference between
forty paid calls and a handful. Adverts too vague to screen on are passed
through rather than judged unfairly.

**The same vacancy on three boards is one vacancy.** Titles are normalised —
"Senior Data Engineer (Remote)", "Data Engineer" and "Data Engineer III" at the
same company collapse to one entry, keeping the copy that has an application
address, since that's the one the email path can use. Three applications to the
same employer is how you get remembered for the wrong reason.

**It knows what to search for.** With no query set, an unattended run uses the
target roles already in your profile rather than making you retype them. An
explicitly empty search box still means "show me everything" — those are
different intentions and now behave differently.

**Run this daily** sets the whole loop going in one action: find, de-duplicate,
screen, score, draft, apply. Rehearsal stays on, so the first days produce
drafts to read rather than sent mail.

### 64. Finding job sites on its own — and proving they work

**Sites & filters → Find sites for me.** It reads what your profile says you
do, works out where you can work, matches boards against both, then **fetches
each one and checks it actually returns roles before adding it**.

That last step is the whole point. A board that 403s, or builds its list in
JavaScript, sits in your sources failing quietly for weeks. Everything added
here returned real vacancies during the check, and everything rejected says
why: *"blocks automated readers (403)"*, *"nothing job-shaped came back — the
list is probably built in the browser after loading"*.

**On "which I qualify for" — an honest answer.** This cannot check your right
to work anywhere, and would be lying if it implied otherwise. It matches on the
two things that *are* knowable: boards serving a region you have said you can
work in, and remote-worldwide boards, where the employer's constraint is
usually a timezone rather than a passport. A US-only board is excluded not
because the app knows your immigration status, but because you told it where
you can work. It says so in the result rather than leaving you to assume more.

The catalogue is deliberately small and checkable — worldwide-remote boards,
South African IT boards, and a few European remote ones — because a long list
of sources that don't parse is worse than a short list that does. It keeps
trying down the ranked list until it has enough working boards, rather than
giving up at a fixed number and leaving a good one untested.

### 65. The Jobs list, as something you can actually work

**The tabs were underlined text**, which reads as a row of links rather than
as a place you are. They're now a **segmented control** — the whole set as one
object with your position inside it — each with an icon, and counts still on
Tracked and Held.

**Rows are scannable.** Each carries a **company monogram** with a colour
derived from the name, so the same employer looks the same every time and your
eye finds it without reading. Title, company, location, source and state now
sit in a proper grid instead of a run of text.

**And the things a forty-role list actually needs:**

- **Sort** by best fit, newest, company or stage.
- **Quick filters** — *Can email*, *Fit 75+*, *Not scored*. Those are the three
  questions you ask of a long list: what can it send for me, what's worth my
  time, what haven't I looked at.
- **Bulk actions.** Tick several and score them in one go, or mark them all
  *Not interested*. Ticking deliberately doesn't change your selection — you
  can keep reading one role while picking others.

Scoring in bulk keeps going if one role fails, rather than abandoning the rest,
and each row shows its own progress while it works.

> **Fixed: "drafts show 15 but nothing on the Held tab".** A design flaw of
> mine — the tab listed **claims**, and never the drafts themselves. A draft
> held for a reason that named no specific claim showed as an empty tab beside
> a non-zero count, and there was no way to read what was actually blocked.
>
> The Held tab now lists **the held drafts**. Selecting one shows why it's
> held, the subject, and the full draft text, with **Redraft this** beside it.
> A draft whose check failed without naming anything says so plainly rather
> than vanishing. Claims to decide are still there, as a second section
> underneath.
>
> And the numbers reconcile: when nothing is held it now says *"No draft is
> held. 13 are drafted and ready — you'll find them in Tracked"*, rather than
> an empty panel that contradicts the count on the tab.

> **Fixed: fifteen "400 Bad Request" lines while scoring.** Three faults, and
> the worst was silent.
>
> **Bulk scoring counted failures as successes.** A `fetch` that comes back
> 502 does not throw, and the loop only caught exceptions — so it reported
> "Scored 15 of 15" while all fifteen had failed. It now checks the response,
> collects the reasons, and when they're all the same says so once: *"None
> scored — the API key was rejected."* Fifteen identical failures are one
> problem, not fifteen.
>
> **400 was the wrong status.** The request was perfectly well formed; the
> engine refused it. Engine failures are now **502**, a missing role is **404**,
> and an empty profile is **409** — the status is the first thing anyone reads
> in a log, and all three were saying the same wrong thing.
>
> **The provider's wording reached nobody.** `invalid x-api-key` is now *"the
> API key was rejected — check it in Settings"*, and a billing error is *"no
> credit — switch to a local engine and it costs nothing"*.

### 66. Adverts that have closed

A vacancy is perishable, and nothing here noticed. A role found in July looked
exactly like one found this morning, and an application could be drafted for
something that shut weeks ago.

**Check for expired** reads the oldest adverts and closes the ones that say
they're shut — *"the page says 'this job has closed'"*, or a 404. Each closure
records **when** and **why**, and the row carries an **expired** tag with the
date. Closed roles keep a `closed <date>` tag so a decision three weeks old is
still legible.

**The distinction that matters:** a page *saying* it is closed is evidence; a
page that will not load is not. Boards block automated readers constantly, and
treating a 403 as "expired" would quietly delete live roles — a worse fault
than leaving a dead one on the list. So there are three answers, not two:
**closed**, **open**, and **unknown**, and unknown never closes anything. The
result says so: *"A page that wouldn't load is left alone."*

**Old is a label, not a verdict.** An advert listed 45 days that you never
acted on gets a *listed 62d* tag rather than being closed on suspicion. One
you already applied to is never called stale — you're waiting on them, not the
other way round.

A **Still open** filter hides the lot, and the daily run sweeps a few each
time, so the list stays current without you doing anything.

### 67. Job alerts by email — the way in that boards support

PNet, Careers24, CareerJunction and the rest block automated readers, and they
are entitled to. **All of them will email you the same listings** if you save a
search as an alert. That is the route they support, and it beats scraping on
every axis that matters:

- **It doesn't break.** No headers to tune, no browser to keep ahead of a
  detection vendor, nothing that works today and fails silently next month.
- **It's what the board wants.** You asked for these; they sent them.
- **It's filtered at source**, by matching that sees fields the page only
  renders.
- **It reaches adverts a reader can't**, including those behind a search
  interface.

Alert emails are parsed into roles — unsubscribe links, social buttons and
"View all jobs" navigation ignored, duplicates collapsed, the employer picked
up where the layout allows. Unknown senders still work, since the parser goes
by shape rather than by a per-board template that would need maintaining.
Everything downstream — screening, scoring, drafting, the fabrication check —
is unchanged and doesn't care where a role came from.

**Also: requests to one host are paced**, 2.5 seconds apart. That isn't
evasion; it's the difference between a reader and a load generator. Most
"blocks" are rate limits, so pacing genuinely reduces them, and a site asking
you to slow down has said something worth honouring.

> **On stealth scraping specifically.** I didn't build fingerprint spoofing or
> behavioural mimicry. Circumventing a site's access controls is a different
> thing from reading a page, the tooling is general-purpose whatever it's
> pointed at, and it would sit inside an agent that also submits applications
> on your behalf. It also wouldn't work reliably — the major detection vendors
> already recognise the common stealth toolkits, so it would fail
> intermittently and silently, which is the worst way for anything here to
> fail.

> **Finishing what the last change started.** The alert parser had endpoints
> and nothing feeding it — an API with no way in, which is the same
> "pieces that don't join up" fault this app keeps being caught by. Three
> routes now, in order of how little they ask:
>
> **Paste one.** A box in Sites & filters. Works this minute, no setup, no
> credentials. It takes a full message with headers, an HTML fragment copied
> out of a mail client, or plain text — being fussy about format would defeat
> the point of a paste box. It lists what it found before tracking it.
>
> **Drop .eml files in the alerts folder.** Outlook and Thunderbird both save
> that way, and a mail rule can do it for you. Processed files are **moved
> aside, not deleted** — if a parse comes out wrong you still have the
> original to show me.
>
> **How do I set these up?** lists the exact steps per board.

### 68. Open-weight models — what fits, and building your own

**Engines → Open-weight models.** Three jobs that usually get muddled, kept
apart.

**What will actually run.** Weights are parameters × bits-per-weight; the KV
cache grows with context and is what kills a model that loaded fine but dies
on a long document. Crucially it subtracts what the **display already holds** —
about 1.5 GB on a Windows desktop. Without that it called a 34B "comfortable"
on a 24 GB card with 2.7 GB spare, which is how you get an out-of-memory error
after a 20 GB download. On a 3090: 14B comfortable, 32B **tight**, 70B no.

**Licences are stated**, because they differ more than people assume.
Apache-2.0 (Qwen, Mistral) and MIT (DeepSeek-R1, Phi-4) are not the same offer
as Llama's community licence or Gemma's terms — and it matters if anything you
build gets used commercially.

**Building your own engine — two routes, and they aren't equivalent.**

A **Modelfile variant** takes an existing model and fixes its system prompt,
temperature and context under a new name. No training, no GPU time, ready in
seconds, and it becomes a real engine you can pin to a feature. For most of
what people mean by "my own model", this is it — and it's one form in this
panel.

A **LoRA fine-tune** changes the weights. The panel gives you a plan sized to
your card (8B on 24 GB works; 70B doesn't), a rough hour count, the dataset
format, and a runnable QLoRA script. It is presented as a plan rather than a
button for two reasons: it's hours of GPU time on a dataset you have to build,
and **this app cannot verify any of it from here** — a claim that it works
would be worth nothing.

It also says the thing people most need to hear before starting:
**fine-tuning teaches tone and format far more reliably than it teaches
facts.** If you want a model that knows your documents, retrieval beats
training — it costs nothing, and you can correct it.

### 69. Symbolic Synapse

The sidebar footer read **"backend: anthropic"** — a supplier's name where the
product's own belongs. If this goes to a client, the model provider is an
implementation detail, not the masthead.

It now carries a **Symbolic Synapse** mark: a synapse — two cells and the gap
that does the work — drawn in `currentColor` so it takes the accent of
whichever theme is on, with the name and tagline beside it. Clicking it opens
your site if `BRAND_URL` is set. All three are settings (`BRAND_NAME`,
`BRAND_TAGLINE`, `BRAND_URL`), so a different client engagement can carry a
different name without touching the code. Health and Issues reports are headed
with it too.

**The engine stays visible**, and that was deliberate: the footer now reads
*"engine: claude-sonnet-4-6"*. When something fails, the first question is
always which engine was serving it — the image-attach billing surprise went
unnoticed for weeks precisely because the engine in use wasn't shown. The
reports keep both `engine` and `provider` for the same reason. Branding the
product is right; obscuring what it ran on would cost you the next diagnosis.

> While doing this a test failed on `len(settings) == 27` — a hardcoded count
> that breaks whenever a setting is added, teaching you to edit the number
> rather than read the test. Replaced with the invariant it was reaching for:
> the endpoint exposes exactly what config declares saveable.

### 70. Auto-apply, showing the consequence rather than the rules

The tab had gates and a Run button — which tells you the rules but not what
they'd do. Deciding whether to turn rehearsal off is a decision about **this
list**, not about settings in the abstract. So it works it out first, locally,
spending nothing:

> *"The next run would send 2 (rehearsal, so nothing leaves), score 1, hold 1
> back, skip 1 with no address, ignore 1 that doesn't match."*

Underneath, every role sorted into what would happen to it — **would send**
(with the address and fit), **over today's limit**, **needs scoring first**,
**held back** and why, **no application address**, and **ignored, no engine
call spent**. Beside it: the last unattended runs and what they did, anything
waiting on a reply, and a plain statement of what it will never do — send a
draft the profile can't support, apply twice, exceed the cap, or submit a
portal form.

The preview is entirely free. Screening is keyword work, the gates are
arithmetic, and where an engine call would be needed it says *"it would score
this one"* rather than guessing the result.

### 71. Removing a bug class instead of fixing it again

The same fault appeared three times in this panel:

- the Held tab reported 9 while the list showed nothing
- the dashboard said "applications need checking" for claims already decided
- the tab said 15 held beside an empty panel

Each time a **count** was computed one way and a **list** another, and each fix
corrected one instance of something that could recur anywhere. By this point
four callers classified roles independently — the pipeline, the Held tab, the
auto-apply preview and the tracked list — which is four chances to disagree.

There is now **one** function that answers "what state is this role in", and
all four read it. Every role lands in exactly one of eleven buckets, each
carrying the reason in the same words the UI shows you. A disagreement between
the count and the list stops being a bug that can be written.

The test that matters asserts the four views agree on the same ten roles —
including the exact pairing that broke three times. It would have caught all
of them.

> It also caught something in my own test fixtures: they attached a draft to a
> role still marked "found", a state the app never actually produces, because
> drafting always sets the stage. A fixture that can't occur in practice is a
> test asserting something about nothing.

> **Fixed: the header lockup had changed.** The Symbolic Synapse mark added at
> the foot of the sidebar reused the class names `.brand-mark` and
> `.brand-text` — **which the app's own header already used**. Being later in
> the stylesheet, those rules won, and quietly reshaped the 38px avatar and
> "Agent Jo / agentic workspace" at the top. Reusing a class name is how a
> change to one thing lands on another.
>
> The footer mark is gone: the app already has a lockup, and a second one
> competed with it. Three assertions now hold the header — that it exists,
> that nothing else claims its class names, and that its sub-label keeps its
> uppercase treatment.
>
> The brand still heads Health and Issues reports, where it identifies whose
> tool produced them, and `BRAND_NAME` remains a setting.

### 72. An archive, and "I applied"

**Archive closed** moves closed and expired roles out of the working list.
They aren't deleted — deleting loses the record, and you want to know you
already looked at a company when they advertise again. Everything comes with
them: the draft, the score, why it closed. The **Archive** chip shows them,
each with the reason and date, and **Put it back** returns one to the list as
if newly found.

It can also sweep applications you never heard back about — *"no reply after
30 days"* — which is the other thing that silts up a list.

**"I applied"** records an application you made yourself. The pipeline assumed
it did the sending, but most applications are still made by a person: a portal
form, an email you wrote, a referral. Without this the role sat looking
untouched — no follow-up clock, and a real chance of applying twice to the
same employer.

It notes **how** (by email or on their portal) and **when**, starts the
follow-up clock, counts against the day's cap, and refuses a second attempt:
*"'Senior Data Engineer' is already marked applied — nothing sent twice."*

The list now carries each role's bucket from the same classifier the counts
use, so the button only appears where applying is actually possible.

### 73. Auto that respects your choice, rules you can read, and intercepts

**Auto reached for Claude first regardless.** The fallback chain was built in a
fixed "quality order" with Anthropic at its head, so choosing another cloud
engine changed the label in the top bar and very little else. **Your chosen
engine now leads the chain.** Claude is one option in a list rather than the
list.

**Rules-based routing.** The task decides the tier, the tier decides the
engine: summarising, extracting, reformatting, classifying and translating go
**local**; code, planning, analysis and comparison go to **reasoning**; client
work, tool use, attachments and long context go to **cloud** — where *cloud*
means whichever engine **you** chose, and *reasoning* means the strongest thing
you have configured. Rules are ordinary data: readable, editable, individually
switch-offable, resettable, and every decision records which rule fired and
where the work went.

> **Routing currently advises rather than overrides, deliberately.** My first
> version had it reassign the engine directly, which silently disabled Turbo
> further down the same function — two mechanisms fighting over one variable is
> worse than either alone. The chain fix addresses the actual complaint; the
> rules make the reasoning visible. Wiring them to override needs the two
> paths merged properly, not layered.

**Human-in-the-loop intercepts.** Under full access and unattended runs, calls
are sorted by what they would change. **Reads and local work run** — prompting
on those teaches you to click through without reading, which is worse than not
asking. **Anything reaching outside** — mail, form submissions, API posts,
deletions, shell commands — is **held with its exact inputs**, and waits.

The inputs are the point. "send_email" tells you nothing; *"to
hiring@acme.com · 'Application — Senior Data Engineer' · 1,400 characters"*
tells you whether it's right. A read-only shell command (`dir`, `git status`)
runs; `rm -rf` waits. Decisions stick, nothing is decided twice, and the
record survives the decision.

### 74. Routing and Turbo, merged into one decision

Routing had to stay advisory because it and Turbo were asking the **same
question** — "can a small model do this?" — with two classifiers, in two
places, over the same variable. Whichever wrote the engine last silently
disabled the other.

There is now **one plan**. It returns the engine, the tier, and whether this
is a local attempt that may escalate. **Turbo becomes what it always was
underneath: the escalation policy for work routed local**, not a second
router.

- Light work → the local engine, escalating to your chosen cloud engine only
  if the gate finds the answer unusable.
- Turbo off → still routed local, but no escalation.
- Code, planning, analysis → the strongest engine, and it never escalates
  because it never went local.
- Attachments override a local rule; no local engine means your chosen one.
- **Turbo's learning keeps its veto.** A category that has repeatedly failed
  locally stops being routed there — that's a fact about your machine, and it
  outranks a rule.

**It stays out of the way where it would change nothing.** Counting engines was
the wrong test: two cloud engines with no local model and no stronger sibling
means every tier resolves to the same place, so routing would change the
plumbing and nothing else. It now asks whether the tiers actually differ.

> Two of my own bugs on the way, both caught by the suite. The first override
> attempt assigned the enclosing function's parameter inside a nested
> generator, which makes Python treat it as local for the whole generator —
> so the read *above* the assignment raised `UnboundLocalError` and every
> chat turn failed. The routed token now travels in its own variable. The
> second was the engine-count guard, which engaged routing in a setup where
> every tier resolved to the same engine.

### 75. Engines: no built-ins you can't touch, tagged by what they are

**The DeepSeek Pro and Flash entries are gone.** They were built in when they
were the only alternative worth wiring by hand. They aren't special any more —
one of them was retired by the provider in August and kept failing every
morning — and an engine you can't edit or remove is worse than one you added
yourself. Add them like anything else if you want them.

**You can edit an engine that already exists.** Correcting a typo in a base
URL used to mean deleting it and adding it again, which lost the key and any
feature pinned to it. Now: change the model, the URL, the prices, or **rename**
it, and if the renamed engine was your default the setting follows it.

Leaving the key blank keeps the stored one. That matters more than it sounds —
otherwise adjusting a price would silently unauthenticate the engine, and the
next failure would look like a provider outage. The form never receives the key
back, only a hint that one is set.

**Custom engines are tagged cloud or local**, by their URL rather than their
name. `kind` used to say `"custom"`, which described where an engine came from
rather than what it is — so a local model was never recognised as free, and
routing had no way to tell whether a call left the machine. `localhost` and
private LAN addresses are local; anything else is cloud, and the panel shows
**local · free** or **cloud** on each row.

> Whether *you* added an engine is now a separate flag from what it is. The
> two were conflated, and a test caught the consequence immediately: the
> engine list filtered on `kind === "custom"`, so retagging would have emptied
> the panel.

> **Fixed: an attached JPEG the agent couldn't see.** The image was built,
> resized and encoded correctly — and then **silently dropped**. The
> OpenAI-compatible message converter collected only `text` blocks from a user
> message, so on any engine except Claude the photo never left the browser. No
> error, no warning; the model simply answered as though nothing was attached.
>
> Images are now converted to the shape those APIs expect — a `data:` URL in
> an `image_url` part — and an image sent without a caption gets one, since
> some providers reject a vision message carrying no text at all.

### 76. More file types, read honestly

Alongside PDF, Word, Excel and PowerPoint: **OpenDocument** (.odt/.ods/.odp),
**HTML** (with the navigation and scripts stripped), **RTF**, **saved emails**
(.eml/.msg, headers kept — usually the point), **EPUB**, and **archives**.

Two decisions worth stating. A **.zip is listed, not unpacked** — extracting an
archive someone sent you is a decision, not something to do silently on upload.
And an **image attached as a document** is OCR'd only if Tesseract is present,
labelled *"OCR — check it"*, because OCR on a photograph is unreliable and
presenting a guess as the file's contents is worse than saying so.

RTF reads even without the optional `striprtf` library, using a plainer
fallback — refusing a file because a nice-to-have dependency is missing helps
nobody. And an unreadable type now names what *does* work rather than just
saying "unsupported".

### 77. Jobs: fewer bands, one search, the score where the eye goes

Counting what was actually on screen explained why it felt cluttered: on
**Tracked**, five horizontal control strips sat above the content — pipeline,
tabs, a toolbar, a chip row, and the bulk bar. The fix for "not sleek" was
removing rows, not adding decoration.

**One search box instead of two.** There were separate inputs for "search my
sites" and "paste a job page", which asked you to know which kind of thing you
had before typing it. Now one box notices: words search your boards, a URL
reads that page, and the button relabels itself — *Search* or *Read this page*
— with **Use browser** appearing only when a URL is present. `careers24.com/jobs/it`
counts as a URL; `power bi` doesn't.

**The fit score has its own column.** It was a pill among five others, when
it's the single thing you scan a list by. It's now a right-aligned figure in
tabular numerals, green above 75 and greyed below 50, so a strong match is
visible without reading anything.

The control rows are tightened onto the shared spacing scale, the search bar
takes a proper focus ring, and secondary actions in the detail pane recede so
the primary one reads as the thing to press.

### 78. Challenges: AI and tech, and two honest answers

The scan read general South African news, so it surfaced real problems this
app is in no position to do anything about. It now reads where AI and software
problems actually get written up: **arXiv cs.AI and cs.SE**, Hacker News, Ars
Technica, MIT Technology Review, The Register, and locally ITWeb and
MyBroadband. Research first, because a limitation named in a paper is usually
a problem eighteen months before anyone builds around it.

The filter changed with it. Words like "crisis" and "backlog" pulled in
politics and load-shedding; it now looks for what goes wrong in this field —
hallucination, prompt injection, context limits, inference cost, evaluation,
drift, POPIA and provenance, technical debt.

**"What would fix this?" gives two answers, and says which is which.**

- **App-level** — the model can already do the work and what's missing is
  scaffolding: a check on its output, a retry, a store, a human gate. Agent Jo
  lists what it would build, **which of its existing capabilities it would
  use**, what's genuinely new, an effort estimate, and why it might fail.
  **Send to self-improvement** turns that into a build request — a *request*:
  it still runs the whole suite and stops for you, because a challenge scraped
  off a feed should not start writing code on its own.
- **Model-level** — no scaffolding fixes a context limit, a missing modality,
  or reasoning that comes out wrong. An app can *detect* those, not prevent
  them. **Write it up as a feature note** produces something worth sending: the
  observed limitation, why it matters, a proposal, and **how to verify a fix** —
  because "make the model better" helps nobody.

Getting that distinction right is the point. Claiming an app can fix a model
limitation is how a month disappears. And a proposal that arrives without a
stated failure mode gets one, since an opportunity list with no risks is a wish
list.

### 79. Finding MCP servers — and being careful about wiring them

**MCP → Find servers** searches the npm registry live. Not a bundled list that
goes stale: the actual current set, with real versions and publish dates, so
"latest" means latest. A test run returned 127 servers, official ones first.

**Discovery and wiring are kept apart, deliberately.** Adding an MCP server
isn't like adding a bookmark — it grants a program the ability to act on your
behalf, usually with a token you supply. So each result states plainly **what
it would be able to reach**: *"read and change your repositories"*, *"read and
send messages as you"*, *"see and move money"*. And **Add** writes a
**disabled** entry with its required environment variables listed and blank.
Even after you accept a server, nothing runs until you enable it and supply
its credentials.

That gap is the point. A registry search returning a package called
`mcp-server-github` proves nothing about who published it, and an agent that
installs *and enables* tools it found on the internet is a bad idea however
convenient.

Two smaller judgements: **libraries are not servers** — the official scope also
contains the SDK, the core library and the inspector, and offering
`@modelcontextprotocol/sdk` as something to install would be a confident wrong
answer. And anything unpublished for over a year is hidden unless you ask,
because unmaintained is a fact worth acting on.

### 80. The guide had fallen behind the app

Documentation decays silently: nothing breaks, it just quietly stops
describing the thing in front of you. Measuring it found **six panels the
guide had never heard of** — including **Issues**, which is what you open when
something is wrong.

They're written now, but the fix is the check rather than the six stops: the
suite fails if a panel has no stop, or if a stop points at a panel that no
longer exists. A panel shipped unexplained is a panel nobody finds; a stop
pointing at a button that's gone sends people looking for it.

**The demo was missing the largest feature.** The presenter never showed Jobs
at all, nor the human gates — which is the thing that makes the whole idea
defensible. Three scenes added: the job pipeline running unattended, the
fabrication check refusing to send what the profile can't support, and the
consequence-sorted gates. Five minutes, still.

There's deliberately **no coverage rule for the presenter**. A demo selects
where a guide covers — walking an audience through twenty-four panels is a
tour, not a demonstration. What is enforced is that every panel it opens
exists, because sending a presenter to a button that isn't there happens in
front of people.

> **Fixed: tracked roles didn't fit their row.** When I gave the fit score its
> own column, rows already had their column count set **inline** — written
> when the selection checkbox was added. An inline style beats a class rule,
> so a row with both a checkbox and a score had **four children in a
> three-column grid**, and the fourth wrapped onto a second line.
>
> The grid is now set entirely in CSS, with all four combinations spelled out:
> plain, with-checkbox, with-score, and both. The test asserts the invariant
> that actually broke — **every row's child count matches the columns its
> classes ask for** — rather than checking that some rule exists.
>
> Two of my own changes were each fine alone and wrong together, which is the
> argument against setting layout inline: a later addition has no way to know
> what an earlier one hardcoded.

> **Fixed: Auto called a wrong engine alias.** The fallback chain is built
> from **model tokens**, and I had been adding the chosen engine's display
> **name** straight into it — so Auto tried to call a model literally named
> after the engine. Names are now resolved to tokens, and a name that can't be
> resolved is **left out rather than guessed**, since guessing is what
> produced the bad alias.
>
> Fixing that exposed the other half of the original complaint. When there was
> no router pick, Claude was added at the top of the chain *before* the line
> that put your engine first ever ran — so the earlier fix was being
> overtaken by a branch above it. Your default now leads in both branches,
> with Claude still behind it as a fallback.
>
> **And the rows were three lines tall.** Title, then location and source,
> then tags on a line of their own. Tags are secondary information like the
> rest, so they sit on the secondary line; with tighter padding that's two
> lines instead of three, and roughly a third less height per role.

> **Fixed: "404 — model: DeepSeekReplika".** An engine name that no longer
> resolved was **falling through to the Anthropic client as a model id**. The
> dispatcher tries DeepSeek, then custom engines by name, and if none match it
> passes the token on to Claude — so a renamed, removed or misconfigured
> engine became a 404 from Anthropic *naming your engine*, which makes it look
> like the provider's fault.
>
> An unrecognised selector is now caught before it gets there, and says what
> actually happened: *"'DeepSeekReplika' isn't an engine I can reach any more.
> It may have been renamed or removed. Engines available: … Pick one in
> Settings, or set the default to Auto."*
>
> Telling a model id from an engine name is the trick: model ids are
> lowercase, hyphenated and versioned (`claude-sonnet-4-6`, `qwen3.6:latest`),
> while engine names carry capitals and spaces. It errs towards "this is a
> name", because being wrong that way produces a clearer error rather than a
> confusing one.

### 81. Claude is an engine like any other

It was a permanent built-in: you could neither edit nor remove it, so a wrong
key or a stale model id had nowhere to be corrected. Worse, the hardcoded
fall-through behind it is what turned any unrecognised engine name into a
`404 — model: DeepSeekReplika` from Anthropic.

**Add an engine called Claude with your own key and model and it takes over.**
Editable, removable, renameable, priced, and tagged cloud like everything else.
The built-in row is now only a **seed** — shown until you define your own,
hidden the moment you do, and back again if you remove yours. Only `Auto` and
`Ollama` remain reserved, because those are the router's own words.

This is the structural fix for the 404 rather than another guard in front of
it. The special case is gone, so the failure mode it created is gone with it.

> **Found it: the model, not the engine.** The Issues report gave it away in
> one line — `engine=Claude, provider=anthropic, **model=DeepSeekReplika**`.
> The Claude engine's MODEL field held an engine name, so every turn asked
> Anthropic for a model called DeepSeekReplika. Every guard I'd added checked
> the engine **selector**; none checked the model behind it, which is why
> three rounds of fixes changed nothing.
>
> Three parts now:
> **It can't be called** — Anthropic requests refuse a model that isn't a
> `claude-…` id, with a message naming the setting instead of a 404 naming a
> string. **It can't be saved** — Settings rejects a non-Anthropic model id
> and points at Engines for other providers. **And the one already saved is
> repaired on startup**, since it's sitting in settings.json failing every
> turn and finding it from a 404 is not a reasonable ask.
>
> **Take it over** now appears on the built-in Claude row: it fills the engine
> form with the Anthropic URL and a real model id, so you add your key and
> press Save. "Add an engine called Claude" was the right mechanism but an
> indirect instruction, when the complaint was that it wasn't editable.
>
> One of my own: I added the Anthropic model check to *both* call sites, and
> one of them is `OpenAIBrain`, where a DeepSeek model id is perfectly valid.
> The suite caught it immediately.

> **Fixed: the stretched tracked list.** Your screenshot showed the cause —
> the score sat in the middle of the row with the title shoved to the right.
> The score element was appended **before** the body, so it took the wide
> content column and the title was squeezed into the narrow one. It's now
> appended last, and the test asserts it: the score must be the final child,
> with the title before it.
>
> With the order right, the rest is sizing: the score drops from 17px to 13px
> and sits inline rather than stacked over its label, the monogram from 30 to
> 26px, row padding from 8px to 6px. The pipeline strip above stops stretching
> to fill the panel — six steps at `flex: 1` across 1400px read as cards when
> they're a readout — so it's capped and sits at its natural width.

> **Fixed: `/api/jobs/interview` returned 422.** The body model was defined
> **after** the routes that used it. FastAPI resolves the annotation when a
> route is registered, so a model declared later isn't found and the parameter
> is treated as a **query** field — hence "body: Field required", a
> server-side mistake that reads like the client sent the wrong thing. Match,
> Tailor CV and Interview prep were all affected; Score and Draft worked only
> because they happen to sit below the class. It's defined ahead of all of
> them now, and the suite asserts that ordering.
>
> **And "[object Object]" is gone.** FastAPI returns validation details as a
> *list of objects*, and the UI interpolated it straight into a string — so an
> error that names the exact missing field arrived saying nothing at all.
> A single `errText()` now renders any shape: a string, a validation list
> ("query: Field required"), an object with a message, or a fallback when
> there's nothing usable. It's wired through all 37 error paths, not just this
> one.

> **Fixed: the self-improvement panel 404'd on open.** It called `/api/self`
> while the route is `/api/selfimprove` — a rewrite renamed the caller and not
> the endpoint. A dead link between the front end and the server is invisible
> until someone clicks, so there's now a check that **every `fetch()` in the
> app reaches a declared route**, allowing for the ones that append an id.
>
> On the interview-prep 422: that was fixed in build `23:23`, and the report
> came from `23:05`. Worth deploying before retrying — the build stamp in the
> Issues report exists precisely to settle this question.

### 82. Code map — what imports what, and what breaks if you change it

Point **Code map** at a project folder. It reads the imports and answers the
four questions you have before touching unfamiliar code:

- **What depends on this?** Select a module and see the blast radius — direct
  importers and everything downstream of them.
- **What does everything lean on?** The chokepoints, where a mistake is
  expensive and a test is worth writing.
- **What imports each other in a circle?** Not a style complaint: those
  modules can't be understood, tested or moved separately.
- **What does nothing import?** Entry points and dead weight, separated,
  with the caveat that a script you run by hand looks the same from here.

Python is parsed with `ast` rather than matched with a regex, because a regex
over source finds `import` inside strings and comments and reports
dependencies that don't exist — the test feeds it exactly that and checks it
isn't fooled. JavaScript **is** a regex, and says so.

Run on this app it reports: **90 modules, 49,566 lines, one import cycle**
(`crew ↔ tools`), with `config` used by 56 modules and `audit` by 25.

> Two resolution bugs found by running it on real code rather than a fixture.
> `from . import memory` carries the target in the *names*, not in `module`,
> so every sibling import collapsed onto the package — making `agent` look
> like the thing 56 modules depend on when nothing imports it directly. The
> same fault in absolute form (`from pkg import a`) hid every real edge behind
> one. Both now resolve to the module.

### 83. Signed releases, a licence, and one-click install

**On "so no one can steal it" — the honest version.** A signature cannot stop
anyone copying published code. Anything on GitHub can be read, forked and
republished, and no signature, watermark or obfuscation changes that. What a
signature *does* is prove a copy is yours and unaltered — which matters most
here, because this runs commands on the machine it's installed on and a
tampered build could do real harm.

What governs whether someone may **use** it is the licence.

**PolyForm Noncommercial 1.0.0.** Free for personal, research, educational and
non-profit use. Running it in a business, using it for client work, or putting
it in something you sell needs a separate licence — `COMMERCIAL.md` says so
and how to ask. (I'm not a lawyer; this is a well-drafted standard licence, not
legal advice on your situation.)

**Ed25519 signing.** `python tools/sign.py --sign` writes a SHA-256 manifest of
every file and signs it. Anyone can check with `--verify`, offline, no account.
It names *which* file differs, because "verification failed" tells you
something is wrong while "agent/tools.py changed" tells you whether it's
tampering or your editor rewriting line endings. The private key is
`.gitignore`d; the public key ships.

**One click, including Python.** `install.bat` finds Python 3.10+, and if it's
missing **asks** before installing it — via winget, falling back to python.org.
Then it creates the venv, installs dependencies, **verifies the signature**,
and makes a desktop shortcut. Saying no changes nothing and explains what to do
instead. Installing a language runtime on someone's machine without telling
them is how installers lose trust, and this one is asking to run an agent that
executes commands.

### 84. Drawing the code map

**Code map → Diagram** draws boxes and arrows. Two decisions make it readable
rather than impressive:

**Layers, not a cloud.** Modules are placed by how deep they sit in the
dependency stack — entry points at the top, foundations at the bottom — so
arrows mostly point one way and it looks like the diagram someone would draw
by hand explaining the architecture. On this app that puts `config` and
`audit` on the bottom row, which is exactly right.

**Focus, not everything.** Ninety modules and three hundred arrows at once is
a hairball. The default shows the most connected 45; **click any box** and it
redraws that module's neighbourhood — what it uses, what uses it — and shows
its blast radius beside it. Click the background to come back.

**And it exports**, because a browser is not the best place to draw:

- **Mermaid** — paste into a README and GitHub renders it inline
- **Graphviz .dot** — `dot -Tsvg map.dot -o map.svg` for a proper drawing
- **CSV** — Visio's Data Visualizer builds a diagram straight from an edge
  list, which is the shortest route to something you can rearrange by hand and
  put in a document

The test asserts the property that makes a layered drawing correct: a
foundation must sit **below** the module that imports it. That holds however
the layout is tuned.

> **The diagram now reads left to right, and zooms.** Dependencies read as a
> sentence that way — work starts at the left edge, and the right edge is what
> everything rests on. On this app that puts `app` and the tests on the left
> and `config` at the far right, twelve columns apart. The Mermaid and
> Graphviz exports follow the same direction.
>
> **Scroll to zoom, drag to move, Fit to come back.** Zoom moves the SVG's
> viewBox rather than applying a CSS transform, so text stays crisp at any
> magnification instead of turning into a blur. The point under the cursor
> stays put, which is the difference between zooming and jumping. The
> container deliberately doesn't scroll — two scrolling mechanisms fighting
> is worse than either alone.
>
> **A real bug came out of the switch.** The first left-to-right drawing was
> 24,790 pixels wide: 45 modules across 92 columns. The longest-path
> calculation kept finding a longer route around the `crew ↔ tools` cycle and
> ran to its iteration bound. Cycles are now collapsed before depth is
> measured — members of a cycle share a depth, because for "what sits beneath
> what" they are one thing. Same diagram: 3,190 pixels, twelve columns.

### 85. Jobs — learning from what actually happened

**Jobs → Results.** Which sources produced replies, whether the fit score
predicts anything, whether email beats a portal form, and how long a reply
takes when one comes.

**The hard part isn't the counting — it's refusing to over-read it.** A job
hunt produces tiny samples. Five applications to one board with two replies is
a 40% rate, and it is *also* completely consistent with a board that converts
at 8%. A tool that prints "LinkedIn: 40%" there is worse than one that prints
nothing, because you will act on it — pouring effort into a board that was
merely lucky, dropping one that was merely unlucky.

So every rate carries a **Wilson interval**, drawn as a bar with the observed
rate marked on it. A wide bar reads as "we don't really know" without needing a
caption. One reply from one application shows as *21%–100%*, not 100%.

A finding is only stated as confident when **two intervals don't overlap**.
Below twelve applications it says so plainly and tells you roughly how many
more you'd need. Findings come with the evidence attached, so you can disagree.

**The finding worth building this for** is the one about your own scoring:

> *"Roles you score HIGHER reply less (4% vs 67%). The score is measuring
> something other than what gets replies — probably how well the advert
> matches your profile's wording rather than whether you'd get the job."*

Nothing adjusts itself. It reports; you decide.

> An earlier version also required each interval to be narrower than 0.35,
> which sounds rigorous and is useless: a rate near 50% needs about eighty
> applications to get that tight, so the module would have stayed silent in
> exactly the situations it exists for. Non-overlap is the right test.

> **Fixed: "DeepSeek could not find the model `gemma4:24b`".** That's an
> **Ollama tag sent to a cloud API** — a local model routed to a cloud engine.
> The 404 reads as "your model id is wrong" when the real fault is *where it
> was sent*, so the advice it gave (check your provider's naming) would have
> sent you looking in the wrong place entirely.
>
> This is the mirror image of the Anthropic guard: that one caught an engine
> NAME going into a cloud model field, this catches a LOCAL model going to a
> cloud endpoint. The colon is the giveaway — cloud providers use slashes and
> hyphens, Ollama uses `name:tag` — and the same tag on a **local** endpoint
> still passes, including a machine on your LAN.
>
> **And the message named the wrong company.** `OpenAIBrain` started life as
> the DeepSeek client, so every OpenAI-compatible engine reported "DeepSeek"
> problems — telling someone their Mistral engine has a DeepSeek fault sends
> them to the wrong dashboard. Errors now name the engine you actually
> configured.

> **The `gemma4:24b` saga, and what it was really about.** The user found it,
> after three rounds of me fixing adjacent things: the engine had been **named**
> after a model id. That's ambiguous, because the dispatcher resolves by name
> first and then falls through to treating the string as a model id — so
> "not found" could mean either, and no error message could tell you which.
>
> Three things came out of it:
>
> **The trap is closed.** An engine name that looks like a model id is refused
> *when it points at a different model*. Naming an engine after the model it
> actually runs stays fine — that's the app's own convention for local models,
> and there it reads correctly.
>
> **A local 404 now answers the question.** Ollama can be asked what it holds,
> so instead of advice about provider naming prefixes you get: *"`gemma4:24b`
> isn't installed. This machine has `gemma4:12b`, `qwen3.6:latest`… Did you
> mean `gemma4:12b`?"* Which was the answer all along — the machine had the
> 12b, not the 24b.
>
> **Errors name the engine.** Every custom engine was labelled "custom", so
> messages read "Custom could not find…" instead of using the name you gave it.

### 86. Fine-tuning a local model, by asking

Say *"fine-tune a 14B to answer like a Power BI consultant"* and it runs the
whole thing: plan, dataset, training on your GPU, a blind comparison against
the model it started from, and registration as an engine — **only if it won**.

**It tells you the truth first.** Tuning teaches a model HOW to answer — shape,
register, what to lead with. It does not install knowledge, and synthetic
examples can only pass on what the generating model already knew, mistakes
included, with the confidence attached. For Power BI specifically the plan
says: *"Wrong DAX runs, returns a number, and nobody notices. A model that has
learned to sound authoritative about formulas it gets wrong is more dangerous
than one that hedges."*

It also offers the cheaper thing first: a Modelfile variant with a system
prompt takes seconds and no GPU, and is often enough. If you want the model to
**know** the subject rather than sound like it does, the plan points at
retrieval instead — correctable in seconds, and it cites what it used.

**The gate is the point.** A fine-tune scoring worse than its base is a common
outcome, not a rare one. Some examples are held back and never trained on;
afterwards both models answer them and a judge picks a winner **without being
told which is which**. If the tuned model lost, it says so and keeps the base.

**Each stage stops.** Deliberately not one unattended call that plans,
generates, trains and registers — that would be hours of GPU on a dataset
nobody read. Every stage reports and waits, and `sample` shows you twenty
examples before you commit to them.

> **Fixed: the dataset step crashed with `module 'agent.brain' has no
> attribute 'get_brain'`.** I invented that function and never checked it
> existed. The running brain was already a parameter of the call — building a
> second one would have used different settings and a different engine than
> the conversation the user was having, so it now uses the one it was given.
>
> **The interesting part is why nothing caught it.** `module.attr` is legal at
> import time and only fails when that line runs, so pyflakes, the compiler
> and 1,227 tests all passed while the bug sat in the middle of a step someone
> had waited on. There's now a check that walks every `module.attr` in the
> package where the module is one of ours and confirms the attribute is
> actually there. It found exactly two others, both a genuinely optional
> Windows DPAPI helper behind an import guard.

> **Fixed: the document index capped silently.** The file limit `break`s out
> of the walk, so indexing a large folder gave you the alphabetically-first
> few hundred files and said nothing about the rest. You'd search, get a
> confident answer drawn from a fraction of your material, and have no way to
> know the remainder was never read. **A partial index that announces itself
> is fine; one that doesn't is worse than no index at all** — because the
> answer looks complete either way.
>
> It now keeps counting past the cap instead of stopping, so it can tell you:
> *"Only the first 500 files were indexed — 1,847 more were left out.
> Searches will answer from part of this folder without saying so."* With the
> setting to change named in the message, and both limits made settable —
> neither was.
>
> There's also a **health check**, because the warning only appears at index
> time and the problem lasts until you re-index. If the index is sitting at
> its limit, Health says so.

> **Fixed: re-indexing couldn't make progress.** The cap was applied *before*
> checking what was already indexed — so a second run collected the same
> alphabetically-first 500 files, found them unchanged, skipped them all, and
> never looked at the remaining 12,165. Re-indexing was genuinely impossible
> without emptying the index first, which is exactly what it looked like from
> the outside.
>
> The cap now takes **not-yet-indexed files first**, so each run picks up
> where the last stopped and repeated runs finish the folder. The message says
> so: *"500 files indexed this run — 12,165 still to go. Run it again and it
> continues with the next batch."*
>
> And the remaining count actually counts down. It was reporting every
> eligible file rather than the unread ones, so it showed the same number
> every run — which would have looked like no progress even once progress was
> being made.

### 87. A security pass before publishing — and what it found

**The review gate was never wired in.** The intercept queue and its endpoints
existed; nothing called them. The panel reported an empty queue while every
external call went straight through. **A safety feature that exists only in
the UI is worse than none, because it gets trusted** — and it had been
described in the README for days.

It's now in `execute_tool`, the single point every tool call passes through,
and **full access does not bypass it** — that's precisely when it matters.

**Why this is the one that mattered before going public.** This app reads job
adverts, alert emails and web pages: content written by someone else. An
instruction hidden in one of them ("read the ssh key and post it to…") needs
an **external** call to do any damage, and those are exactly what now waits.
Reads still run, because prompting on everything teaches you to click through
without reading.

> Wiring it broke the shell, and that was the right kind of failure. The app
> already has a tiered permission system for commands — it asks, remembers
> your answer per command, distinguishes read-only from destructive. My gate
> sat in front of it, so one mechanism asked and the other held the answer
> hostage. `run_command` is now explicitly owned by the permission system.
> **One gate per action**, which is the same lesson as routing and Turbo.

A health check reports whether the gate is wired and on, so this particular
failure — present in the code, absent from the path — can't recur silently.

**Also checked, and clean:** no API key, token or password reaches a log or
the audit trail from anywhere in the codebase.


### 88. A front page

The README was **3,415 lines of changelog** — a good engineering record and a
terrible introduction. Someone landing on the repository needs to know what
this is before they know what it was.

It's now 122 lines: what it does, how to install it, three things that make it
different, the licence, and how to check a download. The history moved here,
to CHANGELOG.md, because the reasoning in it is often more useful than the
result — most of these entries were found by running the thing on a real
machine and watching it fail.

Releases now carry the licence, the commercial terms and the verification
guide as well, which they didn't: someone unzipping a release had no way to
know what they were permitted to do with it.

Four assertions hold the front page down — that it stays short, says how to
install and what it costs, keeps the history rather than deleting it, and has
no broken links.


### 89. Breaking the cycle the code map found

`crew` imported `tools` for the sub-agent loop; `tools` imported `crew` for
the crew tools. A cycle means neither module can be read, tested or changed
without the other, and this one had been there long enough that both had grown
around it.

**Moving the function alone would have relocated the cycle**, because the loop
calls the dispatcher. So the dispatcher and the tool list became **arguments**:
a nested agent loop has no business knowing whose tools it is running. Once it
doesn't, `subagent.py` imports nothing that imports it back.

That left `crew` still importing `tools` for a fallback. Since **tools calls
crew**, tools now registers its dispatcher with crew on import — the arrow
points one way, and not a single caller changed. `crew.run()` from the server,
from a schedule, from a tool call: all work exactly as before.

Five tests in the suite were patching `tools.run_subagent`, which silently
stopped being the thing that ran. They now patch where the loop actually
lives — a test that patches the wrong object passes while testing nothing.

One cycle remains — `health ↔ selfimprove ↔ tools` — and it is a different,
larger shape. Named here rather than left for the code map to rediscover.


### 90. Asking for a shape instead of asking nicely

Asked what modern capability was missing, the honest answer wasn't a feature —
it was a foundation that eight existing ones stand on.

**Eight modules asked a model for JSON by instruction** ("Return ONLY raw
JSON, no prose, no fences") and three carried their own extractor that strips
fences, hunts for the first `{` and counts braces. It works most of the time,
which is the problem: the failures are silent and uneven. A challenge brief
comes back as prose and the scan reports nothing found. An eval verdict loses
a field and the score is wrong rather than absent.

Every major provider now solves this properly — define a schema, force a tool
call with that shape, and the API validates before you see it. This app used
it nowhere.

**`structured.ask()`** does that where the engine supports it and falls back to
instruction-and-parse where it doesn't, with the same contract either way so
callers don't branch on which engine they're using. One extractor instead of
three — the shared one handles a brace inside a string, which the old ones got
wrong.

**Validation says what's wrong in words a model can act on**: *"score should be
a number, but the text 'high' was sent"*, not "invalid". That matters because
of the repair step: a bad reply is shown its own fault **once**, and told not
to invent values to fill gaps. In the migrated call site, a prose reply that
previously lost the whole proposal now recovers on the second attempt.

**It fails loudly rather than guessing.** A missing required field is a failure
with the field named, because a plausible default is how a wrong number ends
up in a report nobody questions.

`challenges.propose` is migrated as proof. The other seven can follow one at a
time — each is a schema and a two-line change.


### 91. Building a predictive model from a table

*"Build a model from this CSV to predict churn."* It profiles the data, hunts
for leakage, trains several candidates, validates, tests once, and runs
inference. Classification or regression, decided from the target.

Training is four lines of scikit-learn. Everything else here is what makes the
number at the end mean something:

**A baseline, always.** 94% accuracy sounds excellent and is worthless on a
dataset that's 94% one class. Every score is reported next to the dumbest
possible predictor, and when the model doesn't beat it that is the headline:
*"It has not learned anything useful."*

**The test set is touched once.** Three splits — train, validate, test. Choose
a model on the test set and its score stops predicting anything about new data
and becomes a description of that sample.

**Leakage is hunted.** The commonest way to get a 99% model on business data is
a column that already holds the answer — `cancellation_date` in a churn table.
Any single feature that nearly predicts the target alone is flagged, with the
reason: *"either it restates the answer, or it's recorded after the fact — in
which case it won't exist when you need a prediction."*

**Inference refuses to guess.** A missing column is an error naming it, not a
silent fill: guessing gives you a confident answer built on nothing. And every
prediction carries its confidence, because 51% sure and 99% sure look
identical otherwise.

> **Three bugs found by running it on data with a planted leak**, which is why
> the fixture has one. `dtype == object` is a pandas 2 idiom — in pandas 3 a
> text column is `str`, so the leak check silently converted nothing, the
> exception was swallowed, and it reported "nothing looks like leakage" having
> checked none of them. The same idiom meant identifier columns were never
> dropped. And the categorical imputer filled missing values with the
> commonest one — which turned a column that was blank for one class and
> filled for the other into a constant, destroying the very signal it carried.
> Missingness is now marked as a category rather than guessed away, and
> unchecked columns are listed rather than counted as clean.


### 92. Any data, the right tool, and honest advice about the card

*"Build a model from this"* now works on a table, a column of free text, or a
folder of image folders. It works out which it is, picks what wins for that
shape and size, drops columns that leak the answer, trains, and reports
against a do-nothing baseline. One call.

**The part that matters most is where it declines to use your GPU.**

Asked to "make it use CUDA", the honest answer has a piece people don't
expect: **deep learning is the wrong tool for most business tables.** On a few
thousand rows of mixed numeric and categorical data, gradient boosting beats
neural networks, trains in seconds on a CPU, and needs no tuning. Putting a
600-row churn table on a 3090 would be **slower and no more accurate** — the
transfer costs more than the arithmetic saves. So it says that, in those
words, rather than adding a switch that flatters the hardware.

Where the card genuinely earns its place is text and images. Even there the
state of the art is usually **not** training a network from scratch:

- **Text** — a pretrained model turns each row into an embedding and a simple
  classifier learns on top. Under a few thousand examples this beats
  fine-tuning, which just memorises them. Embedding is the only slow part, and
  it is exactly what the GPU is for.
- **Images** — a network trained on millions of images already knows edges,
  textures and shapes. Using it as a feature extractor and learning only the
  last step needs dozens of images per class rather than hundreds of
  thousands.

**"Just prompt it" means less typing, not fewer checks.** The baseline, the
single-use test set and the leakage hunt all still run — in the demo, `auto`
found and dropped the planted leak on its own and reported 78.3% against a
75.8% baseline rather than the 100% the leak would have bought.

> The torch paths are written against a machine that has it and **could not be
> run here** — this sandbox has no GPU and no PyTorch. They fail with
> instructions rather than a traceback when a dependency is missing, and that
> failure path is tested. The tabular path, the detection, the planning and
> the GPU advice are all tested properly.


### 93. Cleaning data, and features that have to earn their place

Finds duplicates, missing values, numbers and dates stored as text,
inconsistent spellings, constant columns and extreme values — then cleans and
builds features. Two design decisions carry most of the weight:

**Nothing is changed silently, and nothing is changed in place.** Diagnosis
reports counts and does nothing. Cleaning writes a **copy** with a
`.changes.json` beside it recording every decision and its effect. A cleaning
step you later decide was wrong is only recoverable if the thing it was
applied to still exists.

**Outliers are flagged, never removed.** An outlier is often the most
interesting row — the fraud, the churn, the contract worth more than the rest
of the book. Deleting them automatically trains a model to predict everything
except what you care about. Filling a numeric gap adds a `_was_missing` flag
alongside, because the absence is frequently the signal and filling erases it.

**Features are measured, not assumed.** Multiplying every column by every
other gives you hundreds of features, a model that fits beautifully and
predicts nothing. Features are proposed *for reasons* — a date has a weekday
in it, two related amounts have a rate between them — then built and checked
on held-out data. The verdict is explicit either way: *"score went from 0.850
to 0.875 — a real gain. Worth keeping"*, or *"that is not an improvement: the
new columns are adding noise. Use the original file."*

> A false positive caught in testing: the id column `R0, R1, R2…` was read as
> South African currency, because stripping a leading `R` turns `R0` into `0`.
> Acting on that would have destroyed the column. A currency symbol now only
> counts when followed by a space or by digits with a separator — `R 8,784` is
> money, `R0` is an identifier.


### 94. Models gets its own panel

Model building was prompt-only — no endpoints, no UI. Now there's a **Models**
panel with four stages:

**Data** — what's wrong with the file, each problem with its risk stated, and
the plan: which approach and **whether the GPU would help**. For a table it
says plainly that it wouldn't.

**Result** — the verdict, with the score shown *next to what guessing scores*.
That pairing is the point: 81% means nothing until you know the baseline is
75%. Underneath: what it tried, which columns it used, which it dropped as
identifiers, and which it dropped as leaks.

**Predict** — a row as JSON or a file of rows. Confidence comes back with each
prediction, and a missing column is refused rather than filled in.

**Saved** — every model with its baseline, and a warning on any that doesn't
beat doing nothing.

The gates that make this trustworthy had to be visible in the UI too, or the
number on screen is just a number. In the test run the panel found and dropped
the planted leak by itself and reported 81.3% against a 75.0% baseline.


### 95. A real model builder

The first panel was a thin wrapper over a richer flow — a fair criticism. Seven
tabs now, and three changes that alter the answers rather than the appearance.

**Your own test set.** Score a model on a file *you* held back. This is the
only fully fair test: even a held-out split was carved from data whoever
cleaned it could see. It refuses a file without the answer column — there is
nothing to check against — and compares the result to the model's own test
score, so a flattering split is visible.

**The right headline.** Accuracy is the wrong number when classes are uneven.
On the demo data the model scores 76% accuracy and **catches 45% of the
churners** — the first version reported the 76% and stopped. It now leads with
balanced accuracy when one class dominates, says why, and reports per-class
recall: *"yes: catches 45%, right 55% of the times it says so."*

**The threshold is a dial.** 0.5 is a default, not a decision. The same model
catches **45% or 91%** of churners depending where the line sits, trading
precision as it goes. The curve is shown with a suggested balance — and the
note says plainly that which way to move it depends on what each mistake costs
you, which only you know.

**Data engineer** — every column with what you can do to it: cast, rename,
drop, trim, fill, bin, log, clip or flag outliers. One action at a time, each
reporting its own effect ("converted; 14 values wouldn't parse and are now
missing"), always to a copy, and the panel follows the edited file so actions
compose.

**Features** — proposed with reasons, then built and measured **one at a
time**. A batch tells you the set helped; one at a time tells you which. A
feature that doesn't move the held-out score is called noise.

Also added: **feature importance** with the warning that importance is not
causation, and ROC-AUC for how well it ranks regardless of the cut-off.

**Still missing, and worth doing:** cross-validation instead of a single split
(the score moves several points between seeds on small data), calibration
(is 80% confidence right 80% of the time), experiment history to compare runs,
and export of a standalone scoring script.


### 96. Cross-validation, and knowing when a winner isn't one

The top item on my own list of what was missing, and the one that decides
whether any other number can be trusted.

Models were chosen on **one random split**. On a few hundred rows that score
moves several points between seeds, so a 3% gap between two candidates can be
nothing at all — and the panel presented it as a decision.

Each candidate is now scored across **five folds**, and the spread is reported
with the mean. On the demo data:

```
logistic regression   0.780 give or take 0.026
random forest         0.782 give or take 0.012
gradient boosting     0.784 give or take 0.032
```

Gradient boosting "won" by **0.002 with a spread of 0.032**. It says so:
*"That gap is inside the noise — treat them as equal and prefer whichever is
simpler to explain."* The old single split would have reported a winner.

**Importance is now measured rather than read off.** `HistGradientBoosting`
exposes no weights, so the old code silently failed whenever it won — which
cross-validation made the common case. Permutation importance works for any
model: shuffle a column, see how far the score falls. Slower, and truer,
because it says what the model actually relies on rather than how it happens
to be built. On the demo it correctly shows `customer_id` contributing
**nothing**.

> And a failed fold is now a reported result rather than a console warning.
> sklearn prints `FitFailedWarning` and returns NaN — a silent failure wearing
> a warning. It now says *"2 of 5 folds couldn't be fitted — usually too few
> rows, or a class that doesn't appear in every fold"*, where someone will
> read it.

**Still on the list:** calibration (is 80% confidence right 80% of the time),
experiment history, and export of a standalone scoring script.


### 97. A big folder is mostly not knowledge

Reported with a diagnosis good enough to fix it from: a 36,500-file / 4.3 GB
knowledge base that "never finished". It wasn't stuck — **23,000 of those
files were screenshots**, 3.7 GB of them, and only 12,595 files (195 MB) were
text at all.

**Two causes, both fixed.**

The walk used `rglob("*")`, which descends into every directory before
anything can be filtered — so it enumerated `.git`, `node_modules`, `bin` and
`obj` in full. It now walks with `scandir` and **prunes those folders
entirely**, never entering them. On a test fixture that took 1,020 files down
to 120, with the 750 inside build and vendor folders never touched.

Binaries are skipped **by extension before the file is stat-ed**, and counted
separately so the report can say what it skipped rather than merely that it
skipped: *"900 image/binary files were skipped without being opened — they
aren't knowledge, and reading them is what makes a big folder look like it has
hung."*

**And a survey, because nothing had looked first.** *"What's in there?"* counts
the folder in seconds and tells you what indexing would actually involve:

> 1,020 files, of which **120 can be indexed** (0.5 MB). 900 images and
> binaries skipped without being opened. 3 build/vendor folders skipped
> entirely. Roughly 721 chunks, under a couple of minutes.

With the file-type breakdown underneath, and the estimate labelled as an
estimate. A folder that appears to hang is usually one nobody counted first —
and counting costs seconds.


### 98. Indexing that finishes

Reported twice: indexing a large knowledge base "runs forever". Pruning the
walk helped, but the walk was never the bottleneck. Three compounding causes,
and the third is why it felt hopeless rather than merely slow.

**One embedding call per file.** 12,595 files meant 12,595 sequential HTTP
round-trips to Ollama before any real work happened — over an hour of pure
latency. Chunks are now batched **across** files, 256 at a time. On a
400-file test that took 400 round-trips down to **7**.

**One commit, at the very end.** Stop it at hour three and you had indexed
nothing. Each batch now commits, so progress is durable from the first batch
onward.

**One blocking browser request**, which times out long before thousands of
files are done. Indexing now runs in **bounded passes**: each call works for
about a minute, commits, and reports how much is left. The UI loops the passes
itself, so you still press one button — and there's a **Stop** that keeps
everything done so far.

Together with unindexed-files-first from the earlier fix, that makes the whole
thing resumable: a 600-file test finished across three one-second passes with
nothing lost, and re-running a finished folder costs nothing.

> Why bounded passes rather than a background thread: a run that outlives its
> request needs progress polling, cancellation and a way to survive a restart.
> A finite pass that saves its work needs none of that, and is honest about
> where it got to.


### 99. Finding a conversation, and filing one away

**Search, across everything you've said.** Titles are generated and often
generic, so searching them alone misses the conversation you actually
remember — you remember a phrase from it, not what it ended up being called.
It searches message content, shows **the line it matched** so you can
recognise the conversation without opening it, and counts repeat mentions. It
searches across groups on purpose: when you're hunting for something you
remember saying, you rarely remember which group you filed it in.

**Filing now actually files.** A conversation moved into a group stayed in the
general list as well, so the list never got shorter — which is the only reason
to file anything.

The fix isn't to make "All conversations" quietly stop meaning all. The
default view is now **Ungrouped**, and **Everything** is still there under its
own honest name. A filed conversation leaves the first and stays in the
second.


### 100. An installer that works on the second machine

The old one was 192 lines of PowerShell that did one thing — find Python — and
gave up quietly on anything else. It worked on the machine it was written for,
which is how installers usually fail: the next PC has no winget, or a proxy,
or an antivirus, or Windows' blocked-file mark on the download, and the script
exits with nothing to go on.

**PowerShell now does only the irreducible part** — putting a Python on a
machine that has none — in 67 lines. Everything after that is
`install_agent_jo.py`, in Python, where it can be tested and where a failure
can explain itself.

**Every failure names its own fix.** "pip install failed" is useless on a
machine I can't see. Instead:

- *"pip couldn't reach the internet. Behind a corporate proxy, set HTTPS_PROXY
  and run this again."*
- *"A package needs a compiler. Install the Microsoft C++ Build Tools."*
- *"Windows blocked a file — usually antivirus. Allow the folder and run this
  again."*

**Things the old one never handled**, each of which silently breaks an install
on a second machine: the **blocked-file mark** Windows puts on anything
downloaded (cleared before anything else runs — the commonest cause of "it
worked on my PC"); **ARM64 vs x64** Python; a **corporate proxy** on the
download; the **260-character path limit**; and whether the app **actually
loads** once installed, rather than assuming a finished install is a working
one.

**`install.bat --check`** surveys the machine and changes nothing. And every
command and its output goes to `install.log`, with a machine-readable
`install-report.json` beside it — so a failure on a PC I can't see is still
diagnosable from a file you can send me.

> Renamed from `setup.py`: that filename means something specific to pip, and
> a stray `pip install .` would have tried to treat the installer as a package
> build script.


### 101. A fresh install could never be configured

Reported: "it installed but I cannot click on anything." The log gave it away
exactly.

`AnthropicBrain.__init__` called **`sys.exit(1)`** when no API key was set.
That is reasonable in a command-line tool and catastrophic in a web server: it
raised `SystemExit` inside a request, `/api/meta` returned 500, and the front
end never finished loading. Nothing rendered, so nothing could be clicked.

**And a fresh install has no API key by definition.** So the one page that
could accept a key was the page that couldn't render — the app was unusable on
every new machine and perfectly fine on any machine where a key already
existed. Which is precisely the pattern reported.

Four `sys.exit(1)` calls have gone from the library. A library reports; the
caller decides. `EngineNotConfigured` carries the message and the fix, the CLI
still stops with a clear line, and the web server carries on — describing the
engines no longer requires *having* a working one. A keyless install now loads
every page, and sending a message returns a plain *"No engine is configured"*
rather than a crash.

> The test parses the AST rather than grepping for `sys.exit`: the docstring
> explaining the fix mentions the very call it removed, and a text search
> can't tell those apart.

**Also from that log: it was running from `%TEMP%`.** Unzipping and launching
in place is the natural thing to do, and Windows clears that folder whenever
it likes — taking the app, the venv, your conversations and your settings with
it. Health now says so, and says where to move it.


### 102. Yes, on a Mac — now

Asked whether it runs on macOS. The app always did: it installs and serves
fine on a non-Windows machine, which is how the whole suite is tested. What was
missing was **a way in**. No `.command`, no `.sh`, and an installer that told a
Mac user to run `install.bat` — small, and the kind of small that makes someone
conclude the thing isn't meant for them.

- **`install.command`** — double-click in Finder. Offers Homebrew if you have
  it, points at python.org if you don't, and never installs Homebrew on your
  behalf: that's a large thing to add to someone's machine, so it's printed as
  a line to copy.
- **`start_agent_jo.command`** and the `.sh` equivalents, both going through
  `run_web.py` exactly as the Windows launcher does — so all three behave the
  same rather than each picking a port and a browser differently.
- Every message now names the right launcher for the platform.
- **Blender is found inside a `.app` bundle.** macOS buries the binary in
  `Blender.app/Contents/MacOS`, so `which blender` finds nothing on a machine
  that plainly has it.

> Two bugs came out of testing rather than reading. The launcher used a
> `--web` flag that doesn't exist — caught because the test actually curled
> the running server instead of checking the file. And `install.sh --check`
> still asked "Start Agent Jo now?" and waited forever with no terminal
> attached, which hung a test run for five minutes. Both installers now skip
> the prompt after `--check` and when nothing can answer; the Windows one
> defaults to No after 30 seconds.


### 103. Notifications, with three positions rather than two

**Settings → Notifications.**

A single on/off switch would have been the wrong shape. "Off" for a routine
confirmation and "off" for a failure are different requests, and silencing the
second is how you find out a week later that nothing ran. So:

- **Everything** — every confirmation
- **Only what needs me** *(default)* — errors, warnings, anything waiting on a
  decision; drops the messages that only tell you what you just did
- **Nothing** — silent, and the setting says plainly that failures go quiet too

All 68 in-app messages already went through one `toast()` function, so gating
there governs every one of them rather than each caller deciding.

**Desktop notifications**, off by default, for when the window isn't focused —
a toast nobody is looking at has notified nobody. Permission is requested when
you switch the setting on, not when the app loads: an app that asks on load is
one people refuse out of reflex.

> Two bugs caught in testing. An unrecognised level was accepted and stored —
> and since it matches none of the three, notifications would simply have
> stopped while the setting looked saved; it's now refused with the valid
> options named. And the gate initially swallowed `toast()`'s return value,
> which the command-palette test depended on. That test now sets the level
> explicitly and asserts the gate in both directions.


### 104. The switch now governs the cards people actually mean

The notification setting gated the transient toasts and nothing else, so
turning it down changed nothing about the cards sitting permanently at the top
of the window — "Drafts to rewrite", "Trends waiting on you", "Backup is
stale". Those are the notifications most people mean, and a switch that
doesn't move them is a switch that doesn't work.

The cards already carried a severity — **act**, **review**, **note** — which
maps onto the setting exactly:

- **Everything** — every card
- **Only what needs me** — the ones asking you to do or decide something; the
  notes are dropped
- **Nothing** — none

**And a dismiss on each card**, because a global switch is too blunt when it's
one card you're tired of. Cards now carry a stable id derived from the title
rather than the detail — *"Backup is stale"* stays the same key while "15 days
old" becomes "16 days old" — so a dismissal survives a restart and the card
returns if the situation genuinely changes.

**Off means quiet, not blind.** A line stays at the top saying how many cards
the setting is holding back, and another offering to bring dismissed ones
back. Otherwise the setting becomes a trap: silence that looks like
everything being fine.


### 105. A saved engine you could not see

Reported from a fresh laptop: saving a new engine stored it, and the list
never showed it.

**My bug, and a recent one.** Fixing the keyless-crash last build, I added an
early `return items` to `_engine_list()` when no brain could be built — and it
sat *before* the loop that appends the user's own custom engines. So on a
machine with no API key, every engine you saved was written correctly to disk
and was invisible in the picker. The fix skips only the part that needs a
brain.

**And Claude no longer gets offered as if it works.** With no key it now
carries `needs_key`, the picker labels it *"Claude (needs an API key)"*, and
the hint says what to do instead of leaving you to discover it on the first
message. The stored default is `Auto`, which routes hard work to the cloud
tier — Claude — so on a keyless machine it failed the same way while looking
fine.

The app now reports two things rather than one: **`default_engine`**, the
setting exactly as you saved it, and **`start_engine`**, the one the picker
opens on — the stored default when it can run, otherwise the first engine that
can. A local engine needs no key, so a fresh install with Ollama simply works.

> An earlier version collapsed both into `default_engine`, which quietly
> turned a stored setting into a suggestion and broke the settings round-trip.
> A test from months ago caught it — one field, one meaning.


### 106. Your engine, not Anthropic's

From GitHub: a user added a DeepSeek key, said "hi", and got a 500.

`BACKEND` ships as `anthropic`, so `get_brain()` built an **Anthropic** brain
before the request's chosen engine mattered at all. Last build stopped that
killing the process; it still failed the message. And the traceback named
Anthropic — the least useful thing it could have said to someone who had just
configured DeepSeek.

`make_brain()` now falls back to an engine you actually configured: local
first (nothing to get wrong), then any cloud engine carrying its own key. With
nothing configured, the original error stands rather than being replaced by a
vaguer one.

**And the exception no longer reaches the browser as a stack trace.** An
unconfigured engine is a 503 saying what to do.

> Two bugs found on the way, both of the same kind — something resolved once
> and then relied on forever.
>
> `_ENGINES_FILE` was a module constant computed at import, so it pointed at
> whatever `AGENT_HOME` was at that moment. Anything that changed the home
> afterwards — a test, a second profile, a moved data folder — kept reading
> and writing the old location while appearing to work. It's resolved when
> used now.
>
> And my exception handler was inserted between `@app.middleware("http")` and
> the function it decorated, so the handler quietly became middleware and the
> error capture lost its decorator. Both are now asserted.


### 107. "\\ was unexpected at this time"

Reported: the app wouldn't start at all. The folder name gave it away —
**`AgentJo-2026-09-18 (1)`**, the name a browser gives a second download of
the same file.

cmd expands `%~dp0` as literal text. Inside a parenthesised block, the `)` in
`(1)` closes the block early, and the trailing backslash escapes the quote
after it. Both `install.bat` and `start_agent_jo.bat` had a `%~dp0` inside a
block, so both died before doing anything — with a message that names neither
the file nor the cause.

Both now name the folder **once**, in the `cd` at the top, and use relative
paths from there. Blocks that referenced it became `goto` labels.

Two checks scan every `.bat` in the repository for the pattern: no path
expansion inside a parenthesised block, and every block closes. This is not a
bug you find by reading — it depends on what the folder is called.

> Anyone hitting this on an older build can rename the folder to remove the
> brackets and it will start. The fix means you don't have to.


### 108. No vendor is the default

Asked why Anthropic couldn't simply stop being the main model. The honest
answer was that it could, and I had been patching around it instead.

`BACKEND` shipped as `"anthropic"`. One line, and everything followed from it:
a user with a DeepSeek key still had an Anthropic brain constructed before
their engine was consulted. **Four fixes across four builds — the keyless
crash, the engine fallback, `needs_key`, `start_engine` — were all downstream
of it.** Each was a real fix; none touched the cause.

It ships as `"auto"` now, resolved when a brain is built:

1. **an engine you configured** — local before paid, because a local model has
   no key to be wrong
2. **a running Ollama**
3. **Anthropic**, if a key exists
4. otherwise a plain *"No engine is set up yet"* naming where to add one

An Anthropic key no longer outranks something you chose yourself. A
Claude-only setup is unaffected, and `--backend anthropic` still does exactly
what it says.

`MODEL` stays a Claude id, and that's correct — it is the Anthropic model
field, and putting another vendor's model in it is the bug `repair_model()`
exists to undo.

> Worth recording: four consecutive builds treated symptoms of a
> one-line cause. The question that fixed it was not a bug report.


### 109. A test that can fail, and a save that doesn't stall

**"Test connection" never connected.** It checked that two fields weren't
empty and said *"Looks valid. Save it, then send a message to confirm the
endpoint responds."* A test that cannot fail is not a test — it sent people
away confident about an endpoint nobody had contacted.

It now sends one real message and classifies what comes back, because "it
didn't work" is not a diagnosis when a wrong key, a wrong model id, a stopped
server and a firewall all produce the same red text:

| what happened | what it says |
|---|---|
| 401 | the provider rejected the API key — check for a copied space |
| 404 | *'qwen3:8b'* isn't a model this endpoint knows — `ollama list` |
| 429 | rate-limited, **which means it connected** |
| no credit | connected, but the account has no credit |
| refused | couldn't reach it — is Ollama running? `ollama serve` |
| TLS | the certificate wasn't accepted — common behind a proxy |

Local and cloud endpoints get different advice for the same symptom, and a
success says plainly that a working connection isn't a working model.

**Two real hazards fixed underneath it.** A provider error arrives from the
brain as *text* rather than an exception, so it bypassed the classifier
entirely and an unreachable host read as a vague "provider problem". And the
chat client uses a 600-second timeout with two retries — sensible for a long
generation, catastrophic for a connection test, where a host that drops
packets would have hung the button for **half an hour**. The probe carries its
own timeout and no retries.

**The save stall was mine.** Making the backend vendor-neutral last build
meant `resolve_backend()` constructs an `OllamaBrain` to see if one is
running — a network probe of up to three seconds, run every time a brain is
built, and the UI rebuilds one immediately after saving an engine. Whether
Ollama is running doesn't change between two clicks, so the answer is cached
for 30 seconds.


### 110. Picking Claude has to reach Claude

Reported: calls went to DeepSeek while Claude was selected, and other engines
errored. One cause, and it was mine.

`_force_model("Claude")` returned **`None`** — meaning "use whatever this
brain defaults to". That was Anthropic for as long as the backend was
hardcoded. Two builds ago the backend became whichever engine you configured,
so `None` started meaning *your* engine: **choosing Claude called DeepSeek.**
Naming a Claude model id instead was no better — it sent `claude-sonnet-4-6`
to DeepSeek, which is the `400 invalid model name` in the report.

Claude routes **by name** now, exactly like a custom engine. With no key it
says so instead of falling through to something else.

> Narrowing that took three attempts, and each one was the suite refusing a
> worse version. Intercepting any Anthropic-looking model id bypassed
> `HybridBrain`'s own cloud side — a test's injected client was ignored and a
> real API call went out. Only the engine **name** is intercepted; a bare
> model id still belongs to the calling brain, which was always the contract.

**The other half of the report** — `DeepSeekReplika` isn't installed, run
`ollama pull DeepSeekReplika` — is an engine called Nemotron with another
engine's **name** in its model field. Ollama was then asked to pull a model by
that name, so the error blamed the model rather than the mixed-up field.
`repair_model()` has done this for the global setting since August; custom
engines had no equivalent. Health now names the engine, the wrong value and
where to fix it — and an engine named after its own model, which is the app's
convention for local ones, is not flagged.


### 111. A default destination is never a kindness

Deleting every engine and adding them back fixed it, and that tells you what
was wrong: the stored entries had lost their base URL.

`OpenAIBrain` did `base_url or config.DEEPSEEK_BASE_URL`. So an engine with no
base URL **silently called DeepSeek** — which is why the error read "could not
reach the DeepSeek endpoint" on a machine whose engine was called Nemotron and
pointed at Ollama. An error naming a service you never chose, on an engine you
did.

Three fixes, all the same principle: **fail where the problem is.**

- An engine with no base URL is **refused**, naming itself and what to set.
- Network errors name the engine and **its own endpoint** — *"Could not reach
  Nemotron at http://localhost:11434/v1"* — rather than a default someone else
  configured.
- **Health checks stored engines before you send anything**: no base URL, a
  URL without a scheme, no model id, a cloud engine with no key.

And an entry the loader drops — missing a name or a model — used to vanish
without trace, so an engine you saved simply wasn't in the list and nothing
said why. It's reported now.

> None of this needed deducing from a wrong error message, which is what it
> cost to find.


### 112. Agent Jo Jobs

The job search is its own application now, on its own port, with its own
window.

It had become a different product living inside another one. Someone running a
job search doesn't want a code map, a 3D lab and an MCP panel in the way; and
someone using the agent for work doesn't want a job hunt in their sidebar.

**Separated, not duplicated.** All 49 routes moved rather than being copied,
and every module — `jobscout`, `boards`, `cv`, `portal`, `outcomes` — is
imported from where it already lived. One fabrication check, one auto-apply
engine, one definition of "held". Two copies would drift, and the half that
decides whether an application goes out is not a half to let drift. Shared
data, shared engines, shared audit trail; different window.

**The interface follows the work, not a template.** The pipeline is one
continuous object across the top rather than six equal tiles — equal tiles
would claim all six stages matter equally, when the blocked one is the only
one you can act on, and it's the only one that takes the accent colour. Roles
are a dense scannable list rather than a card grid: you read forty looking for
two. Six views — Pipeline, Roles, Drafts, Results, Sources, You.

> **Four bugs caught by testing the window against the running server rather
> than against my assumptions.** `stages` is a dict of counts with
> `blocked_at` naming the stuck one, not a list of objects. `/api/jobs/held`
> doesn't exist — held drafts come from `/claims`. Sources are read from
> `/search/config`; `POST /sources` only adds one. And the scan is `/search`,
> not `/scan`. Every one would have drawn an empty view that looked like
> "nothing found yet" — the worst kind of wrong, because it looks like an
> answer.

The main app keeps one command-palette entry that opens the Jobs window. Four
test harnesses that drove the old panel were replaced by one that drives the
new app against real payload shapes.


### 113. Upgrading

`UPGRADING.md`, because "replace the folder" needs to be shown to be safe
rather than asserted.

**Nothing of yours is in the app folder.** Conversations, memories, engines,
settings, roles, the audit trail and backups all live in `.local_agent` beside
your home directory. Upgrading replaces the code next to it.

Verified rather than claimed: a fixture with an engine, a profile and a role,
a second copy of the code pointed at the same data, then the original folder
deleted outright — all three survived. Four checks now assert the property
that makes this true, so a future change that starts writing user data beside
the code fails the suite rather than quietly breaking upgrades.

The two things that actually break an upgrade are called out: unzipping into
`%TEMP%`, which Windows empties without warning, and a folder named
`AgentJo (1)` — the bracket a browser adds to a second download breaks batch
files.


### 114. Agent Jo Jobs, properly this time

Feedback on the first cut: it looked mid, and functions had gone missing. Both
true, and the second was worse than the first.

**It wired 12 of 47 endpoints.** Scoring, drafting, the ATS check, CV
tailoring, interview prep, portal applications, confirming and dismissing
claims, every auto-apply control, alerts, the archive — gone. A redesign that
removes what people used is a regression wearing new clothes. **46 of 47 are
reachable now**; the one without a button is internal plumbing the alerts
folder calls. A check fails the build if a user-facing endpoint loses its UI
again.

**Looked at, not imagined.** The first version was built blind. This one was
rendered in a real browser, screenshotted with realistic data, and changed on
what the screenshots showed: serif headlines over a sans interface with mono
numbers; a left rail with live counts; the pipeline as a track with the stuck
stage lit; a three-step checklist for a first run instead of a row of zeros;
roles as master and detail, set like an article, with actions grouped by
intent; and the highlighted action being the next step for that role rather
than always the first button.

> **Four bugs the screenshots caught.** Every read was failing: the request
> helper attached a JSON body to GETs, which the browser rejects before
> sending — so the pipeline showed zeros under a headline counting eight
> roles. "0 selected" showed with nothing selected, because a class setting
> `display` outranks the `hidden` attribute. Ticking a role gave no visible
> sign. And held drafts showed no reason, because reasons arrive as strings
> and were read as objects.
>
> Removed on inspection: per-stage conversion rates. "Sent — 100% of held"
> was computed against a stage that isn't in the funnel. A number that looks
> like analysis and isn't is worse than none.


### 115. Agent Jo Jobs as its own project

Agent Jo Jobs now exists as a **separate repository**, `agent-jo-jobs`, that
installs, runs and tests with no copy of Agent Jo on the machine.

**It needed one import removed to be possible.** The Jobs server imported
`agent.main` for two marker values — and `agent.main` pulls in the tool layer
and everything behind it: **51 of 63 modules, 25,700 lines**, including the
Blender lab, neural 3D and the fine-tuner. Defined locally instead, it needs
**17 modules and 11 load at runtime**, every one of them about jobs.

**The honest cost is two copies.** Being separate means those 17 modules live
in both repos, and a fix to the fabrication check here doesn't reach the Jobs
app by itself. So that is made visible rather than left to be discovered:
`tools/sync_jobs_app.py --check` reports every shared module that has drifted,
by hash, and without `--check` copies them across. The Jobs repo carries
`VENDORED.json` naming the build it came from. The check was proven against a
real change before being trusted.

**Found on the first fresh install: the default profile was the author's.**
"Data Engineer, BI Consultant, AI Engineer, remote contract, South Africa"
shipped as everyone's starting targets — harmless in one person's app, wrong
the moment a stranger installs it, and the reason onboarding claimed step one
was already done. The defaults are empty now; an existing saved profile
overrides them, so nothing changes for a current install. The window also had
three definitions of "profile ready" that disagreed; it uses the server's.

It carries the Agent Jo portrait as its icon — in the rail, the browser tab,
and the desktop shortcut the installer creates — and its own 24 checks.


### 116. Agent Jo Jobs — the reference look

Restyled to a reference supplied by the user: cool charcoal behind a soft
blurred backdrop, translucent glass cards, a mint accent with a glow on the
primary action, a bold sans for headlines, icons on every navigation item,
three separate onboarding cards each carrying an illustration, the pipeline
as ring counters, pill buttons, and the engine picker pinned to the bottom of
the sidebar.

Compared screenshot against reference at the same 1024×640 size and adjusted
until they matched: a narrower sidebar and tighter padding so step titles stop
wrapping, and type a step smaller. No functionality changed — all 46
user-facing endpoints are still reachable.

The window is now identical in both repositories, so the sync tool carries it
across along with the shared modules.


### 117. Chrome buttons

Lit buttons in Agent Jo Jobs are polished metal now: a mint-tinted chrome
gradient with a hard specular band, and a highlight that sweeps across on
hover. **While a button is working the sweep runs continuously** and the glow
breathes — the same language as a thinking indicator, so a busy control reads
as the agent at work rather than a frozen button. Every busy button gets it,
not only the primary ones.

The class is added when work starts and removed in `finally`, so a failed
request can't leave a button shimmering forever. People who've asked their
operating system for less motion get the chrome without the sweep.

> Caught at 2× zoom: `overflow:hidden`, which the sheen needs, let a crowded
> row squeeze the primary button until its label read "Run a full cyc".
> Buttons never shrink now; the helper text beside them gives way. And a
> white sweep on pale mint barely registered, so the primary's band is
> brighter and blends as added light.


### 118. Times New Roman, and the reference matched to the pixel

Every heading in Agent Jo Jobs — page titles, card and step titles, role
titles, the app name — is set in Times New Roman; the working interface stays
in the sans. Times ships with Windows and macOS, so nothing is downloaded.
Each heading goes up a touch, because Times sits smaller than the sans at the
same size.

Then compacted against the supplied reference at 1024×640 until the bottom
cards sat fully in view, as they do there (577px of 640). The sidebar name and
the engine box stopped wrapping; the box's side padding gave way so its text
fits in full rather than being clipped — checked with the sandbox's fallback
font, which is wider than Segoe UI, so it has room to spare on Windows. Step
card buttons are rounded rectangles, as in the reference; the rest stay pills.


### 119. Search shows what you have, and Track actually saves

Reported: search results didn't say which roles were already tracked, and
untracked ones couldn't be tracked.

**Tracking from search had never saved anything.** The window sent
`{"roles": [...]}`; the route's field is `items`. The unknown key was dropped,
the call answered `ok` with nothing added, and the window then showed every
role as tracked. The request now refuses unknown fields, so a wrong name fails
loudly instead of succeeding at nothing.

**The server already knew which results were tracked** — it computed
`already_tracked` for every keyword search. The window just never read it.
Reading a single posting by URL didn't compute it at all; both paths now go
through one `mark_tracked`, which also flags roles **you removed earlier**.
Those mattered most: `add_roles` skips them silently, so their Track button
did nothing.

Each result now shows its state — **New**, **Tracked** or **Removed earlier**
— with the matching action: Track, Open, or Restore. A summary counts them
before you touch anything, chips filter by state, and **Track N new** tracks
only what you don't already have. Rows update from the server's per-role
answer, never from an assumption that it worked.

Verified in a real browser against the real write path: three new, two
tracked and one removed became six tracked, and the store held six roles.


### 120. Glass

A new theme, restyled to a supplied reference: deep teal-black, translucent
panels, one teal accent. It's what "System" resolves to in dark mode; anyone on
the previous dark default is moved to it once, and a choice made after that is
left alone. The older themes stay selectable in Settings.

- Each sidebar group — Core, Automation, Extend, Safety — is its own glass
  panel with the chevron on the right.
- The top-bar controls sit in a centred pill, the engine chip on its own at the
  right. The four switches are still there, compacted to icon and track with
  their names as tooltips; hiding a label doesn't hide what a switch does.
- Tiles run six across, compact, with teal charts. The size comes from a CSS
  variable the existing layout reads, so rows still fill completely.
- **Task Feed**: upcoming schedule runs and recent tasks, from real data, beside
  the greeting. A container query puts it under the suggestions when the chat
  area is too narrow to hold it without covering anything.

> **Found while restyling, and mine: every load has shown "Could not reach the
> server" since build 105.** An edit meant for another function put
> `state.startEngine = d.start_engine` into the start-up block, where the
> variable is `meta`. It threw on every load; the catch blamed the server; and
> everything after it was silently skipped — the microphone button, the
> no-engine warning, and the start-engine fix the line was meant to deliver.
> Nothing ever executed start-up, so nothing noticed. A check now does.
>
> Three existing checks caught the restyle on the way through: the tile
> harness has no `getComputedStyle` and the dashboard harness no
> `requestAnimationFrame` (both now fall back gracefully, as an old browser
> should); and the Glass rules had been appended after `[hidden] { display:
> none !important; }`, which must stay last.


### 121. Motion

Both apps move between states now instead of snapping, on one easing curve so
they feel like one system.

**Panels** fade their backdrop in and the card rises and settles. **Closing**
was the careful part: the app hides panels instantly in 25 places, and its
logic relies on "closed" meaning closed at that moment. So closing is never
delayed — a copy of the panel, stripped of every id so nothing can find it,
fades and sinks on top while the real one is already gone. One observer
handles all 25; none of them changed.

**Conversations** ease in when you switch — only then. The thread is rebuilt
on every update, so animating each message would have replayed the whole
conversation's entrance on every reply. **Sidebar groups** fold and unfold,
and a folded group leaves the tab order. The welcome cards arrive one after
another.

In **Agent Jo Jobs**, each view eases up as it opens, and the role or draft
on the right settles in when you pick another one; onboarding steps and the
pipeline rings arrive in sequence.

Anyone whose system asks for reduced motion gets none of it. Verified in a
browser: the panel is hidden at the instant it's closed, one id-less copy
fades, and none remains after 580 ms.


### 122. Glass panels without hard edges

Reported with a screenshot of Health: square boxes around the title, the
summary line and the list, square status bars, and a sideways scrollbar.

**Most of it was one rule of mine.** Glass styled `.modal > div` — meant for
the panel card, it gave every direct child of every panel its own square frame.
It targets the card alone now, and all eight panels were scanned in a browser
afterwards for any bordered square box or sideways scrolling: none.

The summary line is a soft pill; the list is one rounded surface; rows are
rounded tiles whose status bar is an inset shadow, which bends with the corner
where a border can't; section headings are separated by space rather than a
rule; long lines wrap instead of scrolling the list sideways; scrollbars are
thin and sit inside the curve.

> **And a colour that lied.** In Health, warnings use the "gold" tier — which
> Glass had made the same teal as a pass. A warning drawn in the "everything's
> fine" colour contradicts its own text. Health now maps its tiers explicitly:
> teal ok, amber warning, red failing. Elsewhere gold stays amber, because
> there the tiers rank quality, and a red bronze would read as a failure.


### 123. A task feed you can move and put away

Reported with a screenshot: the Task Feed sat on the greeting. Its placement
rule assumed the welcome cards were narrower than they are on a wide window.
Rather than a better guess, it's yours to place now:

- **Drag it** anywhere by the header. It can't be dragged off-screen, and it
  comes back inside the window if the window shrinks.
- **Minimise it** to a pill showing the count; a click on the pill opens it.
  The click that ends a drag doesn't count as one.
- **Double-click the header** to send it back to its corner.
- Position and minimised state are **remembered** across reloads, and a narrow
  window starts it minimised so it can't cover anything by default.

It lives on the page now rather than inside the chat area, whose own
animations and layout would have moved a floating panel with them. It still
shows only on the welcome screen — verified in a browser: shown there,
hidden when a conversation opens, back for a new one.


### 124. Agent Jo Jobs, restyled to the supplied screens

Four views rebuilt to a supplied design — **What You Can Claim**, **Sources
Manager**, **Auto-apply Rules & Applications** and **Find Roles Search** — with
large serif titles, glass surfaces and a green accent, plus an auto-apply switch
and a profile completeness bar in the sidebar.

**The design showed things the app didn't know; it shows them only where
something real stands behind them.**

- *Verified / Pending* on sources needed a source of truth. Every search now
  records what each source returned, so a board is **Verified** because it
  returned roles on its last check, **Failing** if it errored or came back empty
  (hover for the reason — "403 Forbidden"), **Pending** if never checked, and
  "Last checked" is when that really happened.
- *"98% Match"* was on every card in the design. Search results aren't scored
  until you score them, so a tracked role shows its real score and a new one
  says **Not scored** — never a number nobody computed.
- The profile percentage is the share of the fields a draft relies on that are
  filled in, with the missing ones named on hover.
- The sidebar switch changes only *enabled*, and reads **Test** rather than
  **On** while rehearsal means nothing is actually sent.

The design's garbled placeholder text ("Symbuht Synapse", "draft bot mever
cend") is replaced with the app's real wording throughout.

> Caught while matching: an older `.nav span { flex: 1 }` also caught the
> switch, which stretched and cut "Auto-apply" to "Auto…" even with the rail
> widened. And the Jobs harness found that a missing piece of the switch would
> have thrown in the function that refreshes the sidebar on nearly every view.


### 125. No brackets on the claim rows

Reported as messy: each row in Verifiable Claims Coverage had a green bar on
its left edge — an inset shadow, which on a row this short bends round both
rounded corners and reads as a bracket. The rows are a soft surface now, and a
check keeps the bar from coming back.


### 126. Auto-apply: why it never sent

Reported: auto-apply has never sent an application on its own — the app's
whole point.

**The sending code was never broken.** Proven end to end with the mail sender
stubbed: with everything set, it calls the sender for real, the role moves to
*applied*, and the follow-up clock starts. What was wrong is that sending is
guarded by half a dozen separate conditions, and when one was off **nothing
said so** — the run reported "0 sent", which reads as a broken feature rather
than a setting.

**`auto_readiness()` now states every condition and which one isn't met**, each
with what to do: auto-apply off, rehearsal on, no email account, observe mode,
a thin profile, no role clearing the gates (with the top reasons counted), the
daily cap used up. It shows at the top of the Auto-apply view, and after any
run that sent nothing.

**Four real faults found on the way:**

- **The standalone Jobs app ran no scheduler at all.** The daily job only ever
  happened if the main Agent Jo app was open at the time. The Jobs app runs it
  now, and an atomic claim in the shared store means the two apps can never run
  the same job twice — proven with two stores over one database.
- **A draft flagged for rewriting could be sent.** The app marks a draft
  `needs_redraft` when it no longer matches your profile; the gate never
  checked, so an unattended run would have sent exactly that draft.
- **The preview and the run disagreed.** The preview classified by bucket while
  the run decides by the gate, so it could say "nothing clears the gates" about
  a role the run would have sent. Both ask the gate now.
- **Two results were read from keys that were never returned** — the run's
  `summary` and the preview's `sentence`. Every run reported "Ran once."
  whatever it did, and the preview printed "Auto-apply is off." while it was
  on. Both are returned by the server now, and a run reports what it sent,
  what it held and why.

The daily schedule's own description said *"Do not send anything"*, which was
never what it does — it runs the full cycle and sends whatever clears the
gates. It now says so, and that nothing goes while rehearsal is on.

> **To actually send:** turn auto-apply on, untick **Rehearsal**, and set up an
> email account in Agent Jo's Outreach panel. The readiness panel lists exactly
> what's still missing.


### 127. Portal applications from a chat

Reported: applying via the portal failed with *"It looks like you are using
Playwright Sync API inside the asyncio loop."*

Reproduced exactly. The synchronous browser API refuses to start on a thread
that has a running event loop. An HTTP route is safe — those run on a worker
thread — but a **tool call inside a chat turn runs on the loop**, so every
portal application driven from a conversation failed. The message reads like a
coding fault in the app, which is what makes it useless: it says nothing about
where it was called from.

The browser is now started off the loop whichever way it's reached: if a loop
is running on the calling thread, the work goes to one without. Ordinary
callers are untouched.

> **And the failures now say what to do.** A missing browser printed a wall of
> Playwright output with the answer buried in it; it now reads: *"Playwright is
> installed but its browser isn't. In the app folder run: .venv\\Scripts\\python
> -m playwright install chromium"*. Same for a missing driver, a timeout, and
> an unreachable advert.


### 128. Setup installs the browser

Asked whether the extra packages can be installed at first setup. They can, and
the browser now is: portal applications are a headline feature, and setup only
ever printed the two commands to run — leaving the feature one undocumented
step from working on every fresh machine.

Both installers now install Playwright and download Chromium, say it's about
150 MB, and can be declined (`--skip-extras`, or answering no; an unattended
run never waits more than 30 seconds for an answer). A blocked download says
what it means — "the download server wasn't reachable… set HTTPS_PROXY and
run: …" — instead of printing a JavaScript stack trace, and setup still
finishes.

> **The old readiness test could pass with no browser on disk.**
> `playwright install --dry-run` exits 0 whether or not the browser exists, so
> setup reported "Playwright ready" on machines that had none — and portal
> applications then failed at the moment they were used. Readiness is now
> whether the executable is actually there, and Health reports it too.

Ollama is still offered rather than installed: it's a separate application with
its own installer, and pulling a model is a choice about disk and bandwidth
that shouldn't be made for you.


### 129. An engine pointing at the wrong kind of endpoint

Reported: connecting directly to the DeepSeek API answered *"`deepseek-v4-pro`
isn't installed. This machine has: command-r:latest, ..."* — which is the
**local runner's** reply. The call never reached DeepSeek.

Routing by engine name was correct; the engine itself pointed at
`localhost:11434`. An engine with a cloud model id and a local base URL saved
without a word of warning, so every call went to Ollama — and the message named
the model rather than the fact that **the request never left the machine**.
That is what made it hard to see.

- **Saving says so.** A cloud model id against a local runner, or an Ollama
  tag against a cloud API, is named at save and at edit, with the provider's
  real base URL given (DeepSeek's is `https://api.deepseek.com/v1`). It still
  saves — you may know better than the check.
- **Health lists it** under Engine setup.
- **Test connection** explains it before you ever send a message.
- **The old message now names the endpoint**: "isn't installed on the runner at
  http://localhost:11434/v1 … this engine points at that runner, so the request
  never left this machine — if `deepseek-v4-pro` is a cloud model, this
  engine's base URL is wrong."

**To fix yours:** open Engines, edit the DeepSeek one, and set the base URL to
`https://api.deepseek.com/v1` with a model the API knows, such as
`deepseek-chat`. Press Test connection — it now reaches the provider and says
what came back.


### 130. Engines in Agent Jo Jobs

Agent Jo Jobs could only pick from engines defined in Agent Jo, which is what
made the DeepSeek base-URL mistake so awkward: the app reporting the fault
wasn't the app that could fix it.

It has an **Engines** view now — add, edit, test and remove, with the engine
picker updating as you go. It writes to the same store Agent Jo reads, using
the same module rather than a second copy, so an engine added in either app
appears in both.

**Presets spell out the real base URLs** — DeepSeek, OpenAI, Groq, Together,
OpenRouter, Ollama, LM Studio — because a base URL typed from memory is exactly
how a cloud engine ends up pointing at a local runner. A mismatch is still
named when you save, and **Test connection** contacts the provider and reports
what came back.

The rest of the app was brought up to the standard of the redesigned screens:
the same corner radii, serif headings and spacing across Overview, Roles,
Drafts, Results and Archive.

> **Found by the test that checks every call reaches a real route:** the Jobs
> app's own `/api/engines/test` had been lost, and `/api/engines/{name}` was
> matching that path instead — a POST answered 405. Whichever is declared
> first wins, so the test route is declared first. And `agent/engines.py`
> wasn't in the standalone app's sync list, so it failed to start outright;
> the list is now what the app imports.


### 131. A stale process must not look like a failed fix

Reported again, with the identical message — and the wording was the giveaway:
that exact sentence was replaced two builds ago. The process was running code
from before the update.

A running app holds whatever was on disk when it started, so an update that
isn't restarted keeps answering with the old behaviour — which reads as "the
fix didn't work" rather than "this isn't the fix". Health now compares the
build the process is running with the build in the files and **fails** when
they differ: "Running build X, but the files on disk are Y — you updated the
app but it wasn't restarted."


### 132. Picking an engine now reaches that engine

Two screenshots settled it: a DeepSeek engine whose **Test connection said
"Connected and answered in 1.1s"**, and every chat answering *"Could not reach
Command-r:latest at http://localhost:11434/v1"* — with the DeepSeek engine's
**name** shown in the model column.

**`OpenAIBrain` was the only brain that never asked the dispatcher.** Anthropic,
Ollama and Hybrid all did. So whenever the current engine was an
OpenAI-compatible one — and an Ollama server on its OpenAI port is exactly that
— a per-call engine choice was handed to *that* endpoint as a model id. The
chosen engine was never called. The engine was always fine; it simply wasn't
being used.

Reproduced with the reported engine names, fixed, and checked both ways: the
choice now reaches the chosen engine, and an engine's own model still goes to
its own endpoint.

> **And a loop the fix would have introduced.** One of the reported engines is
> named after its own model ("command-r:latest" pointing at command-r:latest).
> Dispatching on that name sent it to itself for ever — a hang, not an error.
> A brain asked to dispatch to itself now handles the call directly. A check
> holds both, and another asserts every brain consults the dispatcher, so a
> fifth one can't quietly skip it.


### 133. Agent Jo Jobs — the window says one thing, once

Rendered every view with realistic data and fixed what the screenshots showed.

**The overview used a third of the screen and left the rest blank.** It now
carries three panels, built only from what the app already knows:

- **Needs you** — held drafts, roles scored but not drafted, roles not scored,
  portal-only roles, a profile too thin to draft from. Each row opens the view
  that fixes it.
- **Where they came from** — tracked roles by source, and any source that
  returned nothing on its last check.
- **Lately** — real events from the roles themselves: applied, drafted, held,
  replied, with when.

An empty panel says so rather than showing rows that look like activity.

**And the same role was described two ways.** The list showed the raw stage
("drafted") beside a detail showing the real state ("held"), and the
highlighted action told a held draft to mark itself applied. All three read
the state now. A role's page also shows **why it scored**, **what the draft
says** — with the exact claim that held it — and its **history**, instead of
empty space below the buttons.

Cards size to their content: a grid stretches its children by default, which
left half-empty boxes above a page that still had room.

> Worth recording: the pipeline counts looked wrong while testing, and weren't.
> The seed data set a draft without the stage that drafting sets. The app was
> right; the fixture was.


### 134. Applying beyond email

Reported: auto-apply "only works for email application", and the portal
rehearsal left you to retype everything. Most adverts are portals, so
auto-apply that only emails is auto-apply that mostly does nothing.

**The engine answers the form.** Portal forms ask what no field table can
cover — "why this role", "years with Power BI", "notice period". Those were
left blank and the application stopped there. The engine now answers them
**from your profile and nothing else**, and every answer goes through the same
claims check a drafted email does:

- supported by your profile → typed in;
- claims more than your profile → **held**, shown with the reason, never typed;
- not answerable → left blank, and it says so.

**And it hands the work over.** Automation gets some way into most forms and
stops — an upload it can't reach, a question inside a widget. Retyping what the
app already worked out is where people give up, so a rehearsal now produces a
**paste pack**: every field and answer as plain text, with a Copy button per
answer and *Copy everything*, beside a button that opens the form.

**Auto-apply covers portal-only roles.** A new setting decides what happens to
adverts with no email address: **prepare** (default — fill the form, answer
what it can, leave it to you), **submit**, or **off**. A run reports them
separately: "2 portal form(s) filled for you to finish", each with the
questions left for you.

> Submitting on its own stays **off** unless you ask for it. A form filled by a
> machine and sent without being read is the one thing worse than a form not
> filled at all.
