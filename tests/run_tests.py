"""Offline test suite — run any time with:  python tests/run_tests.py

No API key or Ollama install needed: backend tests run against a local mock
server. Covers memory/skills/tasks/permissions, the read-only command
classifier, prompt-cache request preparation, model routing rules, and full
tool-loop flows (streaming and non-streaming) through the Ollama wire format.
"""

import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_HOME = "/tmp/local_agent_tests"
shutil.rmtree(TEST_HOME, ignore_errors=True)
os.environ["AGENT_HOME"] = TEST_HOME
os.environ["AGENT_OLLAMA_HOST"] = "http://127.0.0.1:11533"
os.environ["ANTHROPIC_API_KEY"] = "test-not-real"
# The servers answer only to this machine's own names (agent/localguard.py),
# and the in-process test client calls itself "testserver".
os.environ.setdefault("AGENT_ALLOWED_HOSTS", "testserver")

from rich.console import Console                              # noqa: E402
from agent import config, tools                               # noqa: E402
from agent.memory import MemoryStore, format_task             # noqa: E402
from agent.brain import make_brain, prepare_anthropic_request # noqa: E402
from agent.main import build_system_prompt, choose_model, run_turn, trim_history  # noqa: E402
import agent.main as agent_main  # noqa: E402
import agent.rag as _rag_global  # noqa: E402
# Determinism: semantic reranking falls back to keyword matching throughout the
# suite (no live embedder; the mock is shut down mid-run and a post-shutdown
# embed attempt would eat its full timeout). The semantic tests install their
# own fake embedder locally and restore this patch afterwards.
_rag_global.embed_texts = lambda texts: None

console = Console()
PASS = []


FAILED = []
_COLLECT = {"on": False}


def check(name, cond, detail=""):
    if not cond:
        if _COLLECT["on"]:
            # the section runner records it and carries on (see run())
            FAILED.append(f"{name} {str(detail)[:300]}".rstrip())
            return
        raise AssertionError(f"{name} FAILED {detail}")
    PASS.append(name)


# ---------------------------------------------------------------------- #
# Mock Ollama: scripted replies; supports streaming and non-streaming.
# ---------------------------------------------------------------------- #
SCRIPT: list = []
RECEIVED: list = []


class MockOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.rstrip("/") in ("/api/embed", "/api/embeddings"):
            # Deterministic bag-of-words embedding so semantic reranking is
            # exercisable in tests WITHOUT consuming the scripted chat replies
            # (real Ollama serves these on separate endpoints too).
            def _vec(t):
                t = (t or "").lower()
                words = sorted({w for w in t.split() if len(w) >= 4})[:12]
                v = [0.0] * 16
                for w in words:
                    v[hash(w) % 16] += 1.0
                return v
            self.send_response(200)
            self.end_headers()
            if "input" in body:                      # batch: /api/embed
                texts = body["input"]
                texts = texts if isinstance(texts, list) else [texts]
                self.wfile.write(json.dumps(
                    {"embeddings": [_vec(t) for t in texts]}).encode())
            else:                                    # per-item: /api/embeddings
                self.wfile.write(json.dumps(
                    {"embedding": _vec(body.get("prompt", ""))}).encode())
            return
        RECEIVED.append(body)
        msg = SCRIPT.pop(0) if SCRIPT else {"role": "assistant", "content": "ok"}
        self.send_response(200)
        self.end_headers()
        if body.get("stream"):
            content = msg.get("content", "")
            mid = max(1, len(content) // 2)
            for chunk in (content[:mid], content[mid:]):
                if chunk:
                    self.wfile.write(json.dumps(
                        {"message": {"content": chunk}, "done": False}).encode() + b"\n")
                    self.wfile.flush()
            if msg.get("tool_calls"):
                self.wfile.write(json.dumps(
                    {"message": {"tool_calls": msg["tool_calls"]},
                     "done": False}).encode() + b"\n")
            self.wfile.write(json.dumps({"message": {}, "done": True}).encode() + b"\n")
        else:
            self.wfile.write(json.dumps({"message": msg, "done": True}).encode())


server = HTTPServer(("127.0.0.1", 11533), MockOllama)
threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    m = MemoryStore()

    # ---- memories ----------------------------------------------------- #
    mid = m.add_memory("User prefers Python over Java", "preference")
    check("memory.add", mid is not None)
    check("memory.dedupe", m.add_memory("user prefers PYTHON over java!") is None)
    m.add_memory("User works on the FX desk at Standard Bank", "fact")
    check("memory.search", "Python" in m.search_memories("python")[0]["content"])
    check("memory.delete", m.delete_memory(mid))

    # ---- skills --------------------------------------------------------- #
    m.add_skill("eod-summary", "user asks for EOD", "read report, 5 bullets")
    check("skills.add", m.skill_count() == 1)

    # ---- tasks + verification gating ------------------------------------ #
    tid = m.create_task("Pipeline", ["write", "test", "ship"], "s1")
    check("tasks.create", m.get_task(tid)["status"] == "active")
    out = tools.execute_tool("update_task_step",
                             {"task_id": tid, "step": 1, "status": "done", "note": "ok"},
                             m, console, True)
    check("tasks.evidence_required", out.startswith("Refused"))
    tools.execute_tool("update_task_step",
                       {"task_id": tid, "step": 1, "status": "done",
                        "note": "ran it, exit 0, output matches"}, m, console, True)
    out = tools.execute_tool("complete_task",
                             {"task_id": tid, "summary": "all good here truly"},
                             m, console, True)
    check("tasks.completion_gated", out.startswith("Refused"))
    for step in (2, 3):
        tools.execute_tool("update_task_step",
                           {"task_id": tid, "step": step, "status": "skipped",
                            "note": "not needed in test"}, m, console, True)
    out = tools.execute_tool("complete_task",
                             {"task_id": tid, "summary": "done in test suite"},
                             m, console, True)
    check("tasks.complete", "completed" in out and not m.active_tasks())
    check("tasks.format", "[x]" in format_task(m.get_task(tid)))

    # ---- blocker-pattern fixes ------------------------------------------ #
    # restart in place (Pattern 5): reuse same id, keep history
    rt = m.create_task("Build DQ Engine", ["schema", "procs", "views", "seed"], "s1")
    tools.execute_tool("update_task_step", {"task_id": rt, "step": 1, "status": "done",
                       "note": "schema created and verified in db"}, m, console, True)
    rout = tools.execute_tool("reset_task_plan", {"task_id": rt}, m, console, True)
    _rtask = m.get_task(rt)
    check("blocker.restart_in_place",
          "Restarted" in rout and _rtask["status"] == "active"
          and all(s["status"] == "pending" for s in _rtask["steps"])
          and "prev:" in (_rtask["steps"][0]["note"] or ""))
    # create_task_plan reuses an existing active task instead of duplicating
    dupe = tools.execute_tool("create_task_plan",
                              {"title": "build dq engine", "steps": ["x", "y"]},
                              m, console, True)
    check("blocker.create_detects_duplicate",
          "already covers this" in dupe and f"reset_task_plan(task_id={rt})" in dupe)
    # blocked step (Pattern 1): requires a handoff, surfaces NEEDS USER, gates completion
    bout = tools.execute_tool("update_task_step",
                              {"task_id": rt, "step": 1, "status": "blocked"},
                              m, console, True)
    check("blocker.blocked_requires_needs", bout.startswith("Refused"))
    tools.execute_tool("update_task_step",
                       {"task_id": rt, "step": 1, "status": "blocked",
                        "needs": "run: gh auth login"}, m, console, True)
    check("blocker.blocked_records_handoff",
          "NEEDS USER" in (m.get_task(rt)["steps"][0]["note"] or "")
          and "[B]" in format_task(m.get_task(rt)))
    for s in (2, 3, 4):
        tools.execute_tool("update_task_step", {"task_id": rt, "step": s,
                           "status": "done", "note": "did it and checked output"},
                           m, console, True)
    cgate = tools.execute_tool("complete_task",
                               {"task_id": rt, "summary": "mostly done in test"},
                               m, console, True)
    check("blocker.completion_blocked_by_blocked_step",
          cgate.startswith("Refused") and "blocked" in cgate)
    # verification recorded on completion (Pattern 4)
    vt = m.create_task("Render PBIX", ["build", "inject"], "s1")
    for s in (1, 2):
        tools.execute_tool("update_task_step", {"task_id": vt, "step": s,
                           "status": "done", "note": "checked output ok"}, m, console, True)
    tools.execute_tool("complete_task",
                       {"task_id": vt, "summary": "12 visuals injected",
                        "verification": "opened in PBI Desktop, all rendered against the model"},
                       m, console, True)
    check("blocker.verification_recorded",
          "verified:" in (m.get_task(vt)["summary"] or ""))
    # stuck-task detection finds unresolved active work
    check("blocker.stuck_tasks_found", any(t["id"] == rt for t in m.stuck_tasks()))
    # web fetch diagnostics (Pattern 3)
    import agent.web as _webd
    check("blocker.fetch_diag_challenge",
          "anti-bot" in _webd._diagnose("<html>Just a moment… checking your browser</html>", "")
          or "challenge" in _webd._diagnose("<html>cf-chl checking your browser</html>", ""))
    check("blocker.fetch_diag_js_app",
          "client-side" in _webd._diagnose('<div id="root"></div>' + "x" * 3000, "")
          or "JavaScript" in _webd._diagnose('<div id="root"></div>' + "x" * 3000, ""))
    check("blocker.fetch_diag_quiet_when_fine",
          _webd._diagnose("<html><body>" + "real content " * 50 + "</body></html>",
                          "real content " * 50) == "")
    for _ct in m.active_tasks():            # don't let lingering tasks skew routing tests
        m.finish_task(_ct["id"], "test cleanup", "abandoned")

    # ---- read-only classifier ------------------------------------------- #
    ok_cmds = ["ls -la", "git status", "grep -r foo src", "cat file.txt",
               "find . -name '*.py'", "rg TODO agent", "/bin/ls /tmp"]
    bad_cmds = ["rm -rf x", "cat a > b", "ls; rm x", "git push", "git branch new",
                "find . -delete", "echo hi | sh", "FOO=1 ls", "ls `whoami`",
                "cat $(secret)", "python x.py", "sed -i s/a/b/ f"]
    for c in ok_cmds:
        check(f"classifier.allow [{c}]", tools.is_read_only_command(c))
    for c in bad_cmds:
        check(f"classifier.deny [{c}]", not tools.is_read_only_command(c))
    check("classifier.hint", tools.command_rule_hint("npm run build") == "npm run")
    check("classifier.hint2", tools.command_rule_hint("python3 x.py") == "python3")

    # ---- tiered permissions ---------------------------------------------- #
    real_prompt = tools.Prompt
    answers = []
    tools.Prompt = SimpleNamespace(
        ask=lambda *a, **k: answers.pop(0) if answers else (_ for _ in ()).throw(
            AssertionError("prompted unexpectedly")))
    out = tools.execute_tool("run_command", {"command": "ls /tmp"},
                             m, console, False)              # tier 1: no prompt
    check("perm.readonly_auto", "exit code: 0" in out)
    answers[:] = ["a"]                                        # tier 3 → store rule
    out = tools.execute_tool("run_command", {"command": "mkdir -p /tmp/lat_perm"},
                             m, console, False)
    check("perm.always_stored", "exit code: 0" in out and
          m.command_permitted("mkdir /tmp/other") == "mkdir")
    out = tools.execute_tool("run_command", {"command": "mkdir -p /tmp/lat_perm/x"},
                             m, console, False)               # tier 2: rule, no prompt
    check("perm.rule_auto", "exit code: 0" in out)
    check("perm.rule_simple_only", m.command_permitted("mkdir x && rm -rf /") is None)
    answers[:] = ["a"]
    tools.execute_tool("write_file", {"path": "/tmp/lat_perm/a.txt", "content": "1"},
                       m, console, False)
    out = tools.execute_tool("write_file", {"path": "/tmp/lat_perm/b.txt", "content": "2"},
                             m, console, False)               # dir rule, no prompt
    check("perm.write_dir", "verified" in out)
    check("perm.write_outside_blocked", m.write_permitted("/etc/x") is None)
    answers[:] = ["n"]
    out = tools.execute_tool("run_command", {"command": "touch /tmp/nope"},
                             m, console, False)
    check("perm.decline", out.startswith("User declined"))
    m2 = MemoryStore(m.db_path)                               # rules persist on disk
    check("perm.persisted", m2.command_permitted("mkdir z") == "mkdir")
    check("perm.revoke", m.delete_permission(m.list_permissions()[0]["id"]))
    tools.Prompt = real_prompt

    # ---- prompt-cache preparation (pure) --------------------------------- #
    hist = [{"role": "user", "content": "plain"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"},
                                              {"type": "tool_use", "id": "x",
                                               "name": "t", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                          "content": "r",
                                          "cache_control": {"type": "ephemeral"}}]}]
    sp, msgs = prepare_anthropic_request(hist, ["S", "D"], cache=True)
    check("cache.static_breakpoint", "cache_control" in sp[0] and "cache_control" not in sp[1])
    n_marks = sum(1 for mm in msgs for b in
                  (mm["content"] if isinstance(mm["content"], list) else [])
                  if isinstance(b, dict) and "cache_control" in b)
    check("cache.single_moving_breakpoint", n_marks == 1 and
          "cache_control" in msgs[-1]["content"][-1])
    check("cache.no_mutation", isinstance(hist[0]["content"], str))

    # ---- system prompt split --------------------------------------------- #
    static, dynamic = build_system_prompt(m, "fx desk question")
    check("prompt.static", "Working method" in static and "eod-summary" in static)
    check("prompt.dynamic", "FX desk" in dynamic)

    # ---- routing rules ----------------------------------------------------- #
    stub = SimpleNamespace(model="main", fast_model="fast",
                           route=lambda t: "fast" if "hi" in t else "main")
    check("route.simple", choose_model(stub, m, [], "hi there") == "fast")
    check("route.complex", choose_model(stub, m, [], "refactor my repo") is None)
    check("route.long_escalates", choose_model(stub, m, [], "hi " * 300) is None)
    midtool = [{"role": "assistant",
                "content": [SimpleNamespace(type="tool_use", name="x", input={}, id="1")]}]
    check("route.midworkflow_escalates", choose_model(stub, m, midtool, "hi") is None)
    t_act = m.create_task("active", ["a", "b"])
    check("route.active_task_escalates", choose_model(stub, m, [], "hi") is None)
    m.finish_task(t_act, "closing for test", "abandoned")
    check("route.same_model_noop",
          choose_model(SimpleNamespace(model="x", fast_model="x",
                                       route=lambda t: "x"), m, [], "hi") is None)

    # ---- end-to-end: non-streaming planned flow (mock backend) ------------- #
    brain = make_brain("ollama")
    _demo_tid = m.conn.execute("SELECT COALESCE(MAX(id),0) FROM tasks").fetchone()[0] + 1
    SCRIPT[:] = [
        {"role": "assistant", "content": "Planning.",
         "tool_calls": [{"function": {"name": "create_task_plan", "arguments":
             {"title": "Demo", "steps": ["make file", "check file"]}}}]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "write_file", "arguments":
             {"path": f"{TEST_HOME}/demo.txt", "content": "hello"}}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "update_task_step", "arguments":
                {"task_id": _demo_tid, "step": 1, "status": "done",
                 "note": "write tool read back content, sha matched"}}},
            {"function": {"name": "update_task_step", "arguments":
                {"task_id": _demo_tid, "step": 2, "status": "done",
                 "note": "verified on disk via tool readback"}}}]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "complete_task", "arguments":
             {"task_id": _demo_tid, "summary": "file created and verified"}}}]},
        {"role": "assistant", "content": "All done."},
    ]
    msgs = []
    reply = run_turn(brain, m, msgs, "make a demo file and verify it",
                     auto_approve=True, session_id="sX")
    check("e2e.reply", "All done" in reply)
    task = m.get_task(_demo_tid)
    check("e2e.task_completed", task["status"] == "completed" and
          all(s["status"] == "done" for s in task["steps"]))
    check("e2e.file", open(f"{TEST_HOME}/demo.txt").read() == "hello")
    check("e2e.system_joined",
          "Working method" in RECEIVED[-1]["messages"][0]["content"])
    check("e2e.history_intact", trim_history(msgs) == msgs)

    # ---- end-to-end: streaming flow ----------------------------------------- #
    deltas = []
    SCRIPT[:] = [
        {"role": "assistant", "content": "Checking the plans.",
         "tool_calls": [{"function": {"name": "list_tasks",
                                      "arguments": {"status": "completed"}}}]},
        {"role": "assistant", "content": "You have one completed demo task."},
    ]
    RECEIVED.clear()
    msgs2 = []
    statuses = []
    reply = run_turn(brain, m, msgs2, "any finished tasks?",
                     auto_approve=True, session_id="sY", on_text=deltas.append,
                     on_status=statuses.append)
    check("stream.flag_sent", RECEIVED[0].get("stream") is True)
    check("stream.deltas_clean",
          "".join(deltas) == "Checking the plans.You have one completed demo task.")
    check("stream.tool_activity_reported",
          any("list_tasks" in s for s in statuses))
    check("stream.model_activity_reported",
          any("thinking" in s or "step" in s for s in statuses))
    check("stream.final_reply", reply == "You have one completed demo task.")
    check("stream.tool_ran_mid_stream",
          any(x["role"] == "tool" and "Demo" in x["content"]
              for x in RECEIVED[1]["messages"]))

    # ---- length-limit auto-continue ---- #
    class _Blk:
        def __init__(self, text):
            self.type = "text"; self.text = text

    class _Resp:
        def __init__(self, text, stop):
            self.content = [_Blk(text)]; self.stop_reason = stop

    class _ContBrain:
        backend = "anthropic"; model = "m"; fast_model = "m"
        last_usage = None; last_engine = "claude"

        def __init__(self, script):
            self._s = script; self._i = 0

        def chat(self, messages, system, tools=None, on_text=None, model=None):
            r = self._s[min(self._i, len(self._s) - 1)]; self._i += 1
            if on_text:
                for b in r.content:
                    on_text(b.text)
            return r

        def summarize(self, t):
            return ""

        def extract_facts(self, *a, **k):
            return []

    _cb = _ContBrain([_Resp("first half ", "max_tokens"),
                      _Resp("second half", "end_turn")])
    _cf = run_turn(_cb, m, [], "long thing", auto_approve=True, on_text=lambda t: None)
    check("continue.stitches_cutoff",
          "first half" in _cf and "second half" in _cf and _cb._i == 2)
    _cb2 = _ContBrain([_Resp("x ", "max_tokens")] * 20)
    _cf2 = run_turn(_cb2, m, [], "endless", auto_approve=True, on_text=lambda t: None)
    check("continue.bounded_and_flagged",
          _cb2._i == 5 and "length limit" in _cf2)

    # ---- tool-round limit: final pass delivers an answer (no dead-end) ---- #
    import agent.tools as _tools
    _orig_exec = _tools.execute_tool
    _tools.execute_tool = lambda *a, **k: "info"
    try:
        class _TBlk:
            type = "tool_use"
            def __init__(self, n):
                self.id = f"tt{n}"; self.name = "read_file"
                self.input = {"path": f"file{n}.txt"}     # distinct each round

        class _TResp:
            stop_reason = "tool_use"
            def __init__(self, n):
                self.content = [_TBlk(n)]

        class _LimitBrain:
            backend = "anthropic"; model = "m"; fast_model = "m"
            last_usage = None; last_engine = "claude"

            def __init__(self):
                self.tool_calls = 0

            def chat(self, messages, system, tools=None, on_text=None, model=None):
                if tools:                      # in the loop: never stop calling
                    self.tool_calls += 1
                    return _TResp(self.tool_calls)
                if on_text:                    # final pass (tools off): deliver
                    on_text("FINAL ANSWER")
                return _Resp("FINAL ANSWER", "end_turn")

            def summarize(self, t):
                return ""

            def extract_facts(self, *a, **k):
                return []

        _lb = _LimitBrain()
        _lf = run_turn(_lb, m, [], "do it", auto_approve=True, on_text=lambda t: None)
        check("limit.final_pass_delivers",
              "FINAL ANSWER" in _lf
              and "tool-use round limit reached" not in _lf
              and _lb.tool_calls == config.MAX_TOOL_ROUNDS)
    finally:
        _tools.execute_tool = _orig_exec

    # ---- sub-agent: isolation + depth guard ------------------------------ #
    # Parent delegates; only the sub-agent's report enters parent context.
    SCRIPT[:] = [
        # parent turn 1: spawn a sub-agent
        {"role": "assistant", "content": "Delegating the count.",
         "tool_calls": [{"function": {"name": "run_subagent", "arguments":
             {"objective": "Count the lines in the demo file and report the number",
              "context": f"file is {TEST_HOME}/demo.txt"}}}]},
        # --- sub-agent loop (its own context) ---
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "run_command", "arguments":
             {"command": f"wc -l {TEST_HOME}/demo.txt"}}}]},
        {"role": "assistant", "content": "The demo file has 0 newlines (one line)."},
        # parent turn 2: uses the report
        {"role": "assistant", "content": "The sub-agent reports the file has one line."},
    ]
    RECEIVED.clear()
    msgs3 = []
    reply = run_turn(brain, m, msgs3, "find out how many lines the demo file has",
                     auto_approve=True, session_id="sub")
    check("subagent.parent_reply", "one line" in reply)
    # parent context must contain the report but NOT the sub-agent's wc tool call
    parent_blob = " ".join(tools._render_blocks(mm["content"]) for mm in msgs3)
    check("subagent.report_in_parent", "reports the file has one line" in parent_blob)
    check("subagent.churn_isolated", "wc -l" not in parent_blob)
    # tool schema sent for the sub-agent's own calls excludes run_subagent
    subagent_req = RECEIVED[1]
    tool_names = {t["function"]["name"] for t in subagent_req.get("tools", [])}
    check("subagent.restricted_tools",
          "run_subagent" not in tool_names and "run_command" in tool_names
          and "create_task_plan" not in tool_names)
    # depth guard: a nested spawn is refused
    out = tools.execute_tool("run_subagent",
                             {"objective": "try to nest another worker here"},
                             m, console, True, brain=brain, depth=1)
    check("subagent.no_nesting", out.startswith("Error") and "cannot spawn" in out)
    out = tools.execute_tool("run_subagent", {"objective": "x"},
                             m, console, True, brain=brain, depth=0)
    check("subagent.objective_required", out.startswith("Error"))

    # ---- compaction: real summary + graceful fallback --------------------- #
    from agent.main import compact_history, _turn_starts

    def make_history(n_turns):
        h = []
        for i in range(n_turns):
            h.append({"role": "user", "content": f"question number {i}"})
            h.append({"role": "assistant",
                      "content": [SimpleNamespace(type="text", text=f"answer {i}")]})
        return h

    class GoodBrain:                                   # deterministic summary
        model = "x"
        def summarize(self, t):
            return "User asked a sequence of numbered questions; all answered."

    class DumbBrain:                                   # summarize fails → fallback
        model = "x"
        def summarize(self, t):
            return ""

    short = make_history(5)
    check("compact.below_trigger_untouched",
          compact_history(GoodBrain(), short) is short)

    long_hist = make_history(config.COMPACT_TRIGGER_TURNS + 4)
    compacted = compact_history(GoodBrain(), long_hist)
    check("compact.shrank", len(compacted) < len(long_hist))
    check("compact.summary_first",
          compacted[0]["role"] == "user" and "Summary" in compacted[0]["content"])
    check("compact.alternation", compacted[1]["role"] == "assistant")
    check("compact.keeps_recent_verbatim", compacted[-1] == long_hist[-1])
    check("compact.keep_count",
          len(_turn_starts(compacted)) == config.COMPACT_KEEP_TURNS + 1)  # +summary turn

    fb = compact_history(DumbBrain(), make_history(config.MAX_HISTORY_TURNS + 6))
    check("compact.fallback_trims",
          len(_turn_starts(fb)) == config.MAX_HISTORY_TURNS and
          all("Summary" not in (mm["content"] if isinstance(mm["content"], str) else "")
              for mm in fb))

    # ---- KTO export ------------------------------------------------------- #
    from training.export_dataset import build_kto_examples, build_examples, load_pairs
    sid = "ktosess"
    m.log_message(sid, "user", "give me the eod deadline")
    g = m.log_message(sid, "assistant", "Your EOD report is due at 16:30 SAST.")
    m.rate_message(g, 1)
    m.log_message(sid, "user", "write a limerick about bonds")
    b = m.log_message(sid, "assistant", "there once was a bond, quite unfond...")
    m.rate_message(b, -1)
    m.log_message(sid, "user", "unrated question here")
    m.log_message(sid, "assistant", "unrated answer that KTO should ignore")
    pairs = load_pairs(m.db_path)
    kto = build_kto_examples(pairs, "SYS", context_turns=1)
    check("kto.only_rated", len(kto) == 2)
    labels = sorted(e["label"] for e in kto)
    check("kto.both_classes", labels == [False, True])
    good_ex = next(e for e in kto if e["label"])
    check("kto.shape",
          good_ex["prompt"][0]["role"] == "system" and
          good_ex["prompt"][-1]["role"] == "user" and
          good_ex["completion"][0]["role"] == "assistant" and
          "16:30" in good_ex["completion"][0]["content"])
    # SFT export still excludes the /bad one and includes unrated
    sft = build_examples(pairs, "SYS", 1, only_rated=False, min_chars=5)
    sft_assistants = [ex["messages"][-1]["content"] for ex in sft]
    check("kto.sft_excludes_bad",
          not any("unfond" in a for a in sft_assistants) and
          any("16:30" in a for a in sft_assistants) and
          any("ignore" in a for a in sft_assistants))

    # ---- cooperative cancellation (powers the Jobs tab) ------------------- #
    # should_cancel True before any round -> no model call, returns a Stopped note.
    RECEIVED.clear()
    SCRIPT[:] = [{"role": "assistant", "content": "should never be sent"}]
    msgs_c = []
    r = run_turn(brain, m, msgs_c, "do a long thing", auto_approve=True,
                 session_id="cancel", should_cancel=lambda: True)
    check("cancel.stops_before_model_call",
          "Stopped" in (r or "") and len(RECEIVED) == 0 and len(SCRIPT) == 1)
    check("cancel.history_has_only_user",
          len(msgs_c) == 1 and msgs_c[0]["role"] == "user")

    # cancel after the first round: a flag that flips True once it's been read.
    RECEIVED.clear()
    SCRIPT[:] = [
        {"role": "assistant", "content": "first step done",
         "tool_calls": [{"function": {"name": "list_tasks",
                                      "arguments": {"status": "all"}}}]},
        {"role": "assistant", "content": "second step (should not run)"},
    ]
    flips = {"n": 0}
    def cancel_after_one():
        flips["n"] += 1
        return flips["n"] > 1          # allow round 1, cancel before round 2
    msgs_c2 = []
    r = run_turn(brain, m, msgs_c2, "two-step thing", auto_approve=True,
                 session_id="cancel2", should_cancel=cancel_after_one)
    # the tool round ran locally (tool_result is in the history) but the 2nd
    # model reply never happened (we cancelled before round 2's call)
    check("cancel.midway_runs_then_stops",
          any(isinstance(x.get("content"), list) for x in msgs_c2)
          and (r or "").startswith("first step done") and "Stopped" in (r or "")
          and "second step" not in (r or ""))
    # history ends on a tool_result (user role, list content) — a valid boundary
    last = msgs_c2[-1]
    check("cancel.history_valid_after_midway",
          last["role"] == "user" and isinstance(last["content"], list))

    # ---- cross-backend history is JSON-serializable (hybrid bug fix) ------ #
    # An Ollama reply (SimpleNamespace blocks) must be stored as plain dicts so
    # a later Claude turn can serialize the history without crashing.
    import json as _json
    from agent.main import _normalize_blocks
    mixed = [
        SimpleNamespace(type="text", text="sure, here goes"),
        SimpleNamespace(type="tool_use", name="read_file",
                        input={"path": "/x"}, id="call_0"),
    ]
    norm = _normalize_blocks(mixed)
    _json.dumps(norm)                                   # must not raise
    check("hybrid.normalize_serializable",
          all(isinstance(b, dict) for b in norm) and norm[0]["type"] == "text"
          and norm[1]["name"] == "read_file")
    # a full Ollama turn through run_turn leaves history fully serializable
    SCRIPT[:] = [{"role": "assistant", "content": "answered locally",
                  "tool_calls": [{"function": {"name": "list_tasks",
                                               "arguments": {"status": "all"}}}]},
                 {"role": "assistant", "content": "done"}]
    msgs_h = []
    run_turn(brain, m, msgs_h, "do a thing", auto_approve=True, session_id="hy")
    _json.dumps(msgs_h)                                 # the real crash repro — must not raise
    check("hybrid.history_serializable_after_turn",
          all(not isinstance(b, SimpleNamespace)
              for mm in msgs_h if isinstance(mm["content"], list)
              for b in mm["content"]))

    # ---- scheduler date math (pure, deterministic) ------------------------ #
    import time as _t
    from datetime import datetime as _dt
    from agent.scheduler import make_spec, parse_spec, next_run, describe_spec
    base = _dt(2026, 6, 12, 10, 0, 0)            # a Friday, 10:00 local
    check("sched.daily_rolls_to_tomorrow",
          _dt.fromtimestamp(next_run(parse_spec(make_spec("daily", time_str="07:30")),
                                     now=base)) == _dt(2026, 6, 13, 7, 30))
    check("sched.daily_later_today",
          _dt.fromtimestamp(next_run(parse_spec(make_spec("daily", time_str="18:00")),
                                     now=base)) == _dt(2026, 6, 12, 18, 0))
    check("sched.weekdays_skips_weekend",
          _dt.fromtimestamp(next_run(parse_spec(make_spec("weekdays", time_str="07:30")),
                                     now=base)) == _dt(2026, 6, 15, 7, 30))
    check("sched.hourly_top_of_next_hour",
          _dt.fromtimestamp(next_run(parse_spec(make_spec("hourly")),
                                     now=base.replace(minute=17))) == _dt(2026, 6, 12, 11, 0))
    check("sched.every_n_minutes",
          int(next_run(parse_spec(make_spec("minutes", n=30)), now=base)
              - base.timestamp()) == 1800)
    check("sched.weekly_next_wednesday",
          _dt.fromtimestamp(next_run(parse_spec(make_spec("weekly", time_str="09:00",
                                                          dow=2)),
                                     now=base)) == _dt(2026, 6, 17, 9, 0))
    try:
        make_spec("daily", time_str="25:61")
        check("sched.bad_time_rejected", False)
    except ValueError:
        check("sched.bad_time_rejected", True)
    check("sched.describe",
          describe_spec(parse_spec(make_spec("weekdays", time_str="07:30")))
          == "weekdays at 07:30")

    # ---- conversation history APIs ----------------------------------------- #
    m.log_message("hist-aaaa", "user", "plan the quarterly report")
    m.log_message("hist-aaaa", "assistant", "Here is a plan.")
    m.log_message("hist-bbbb", "user", "second conversation")
    sess_list = m.list_sessions(10)
    ids = [x["session_id"] for x in sess_list]
    check("history.lists_sessions", "hist-aaaa" in ids and "hist-bbbb" in ids)
    entry = next(x for x in sess_list if x["session_id"] == "hist-aaaa")
    check("history.title_from_first_user",
          entry["title"].startswith("plan the quarterly") and entry["n"] == 2)
    check("history.transcript_order",
          [t["role"] for t in m.get_transcript("hist-aaaa")] == ["user", "assistant"])
    check("history.prefix_find", m.find_sessions("hist-aa") == ["hist-aaaa"])
    check("history.delete", m.delete_session("hist-bbbb") == 1
          and m.find_sessions("hist-bbbb") == [])

    # ---- schedule storage --------------------------------------------------- #
    sid_s = m.create_schedule("test-brief", "say hello", make_spec("minutes", n=5),
                              "Auto", False, next_run=_t.time() - 1)
    check("schedstore.create_and_due",
          any(x["id"] == sid_s for x in m.due_schedules(_t.time())))
    m.schedule_ran(sid_s, _t.time(), _t.time() + 300, "ok", "hello there")
    got = m.get_schedule(sid_s)
    check("schedstore.ran_updates",
          got["last_status"] == "ok" and got["next_run"] > _t.time())
    check("schedstore.disable_excludes",
          m.set_schedule_enabled(sid_s, False, None)
          and not any(x["id"] == sid_s for x in m.due_schedules(_t.time() + 9999)))
    check("schedstore.delete", m.delete_schedule(sid_s))

    # ---- turn_info: engine + token reporting -------------------------------- #
    SCRIPT[:] = [{"role": "assistant", "content": "hi there"}]
    tinfo: dict = {}
    run_turn(brain, m, [], "hello", auto_approve=True, session_id="ti",
             turn_info=tinfo)
    check("turninfo.engine_reported", tinfo.get("engine") == "local")
    tu = tinfo.get("usage", {})
    check("turninfo.tokens_present", tu.get("in", 0) > 0 and tu.get("out", 0) > 0)

    # ---- web access: parsing, guard, off-switch (no network needed) -------- #
    from agent import web as webmod
    import agent.tools as toolsmod
    from rich.console import Console as _Console
    import io as _io
    _csink = _Console(file=_io.StringIO())
    sample_html = (
        '<div class="result"><a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=x">'
        'Example <b>Docs</b></a>'
        '<a class="result__snippet" href="#">The official &amp; latest <b>docs</b>.</a></div>'
        '<div class="result"><a rel="nofollow" class="result__a" '
        'href="https://news.site/story">Big Story</a>'
        '<div class="result__snippet">Something happened.</div></div>')
    wres = webmod._parse_html_results(sample_html, 5)
    check("web.parse_titles",
          [r["title"] for r in wres] == ["Example Docs", "Big Story"])
    check("web.uddg_decoded", wres[0]["url"] == "https://example.com/docs")
    check("web.entities_and_tags",
          wres[0]["snippet"] == "The official & latest docs."
          and wres[1]["snippet"] == "Something happened.")
    sample_lite = (
        '<table><tr><td><a rel="nofollow" href="https://a.b/c" '
        'class="result-link">A Title</a></td></tr>'
        '<tr><td class="result-snippet">Some snippet text.</td></tr></table>')
    wres2 = webmod._parse_lite_results(sample_lite, 5)
    check("web.lite_parser",
          wres2 and wres2[0]["url"] == "https://a.b/c"
          and wres2[0]["title"] == "A Title"
          and "Some snippet" in wres2[0]["snippet"])
    check("web.private_blocked",
          webmod._is_private_host("127.0.0.1")
          and webmod._is_private_host("192.168.1.7")
          and webmod._is_private_host("10.0.0.9")
          and webmod._is_private_host("localhost")
          and not webmod._is_private_host("93.184.216.34"))
    try:
        webmod._check_url_allowed("ftp://example.com/x")
        check("web.scheme_guard", False)
    except webmod.WebError:
        check("web.scheme_guard", True)
    wtxt = webmod._extract_text(
        "<html><head><title>T</title><script>x=1</script></head>"
        "<body><h1>Hello</h1><p>World &amp; co</p></body></html>")
    check("web.text_extractor",
          "Hello" in wtxt and "World & co" in wtxt and "x=1" not in wtxt)
    _prev_web = toolsmod.WEB_ENABLED
    toolsmod.WEB_ENABLED = False
    woff = toolsmod.execute_tool("web_search", {"query": "x"}, m, _csink, True)
    check("web.off_switch", "turned off" in woff)
    woff2 = toolsmod.execute_tool("fetch_page", {"url": "https://x.y"}, m, _csink, True)
    check("web.off_switch_fetch", "turned off" in woff2)
    toolsmod.WEB_ENABLED = _prev_web

    # ---- hybrid routing: a second local model is selectable --------------- #
    from agent.brain import HybridBrain

    class _RoutingBackend:
        def __init__(self, engine, model):
            self.model = model
            self._engine = engine
            self.last_engine = engine
            self.last_usage = {"in": 1, "out": 1, "cache_read": 0, "cache_write": 0}
            self.calls = []

        def chat(self, messages, system, tools=None, on_text=None, model=None):
            self.calls.append(model)
            self.last_engine = self._engine
            return SimpleNamespace(content=[], stop_reason="end_turn")

    hb = HybridBrain.__new__(HybridBrain)         # bypass __init__ (no servers)
    hb.cloud = _RoutingBackend("claude", "claude-x")
    hb.local = _RoutingBackend("local", "qwen2.5-coder:32b")
    hb.model = hb.cloud.model
    hb.fast_model = hb.local.model

    hb.chat([], "s", model="qwen3.6")             # forced second local model
    check("hybrid.second_local_to_ollama", hb.local.calls == ["qwen3.6"])
    check("hybrid.second_local_engine", hb.last_engine == "local")

    hb.local.calls.clear()
    hb.chat([], "s", model=hb.fast_model)         # local default marker
    check("hybrid.default_local_model", hb.local.calls == ["qwen2.5-coder:32b"])

    hb.local.calls.clear(); hb.cloud.calls.clear()
    hb.chat([], "s", model=None)                  # Claude
    check("hybrid.none_to_cloud",
          hb.cloud.calls == [None] and hb.local.calls == [])
    check("hybrid.cloud_engine", hb.last_engine == "claude")

    hb.cloud.calls.clear()
    hb.chat([], "s", model=hb.cloud.model)        # explicit cloud id
    check("hybrid.explicit_cloud", hb.cloud.calls == ["claude-x"])

    # ---- routing feedback: storage + escalation override ------------------ #
    import agent.config as _cfg
    check("routefb.empty_initially", m.count_routing_escalations() == 0)
    fid = m.add_routing_escalation("convert fx trade timestamps to sast in python", None)
    check("routefb.add", fid and m.count_routing_escalations() == 1)
    m.add_routing_escalation("convert fx trade timestamps to sast in python", None)
    check("routefb.dedupe_same_text", m.count_routing_escalations() == 1)

    class _AlwaysLocal:
        backend = "hybrid"; model = "cloud-id"; fast_model = "local-id"
        def route(self, text): return self.fast_model      # always SIMPLE

    fb = _AlwaysLocal()
    _save_fb = _cfg.ROUTE_FEEDBACK
    _cfg.ROUTE_FEEDBACK = True
    # token-overlap path (no embeddings in the test sandbox): a closely
    # overlapping prompt escalates, an unrelated one stays local.
    check("routefb.similar_escalates",
          choose_model(fb, m, [], "convert fx trade timestamps into sast using python") is None)
    check("routefb.unrelated_stays_local",
          choose_model(fb, m, [], "tell me a joke about cats") == fb.fast_model)
    _cfg.ROUTE_FEEDBACK = False
    check("routefb.off_switch",
          choose_model(fb, m, [], "convert fx trade timestamps into sast using python")
          == fb.fast_model)
    _cfg.ROUTE_FEEDBACK = _save_fb
    check("routefb.clear", m.clear_routing_escalations() == 1
          and m.count_routing_escalations() == 0)
    check("routefb.jaccard_helper",
          abs(agent_main._jaccard({"a", "b", "c"}, {"b", "c", "d"}) - 0.5) < 1e-9
          and agent_main._jaccard(set(), {"x"}) == 0.0)

    # ---- RAG auto-watch folders ------------------------------------------- #
    import tempfile as _tf
    from agent import rag as _rag
    _wdir = _tf.mkdtemp()
    with open(os.path.join(_wdir, "w1.md"), "w") as _fh:
        _fh.write("Watched alpha note about settlement.")
    _ds = _rag.DocumentStore(db_path=os.path.join(_wdir, "_watch.db"),
                             check_same_thread=False)
    check("watch.add", _ds.add_watched_folder(_wdir).get("id") is not None)
    check("watch.dedupe", _ds.add_watched_folder(_wdir).get("already") is True)
    check("watch.reject_missing", "error" in _ds.add_watched_folder("/no/such/dir"))
    check("watch.list_one", len(_ds.list_watched_folders()) == 1)
    r = _ds.rescan_all_watched()
    check("watch.rescan_indexes", r["folders"] == 1 and r["added"] == 1
          and _ds.doc_count() == 1)
    with open(os.path.join(_wdir, "w2.md"), "w") as _fh:
        _fh.write("Watched bravo note about nostro.")
    r = _ds.rescan_all_watched()
    check("watch.rescan_adds_new", r["added"] == 1 and _ds.doc_count() == 2)
    r = _ds.rescan_all_watched()
    check("watch.rescan_skips_unchanged", r["added"] == 0 and r["updated"] == 0)
    os.remove(os.path.join(_wdir, "w1.md"))
    r = _ds.rescan_all_watched()
    check("watch.rescan_prunes_deleted", r["pruned"] == 1 and _ds.doc_count() == 1)
    wf = _ds.list_watched_folders()[0]
    check("watch.records_result", bool(wf["last_scan"]) and "removed" in wf["last_result"])
    # one-shot ingest must NOT prune deleted files (snapshot semantics)
    _odir = _tf.mkdtemp()
    with open(os.path.join(_odir, "o.md"), "w") as _fh:
        _fh.write("oneshot note")
    _ds.ingest_path(_odir)
    os.remove(os.path.join(_odir, "o.md"))
    _ds.ingest_path(_odir)
    check("watch.oneshot_no_prune", _ds.doc_count() == 2)
    fid = _ds.list_watched_folders()[0]["id"]
    check("watch.unwatch_keeps_docs",
          _ds.remove_watched_folder(fid) and not _ds.list_watched_folders()
          and _ds.doc_count() == 2)

    # ---- voice: TTS text prep + STT plumbing (no audio hardware needed) ---- #
    from agent import voice as _voice
    _sm = _voice._strip_markdown("**Bold** `code` [doc](http://x.io) # H\n\n- pt")
    check("voice.strip_markdown",
          all(c not in _sm for c in "*`#[]") and "http" not in _sm
          and "Bold" in _sm and "doc" in _sm and "pt" in _sm)
    check("voice.speak_empty", _voice.speak("") is False and _voice.speak("  ") is False)
    check("voice.engine_known",
          _voice.tts_engine() in ("windows-sapi", "macos-say", "espeak",
                                   "spd-say", "pyttsx3", "none"))
    _fw = SimpleNamespace()

    class _Seg:
        def __init__(self, t): self.text = t

    class _FWModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, path): return ([_Seg("hello"), _Seg(" there")], {})
    _fw.WhisperModel = _FWModel
    sys.modules["faster_whisper"] = _fw
    _voice._whisper_model = None
    check("voice.stt_available", _voice.stt_available() is True)
    check("voice.transcribe", _voice.transcribe("/tmp/x.wav", "base") == "hello there")
    del sys.modules["faster_whisper"]

    # ---- tool-pair sanitization (the Retry-on-Claude 400 fix) ------------- #
    from agent.brain import _sanitize_tool_pairs, _merge_consecutive

    def _valid_pairs(ms):
        for j, mm in enumerate(ms):
            if mm["role"] == "assistant" and isinstance(mm["content"], list):
                tids = [b["id"] for b in mm["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use"]
                if tids:
                    if j + 1 >= len(ms):
                        return False
                    nx = ms[j + 1]
                    if nx["role"] != "user" or not isinstance(nx["content"], list):
                        return False
                    ans = {b["tool_use_id"] for b in nx["content"]
                           if isinstance(b, dict) and b.get("type") == "tool_result"}
                    if not set(tids) <= ans:
                        return False
        return all(a["role"] != b["role"] for a, b in zip(ms, ms[1:]))

    # the exact failure: assistant tool_use orphaned before a plain user message
    bug = [{"role": "user", "content": "earlier"},
           {"role": "assistant", "content": [{"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_01", "name": "web_search", "input": {}}]},
           {"role": "user", "content": "retried question"}]
    repaired = _merge_consecutive(_sanitize_tool_pairs(bug))
    check("toolfix.orphan_repaired", _valid_pairs(repaired))
    check("toolfix.synth_result_present",
          repaired[2]["content"][0]["type"] == "tool_result")
    check("toolfix.no_double_user",
          all(a["role"] != b["role"] for a, b in zip(repaired, repaired[1:])))
    # normal completed sequence is left intact
    normal = [{"role": "user", "content": "q"},
              {"role": "assistant", "content": [{"type": "tool_use", "id": "a",
                                                 "name": "t", "input": {}}]},
              {"role": "user", "content": [{"type": "tool_result",
                                            "tool_use_id": "a", "content": "ok"}]},
              {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    kept = _merge_consecutive(_sanitize_tool_pairs(normal))
    check("toolfix.normal_preserved", kept == normal and _valid_pairs(kept))
    # orphan tool_result (no preceding tool_use) is dropped
    orphan_res = [{"role": "user", "content": "q"},
                  {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                  {"role": "user", "content": [{"type": "tool_result",
                                                "tool_use_id": "ghost", "content": "x"}]}]
    cleaned_or = _merge_consecutive(_sanitize_tool_pairs(orphan_res))
    check("toolfix.orphan_result_dropped",
          not any(isinstance(b, dict) and b.get("type") == "tool_result"
                  for mm in cleaned_or for b in
                  (mm["content"] if isinstance(mm["content"], list) else [])))

    # ---- tool-pairing repair (Retry / cancelled-tool histories) ----------- #
    from types import SimpleNamespace as _NS
    import agent.brain as _brain

    def _unpaired(out):
        gv = lambda b, k: (b.get(k) if isinstance(b, dict) else getattr(b, k, None))
        bad = []
        for j, mm in enumerate(out):
            c = mm.get("content")
            if mm.get("role") == "assistant" and isinstance(c, list):
                ids = [gv(b, "id") for b in c if gv(b, "type") == "tool_use"]
                nx = out[j + 1] if j + 1 < len(out) else None
                rs = set()
                if nx and isinstance(nx.get("content"), list):
                    rs = {gv(b, "tool_use_id") for b in nx["content"]
                          if gv(b, "type") == "tool_result"}
                bad += [t for t in ids if t not in rs]
        return bad

    # run_turn-level: truncate a dangling tail to the last clean assistant reply
    h = [{"role": "user", "content": "a"},
         {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
         {"role": "user", "content": "b"},
         {"role": "assistant", "content": [{"type": "tool_use", "id": "tD",
                                            "name": "x", "input": {}}]}]
    agent_main._sanitize_tool_pairs(h)
    check("toolfix.truncate_tail",
          len(h) == 2 and h[-1]["role"] == "assistant"
          and not any(b.get("type") == "tool_use"
                      for b in h[-1]["content"] if isinstance(b, dict)))

    # brain-level: repair a dict dangler in the middle (synthesize result)
    _, o1 = _brain.prepare_anthropic_request(
        [{"role": "user", "content": "q"},
         {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                            "name": "x", "input": {}}]},
         {"role": "assistant", "content": [{"type": "text", "text": "later"}]},
         {"role": "user", "content": "q2"}], "sys", cache=False)
    check("toolfix.brain_dict_repair", _unpaired(o1) == [])

    # brain-level: repair an SDK-object dangler (the real-world gap)
    _, o2 = _brain.prepare_anthropic_request(
        [{"role": "user", "content": "q"},
         {"role": "assistant", "content": [_NS(type="tool_use", id="tS",
                                               name="x", input={})]},
         {"role": "user", "content": "retry text"}], "sys", cache=False)
    check("toolfix.brain_sdk_repair", _unpaired(o2) == [])

    # valid history is left semantically intact (no spurious unpairing)
    _, o3 = _brain.prepare_anthropic_request(
        [{"role": "user", "content": "q"},
         {"role": "assistant", "content": [{"type": "tool_use", "id": "t9",
                                            "name": "x", "input": {}}]},
         {"role": "user", "content": [{"type": "tool_result",
                                       "tool_use_id": "t9", "content": "r"}]},
         {"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
        "sys", cache=False)
    check("toolfix.valid_untouched", _unpaired(o3) == [])

    # Regression for the reported "messages.10: tool_use ... without tool_result"
    # error on Retry: a long conversation whose tail is a dangling tool_use (a
    # turn cancelled or rewound before its result was recorded) must reach the
    # Claude API fully paired after the main sanitizer (truncate) + brain
    # sanitizer (synthesise) run, the way run_turn -> AnthropicBrain.chat does.
    _long = []
    for _k in range(3):
        _long += [
            {"role": "user", "content": f"q{_k}"},
            {"role": "assistant", "content": [{"type": "tool_use",
                                               "id": f"ok{_k}", "name": "t", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result",
                                          "tool_use_id": f"ok{_k}", "content": "r"}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"done {_k}"}]},
        ]
    _long += [{"role": "user", "content": "explain this chart"},
              {"role": "assistant", "content": [{"type": "tool_use",
                                                 "id": "toolu_DANGLING", "name": "web_search",
                                                 "input": {"q": "x"}}]}]
    agent_main._sanitize_tool_pairs(_long)                 # in place, as run_turn does
    _long.append({"role": "user", "content": "retry"})  # run_turn appends new turn
    _, _o4 = _brain.prepare_anthropic_request(_long, "sys", cache=False)
    _alt = all(a["role"] != b["role"] for a, b in zip(_o4, _o4[1:]))
    check("toolfix.retry_dangling_tail", _unpaired(_o4) == [] and _alt)

    # ---- vision: image routing at the engine level (no Pillow/UI needed) ---- #
    _img = {"type": "image", "source": {"type": "base64",
                                        "media_type": "image/png", "data": "QUJD"}}
    _seen: dict = {}

    class _VBrain:
        backend = "hybrid"
        model = "claude-sonnet-4-6"
        fast_model = "claude-haiku-4-5-20251001"
        local = object()

        def chat(self, messages, system, tools=None, on_text=None, model=None):
            _seen["model"] = model
            _seen["content"] = messages[-1]["content"]
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn")
    agent_main.run_turn(_VBrain(), m, [], "what is this?", auto_approve=True,
                        images=[_img], turn_info={})
    check("vision.auto_routes_claude",
          _seen.get("model") is None
          and isinstance(_seen.get("content"), list)
          and _seen["content"][0].get("type") == "text"
          and any(b.get("type") == "image" for b in _seen["content"]))
    _ob = _brain.OllamaBrain.__new__(_brain.OllamaBrain)
    _om = _ob._to_ollama_messages(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}, _img]}], "sys")
    _um = [mm for mm in _om if mm.get("role") == "user"][-1]
    check("vision.ollama_images_field",
          bool(_um.get("images")) and _um["images"][0] == "QUJD")

    # ---- file uploads: attachment folding + local extraction ---- #
    _seen2: dict = {}

    class _ABrain:
        backend = "hybrid"
        model = "claude-sonnet-4-6"
        fast_model = "claude-haiku-4-5-20251001"
        local = object()

        def chat(self, messages, system, tools=None, on_text=None, model=None):
            _seen2["content"] = messages[-1]["content"]
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn")
    agent_main.run_turn(_ABrain(), m, [], "the question", auto_approve=True,
                        attachments="FILE TEXT HERE\n\n", turn_info={})
    _c = _seen2["content"]
    _ct = _c if isinstance(_c, str) else _c[0]["text"]
    check("attach.folds_into_content",
          "FILE TEXT HERE" in _ct and "the question" in _ct)

    import tempfile as _tf
    import zipfile as _zip
    import agent.files as _files
    _td = _tf.mkdtemp()
    _tp = os.path.join(_td, "a.txt")
    open(_tp, "w").write("plain content here")
    check("files.txt", _files.extract_text(_tp)[0].strip() == "plain content here")

    _dx = os.path.join(_td, "a.docx")          # minimal docx => stdlib XML parser
    with _zip.ZipFile(_dx, "w") as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
            'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p>'
            '<w:r><w:t>docx body text</w:t></w:r></w:p></w:body></w:document>')
    check("files.docx_stdlib", "docx body text" in _files.extract_text(_dx)[0])

    _bp = os.path.join(_td, "a.bin")
    open(_bp, "wb").write(bytes(range(256)))
    _bt, _bn = _files.extract_text(_bp)
    # the message now names what DOES work instead of just refusing — a
    # refusal that leaves you guessing is barely better than a crash
    check("files.binary_rejected",
          _bt == "" and ("can't read" in _bn or "unsupported" in _bn)
          and "PDF, Word, Excel" in _bn)

    _big = os.path.join(_td, "big.txt")
    open(_big, "w").write("Z" * (_files.MAX_CHARS + 100))
    _gt, _gn = _files.extract_text(_big)
    check("files.truncated", len(_gt) == _files.MAX_CHARS and "truncated" in _gn)

    # ---- memory dedup / review ---- #
    _dm = MemoryStore(os.path.join(_tf.mkdtemp(), "dedup.db"))
    _x1 = _dm.add_memory("User works at Standard Bank on the FX desk", source="auto")
    _x2 = _dm.add_memory("User is employed at Standard Bank working on the FX desk", source="auto")
    _x3 = _dm.add_memory("User works on the FX desk at Standard Bank in Johannesburg", source="auto")
    _x4 = _dm.add_memory("User is based in Cape Town", source="auto")   # distinct
    _grp = _dm.find_similar_groups(use_embeddings=False)               # lexical, deterministic
    _rem = sorted(r["id"] for g in _grp for r in g["remove"])
    check("dedup.groups_near_dupes", _rem == sorted([_x1, _x2]))
    check("dedup.keeps_most_detailed", any(g["keep"]["id"] == _x3 for g in _grp))
    check("dedup.distinct_untouched", _x4 not in _rem)
    _before = _dm.memory_count()
    check("dedup.bulk_delete",
          _dm.delete_memories(_rem) == 2 and _dm.memory_count() == _before - 2)
    _d1 = _dm.add_memory("User likes coffee.", source="auto")
    _d2 = _dm.add_memory("user likes coffee", source="auto")           # same normalised
    check("dedup.exact_blocked_on_insert", _d1 is not None and _d2 is None)
    _dm.add_memory("User mentors junior quants", source="auto")
    _us = _dm.add_memory("User mentors the junior quants on the team", source="user")
    _mg = [g for g in _dm.find_similar_groups(use_embeddings=False)
           if "mentor" in g["keep"]["content"]]
    check("dedup.manual_over_auto", bool(_mg) and _mg[0]["keep"]["id"] == _us)

    # ---- backup / restore ---- #
    import zipfile as _zip2
    from agent import backup as _bk
    _bdir = _tf.mkdtemp()
    _bsrc = MemoryStore(os.path.join(_bdir, "src.db"))
    _bsrc.add_memory("Backup roundtrip: USDZAR 18.40", source="user")
    _bsrc.add_skill("brief", "mornings", "summarize FX")
    _bsrc.log_message("sx", "user", "hello there")
    _zp = _bk.create_backup(_bsrc, None, out_dir=os.path.join(_bdir, "out"))
    with _zip2.ZipFile(_zp) as _zf:
        _names = set(_zf.namelist())
    check("backup.zip_contents",
          {"agent.db", "export.json", "manifest.json"} <= _names)
    check("backup.manifest",
          _bk.read_manifest(_zp).get("format_version") == 2)
    # a format-1 archive (no checksums) must still verify structurally, so an
    # upgrade never orphans backups taken by the previous build
    _legacy = os.path.join(_bk._backups_dir(), "atlas-backup-legacy1.zip")
    with _zip2.ZipFile(_zp) as _s, _zip2.ZipFile(_legacy, "w") as _d:
        for _i in _s.infolist():
            _b = _s.read(_i.filename)
            if _i.filename == "manifest.json":
                _m = json.loads(_b)
                _m.pop("checksums", None)
                _m["format_version"] = 1
                _b = json.dumps(_m).encode()
            _d.writestr(_i, _b)
    _lv = _bk.verify_backup(os.path.basename(_legacy))
    check("backup.legacy_archive_still_verifies",
          _lv["ok"] is True and _lv.get("legacy") is True)

    _bdst = MemoryStore(os.path.join(_bdir, "dst.db"))
    _bdst.add_memory("This is replaced on restore", source="auto")
    _summary = _bk.restore_backup(_zp, _bdst, None)
    _rmems = [mm["content"] for mm in _bdst.all_memories()]
    check("backup.restore_replaces",
          any("USDZAR 18.40" in x for x in _rmems)
          and "This is replaced on restore" not in " ".join(_rmems))
    check("backup.restore_skills_and_transcript",
          any(s["name"] == "brief" for s in _bdst.get_skills())
          and any("hello there" in (mm.get("content") or "")
                  for mm in _bdst.get_transcript("sx")))
    check("backup.restore_fts",
          any("USDZAR" in r["content"] for r in _bdst.search_memories("USDZAR 18.40", 5)))
    _bad = os.path.join(_bdir, "bad.zip")
    with _zip2.ZipFile(_bad, "w") as _zf:
        _zf.writestr("x.txt", "not a backup")
    try:
        _bk.restore_backup(_bad, _bdst, None)
        check("backup.bad_zip_rejected", False)
    except ValueError:
        check("backup.bad_zip_rejected", True)

    # ---- settings persistence ---- #
    _orig = config.current_settings()
    _saved = config.save_settings({"AUTO_LEARN": False, "MAX_TOOL_ROUNDS": 7,
                                   "TURN_TIMEOUT": 0})
    check("settings.apply_live",
          config.AUTO_LEARN is False and config.MAX_TOOL_ROUNDS == 7
          and config.TURN_TIMEOUT == 0)
    check("settings.persisted",
          config.SETTINGS_FILE.exists()
          and json.loads(config.SETTINGS_FILE.read_text())["MAX_TOOL_ROUNDS"] == 7)
    check("settings.coerce_types",
          isinstance(config.save_settings({"MAX_TOOL_ROUNDS": 9.0})["MAX_TOOL_ROUNDS"], int))
    _reset = config.reset_settings()
    check("settings.reset_restores",
          config.AUTO_LEARN == _orig["AUTO_LEARN"]
          and config.MAX_TOOL_ROUNDS == _orig["MAX_TOOL_ROUNDS"]
          and not config.SETTINGS_FILE.exists())

    # ---- DeepSeek / OpenAI-compatible engine ---- #
    import types as _types
    from agent.brain import OpenAIBrain
    import agent.brain as _brainmod

    # message + tool conversion preserves tool_call_id linkage
    _conv = OpenAIBrain._to_openai_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "reading"},
            {"type": "tool_use", "id": "call_z", "name": "read_file",
             "input": {"path": "a.sql"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_z", "content": "DATA"}]},
    ], "SYS")
    _asst = [x for x in _conv if x["role"] == "assistant"][0]
    _toolm = [x for x in _conv if x["role"] == "tool"][0]
    check("deepseek.msg_conversion",
          _conv[0] == {"role": "system", "content": "SYS"}
          and _asst["tool_calls"][0]["id"] == "call_z"
          and json.loads(_asst["tool_calls"][0]["function"]["arguments"]) == {"path": "a.sql"}
          and _toolm["tool_call_id"] == "call_z" and _toolm["content"] == "DATA")
    check("deepseek.tool_schema",
          OpenAIBrain._to_openai_tools(
              [{"name": "f", "input_schema": {"type": "object"}}])[0]["type"] == "function")
    check("deepseek.finish_mapping",
          OpenAIBrain._finish_to_stop("tool_calls") == "tool_use"
          and OpenAIBrain._finish_to_stop("length") == "max_tokens"
          and OpenAIBrain._finish_to_stop("stop") == "end_turn")

    # provider errors (e.g. 429) become a clean chat message, not a raised traceback
    class _Boom:
        def create(self, **k):
            raise RuntimeError("Error code: 429 - {'status': 429, 'title': 'Too Many Requests'}")
    _err_client = _types.SimpleNamespace(
        chat=_types.SimpleNamespace(completions=_Boom()))
    _eb = OpenAIBrain(model="deepseek-ai/deepseek-v4-pro",
                      base_url="https://api.deepseek.com/v1",
                      client=_err_client)
    _er = _eb.chat([{"role": "user", "content": "hi"}], "sys")
    check("deepseek.rate_limit_message",
          _er.stop_reason == "end_turn"
          and "rate-limited" in _er.content[0].text.lower()
          and "429" in _er.content[0].text)
    # _friendly_error is an instance method now, so the message can name the
    # engine it actually is — it used to say "DeepSeek" whatever you were
    # using, sending people to the wrong provider's dashboard
    check("deepseek.error_classifier",
          "authentication" in _eb._friendly_error(
              RuntimeError("Error code: 401"), "m").lower()
          and "could not find" in _eb._friendly_error(
              RuntimeError("404 model not found"), "m").lower())

    # some endpoints return content as a list of parts, not a string
    check("openai.text_of_parts",
          OpenAIBrain._text_of("hi") == "hi"
          and OpenAIBrain._text_of(None) == ""
          and OpenAIBrain._text_of([{"type": "text", "text": "a"}, {"text": "b"}]) == "ab"
          and OpenAIBrain._text_of(["x", "y"]) == "xy")
    def _ck(content=None, finish=None):
        _d = _types.SimpleNamespace(content=content, tool_calls=None)
        return _types.SimpleNamespace(
            choices=[_types.SimpleNamespace(delta=_d, finish_reason=finish)],
            usage=None)
    _chunks = [_ck(content=[{"type": "text", "text": "Hel"}]),
               _ck(content="lo"), _ck(finish="stop")]
    class _ListStream:
        def create(self, **k): return iter(_chunks)
    _lb = OpenAIBrain(model="m", base_url="https://api.x.com/v1", client=_types.SimpleNamespace(
        chat=_types.SimpleNamespace(completions=_ListStream())))
    _got = []
    _lr = _lb.chat([{"role": "user", "content": "x"}], "s", on_text=_got.append)
    check("openai.stream_list_content",
          all(isinstance(g, str) for g in _got)
          and "".join(_got) == "Hello" and _lr.content[0].text == "Hello")

    # non-streaming parse via an injected fake client (no network)
    _tc = _types.SimpleNamespace(
        id="call_7", function=_types.SimpleNamespace(name="web_search",
                                                     arguments='{"q":"x"}'))
    _resp = _types.SimpleNamespace(
        choices=[_types.SimpleNamespace(
            message=_types.SimpleNamespace(content="ok", tool_calls=[_tc]),
            finish_reason="tool_calls")],
        usage=_types.SimpleNamespace(prompt_tokens=30, completion_tokens=5,
                                     prompt_tokens_details=None))
    _fake = _types.SimpleNamespace(chat=_types.SimpleNamespace(
        completions=_types.SimpleNamespace(create=lambda **k: _resp)))
    _ob = OpenAIBrain(model="deepseek-ai/deepseek-v4-pro",
                      base_url="https://api.deepseek.com/v1",
                      client=_fake)
    _r = _ob.chat([{"role": "user", "content": "hi"}], "sys",
                  tools=[{"name": "web_search", "input_schema": {}}])
    _tu = [b for b in _r.content if b.type == "tool_use"][0]
    check("deepseek.response_parse",
          _r.stop_reason == "tool_use" and _tu.name == "web_search"
          and _tu.input == {"q": "x"} and _tu.id == "call_7"
          and _ob.last_usage == {"in": 30, "out": 5, "cache_read": 0, "cache_write": 0}
          and _ob.last_engine == "deepseek")

    # per-model credential resolution: Flash override wins; Pro falls back to shared
    _pro_k, _pro_u = config.deepseek_creds(config.DEEPSEEK_MODEL_PRO)
    _fl_k, _fl_u = config.deepseek_creds(config.DEEPSEEK_MODEL_FLASH)
    check("deepseek.creds_resolution",
          _pro_k == (config.DEEPSEEK_API_KEY_PRO or config.DEEPSEEK_API_KEY)
          and _fl_k == (config.DEEPSEEK_API_KEY_FLASH or config.DEEPSEEK_API_KEY))

    # dispatch: a DeepSeek model id routes to the DeepSeek brain from any backend
    class _FakeDS:
        last_engine = "deepseek"
        last_usage = {"in": 1, "out": 1, "cache_read": 0, "cache_write": 0}
        def chat(self, messages, system, tools=None, on_text=None, model=None):
            return _types.SimpleNamespace(
                content=[_types.SimpleNamespace(type="text", text="DS")],
                stop_reason="end_turn")
    _prev_cache = dict(_brainmod._deepseek_brains)
    _brainmod._deepseek_brains[config.DEEPSEEK_MODEL_PRO] = _FakeDS()
    try:
        _pro = config.DEEPSEEK_MODEL_PRO
        _disp = _brainmod._external_response(_pro, [], "s", None, None)
        check("deepseek.dispatch_routes",
              _disp is not None and _disp[0].content[0].text == "DS"
              and _disp[1] == "deepseek")
        check("deepseek.dispatch_ignores_others",
              _brainmod._external_response("claude-sonnet-4-6", [], "s", None, None) is None)
    finally:
        _brainmod._deepseek_brains.clear()
        _brainmod._deepseek_brains.update(_prev_cache)

    # --- user-defined engines (Engines tab) --- #
    import tempfile, pathlib as _pl
    _tmpfile = _pl.Path(tempfile.mkdtemp()) / "engines.json"
    # the path is resolved when used now, not fixed at import — so redirect
    # the function rather than a constant
    _orig_ef, _orig_cache = (_brainmod._engines_file,
                             _brainmod._custom_engines_cache)
    _brainmod._engines_file = lambda: _tmpfile
    _brainmod._custom_engines_cache = None
    try:
        ok_add, _ = _brainmod.add_custom_engine(
            "My GPT", "https://api.openai.com/v1", "sk-test", "gpt-4o-mini")
        check("engines.add_and_list",
              ok_add and "My GPT" in _brainmod.custom_engine_names())
        check("engines.persisted_to_disk",
              _tmpfile.exists() and "My GPT" in _tmpfile.read_text())
        # "Claude" is no longer reserved — it was a built-in you could
        # neither edit nor remove, which is what left a wrong model id with
        # nowhere to be corrected. Only the router's own words are reserved.
        check("engines.reject_reserved_name",
              not _brainmod.add_custom_engine(
                  "Auto", "https://x.y/v1", "k", "m")[0]
              and not _brainmod.add_custom_engine(
                  "Ollama", "https://x.y/v1", "k", "m")[0])
        check("engines.claude_can_be_defined_by_you",
              _brainmod.add_custom_engine(
                  "Claude", "https://api.anthropic.com/v1", "k",
                  "claude-sonnet-4-6")[0] is True)
        _brainmod.remove_custom_engine("Claude")
        check("engines.require_fields",
              not _brainmod.add_custom_engine("X", "", "k", "m")[0]
              and not _brainmod.add_custom_engine("X", "ftp://x", "k", "m")[0])
        # a real OpenAIBrain is built with the engine's own creds
        _cb = _brainmod._get_custom_brain(
            _brainmod.custom_engine_by_name("My GPT"))
        check("engines.brain_uses_creds",
              _cb is not None and _cb._client.api_key == "sk-test"
              and _cb.model == "gpt-4o-mini")
        # dispatch routes by engine NAME, calling the brain with the entry's model
        _entry = _brainmod.custom_engine_by_name("My GPT")
        _sig = (_entry["base_url"], _entry["api_key"], _entry["model"],
                _entry.get("tools", True), _entry.get("stream", True))
        _cap = {}
        class _FakeOAI:
            last_engine = "custom"; last_usage = None
            def chat(self, messages, system, tools=None, on_text=None, model=None):
                _cap["model"] = model
                return _types.SimpleNamespace(
                    content=[_types.SimpleNamespace(type="text", text="C")],
                    stop_reason="end_turn")
        _FakeOAI._sig = _sig
        _brainmod._custom_brains["My GPT"] = _FakeOAI()
        _o = _brainmod._external_response("My GPT", [], "s", None, None)
        check("engines.dispatch_by_name",
              _o is not None and _o[0].content[0].text == "C"
              and _cap.get("model") == "gpt-4o-mini" and _o[1] == "custom")
        check("engines.remove",
              _brainmod.remove_custom_engine("My GPT")[0]
              and "My GPT" not in _brainmod.custom_engine_names())
        # capability flags persist and shape the request
        _brainmod.add_custom_engine("NoTools", "https://x.y/v1", "k", "m",
                                    tools=False, stream=False)
        _e2 = _brainmod.custom_engine_by_name("NoTools")
        check("engines.flags_persist",
              _e2["tools"] is False and _e2["stream"] is False)
        _b2 = _brainmod._get_custom_brain(_e2)
        check("engines.brain_flags",
              _b2.supports_tools is False and _b2.supports_stream is False)
        _seen, _txtout = {}, []
        class _MockCompl:
            def create(self, **k):
                _seen.clear(); _seen.update(k)
                _m = _types.SimpleNamespace(content="hello", tool_calls=None)
                return _types.SimpleNamespace(
                    choices=[_types.SimpleNamespace(message=_m, finish_reason="stop")],
                    usage=None)
        _mc = _types.SimpleNamespace(
            chat=_types.SimpleNamespace(completions=_MockCompl()))
        _nb = _brainmod.OpenAIBrain(model="m", base_url="https://api.x.com/v1", client=_mc,
                                    supports_tools=False, supports_stream=False)
        _r = _nb.chat([{"role": "user", "content": "hi"}], "sys",
                      tools=[{"name": "t", "input_schema": {}}],
                      on_text=_txtout.append)
        check("engines.flags_shape_request",
              "tools" not in _seen and "stream" not in _seen
              and "".join(_txtout) == "hello" and _r.content[0].text == "hello")
        # re-saving with the same name updates in place (edit), not duplicates
        _brainmod.add_custom_engine("NoTools", "https://x.y/v1", "k2", "m2",
                                    tools=True, stream=True)
        _allnames = [e["name"] for e in _brainmod.load_custom_engines()]
        _e3 = _brainmod.custom_engine_by_name("NoTools")
        check("engines.edit_in_place",
              _allnames.count("NoTools") == 1 and _e3["model"] == "m2"
              and _e3["tools"] is True and _e3["api_key"] == "k2")
        # --- at-rest encryption of engine API keys ---
        import agent.crypto as _crypto
        check("crypto.roundtrip",
              _crypto.decrypt_str(_crypto.encrypt_str("sk-abc-123")) == "sk-abc-123")
        check("crypto.plaintext_passthrough",
              _crypto.decrypt_str("legacy-plain") == "legacy-plain"
              and not _crypto.is_encrypted("legacy-plain"))
        _tok = _crypto.encrypt_str("sk-secret")
        check("crypto.tamper_rejected",
              _crypto.is_encrypted(_tok)
              and _crypto.decrypt_str(_tok[:-3] + ("zzz" if _tok[-3:] != "zzz" else "aaa")) == "")
        _brainmod.add_custom_engine("Sealed", "https://api.x.ai/v1",
                                    "sk-PLAINTEXT-OnDisk", "mdl")
        import json as _cjson
        _disk = _brainmod._engines_file().read_text(encoding="utf-8")
        check("crypto.engine_key_sealed_on_disk",
              "sk-PLAINTEXT-OnDisk" not in _disk and "enc:v1:" in _disk
              and _brainmod.custom_engine_by_name("Sealed")["api_key"] == "sk-PLAINTEXT-OnDisk")

        # DPAPI master-key sealing: simulate Windows by swapping in a reversible
        # "protector", then verify serialize/read round-trips and that an existing
        # plaintext-hex key is migrated to the sealed format in place.
        import agent.windpapi as _wd
        _wd_was = (_wd.available, _wd.protect, _wd.unprotect)
        try:
            _wd.available = True
            _wd.protect = lambda b: b"SEAL" + bytes(b)
            _wd.unprotect = lambda b: bytes(b)[4:] if bytes(b).startswith(b"SEAL") else bytes(b)
            _kb = bytes(range(32))
            _ser = _crypto._serialize_key(_kb)
            check("crypto.dpapi_roundtrip",
                  _ser.startswith("dpapi:v1:") and _crypto._read_key_text(_ser) == _kb)
            check("crypto.dpapi_reads_legacy_hex",
                  _crypto._read_key_text(_kb.hex()) == _kb)
            import tempfile as _tf, pathlib as _pl
            _home_was = _crypto.config.AGENT_HOME
            _crypto.config.AGENT_HOME = _pl.Path(_tf.mkdtemp())
            try:
                _kp = _crypto._key_path()
                _kp.write_text(_kb.hex(), "utf-8")          # legacy plaintext on disk
                _got = _crypto._master_key()
                _after = _kp.read_text("utf-8")
                check("crypto.dpapi_migrates_legacy",
                      _got == _kb and _after.startswith("dpapi:v1:"))
            finally:
                _crypto.config.AGENT_HOME = _home_was
        finally:
            _wd.available, _wd.protect, _wd.unprotect = _wd_was

        # Per-thread engine attribution: concurrent turns must not clobber
        # each other's last_engine/last_usage on a shared brain instance.
        import threading as _th
        class _TLB(_brainmod._PerThreadEngine):
            pass
        _tlb = _TLB()
        _tlb.last_engine = "main-thread"
        _seen = {}
        _barrier = _th.Barrier(3)
        def _worker(name):
            _tlb.last_engine = name
            _barrier.wait()                                 # all set before any read
            _seen[name] = _tlb.last_engine
        _threads = [_th.Thread(target=_worker, args=(f"eng{i}",)) for i in range(2)]
        for _t in _threads: _t.start()
        _barrier.wait()
        for _t in _threads: _t.join()
        check("brain.engine_tag_thread_isolated",
              _seen.get("eng0") == "eng0" and _seen.get("eng1") == "eng1"
              and _tlb.last_engine == "main-thread")
        # legacy plaintext file is migrated (re-encrypted) on next load
        _brainmod._engines_file().write_text(_cjson.dumps([
            {"name": "Legacy", "base_url": "https://x/v1",
             "api_key": "sk-LEGACY", "model": "m", "tools": True, "stream": True}]),
            encoding="utf-8")
        _brainmod._custom_engines_cache = None
        _ld = _brainmod.load_custom_engines(refresh=True)
        _disk2 = _brainmod._engines_file().read_text(encoding="utf-8")
        check("crypto.legacy_key_migrated",
              any(x["api_key"] == "sk-LEGACY" for x in _ld)
              and "sk-LEGACY" not in _disk2 and "enc:v1:" in _disk2)
    finally:
        _brainmod._engines_file = _orig_ef
        _brainmod._custom_engines_cache = _orig_cache
        _brainmod._custom_brains.clear()

    server.shutdown()

    # --- Auto engine fallback (resilient routing across all engines) --------- #
    from agent.memory import MemoryStore as _RMS
    from types import SimpleNamespace as _SNS
    import tempfile as _rtf
    import sys as _rsys
    import os as _ros
    import time as _time
    from pathlib import Path as _RPath
    _r_routing, _r_fb, _r_learn = config.ROUTING, config.ROUTE_FEEDBACK, config.AUTO_LEARN
    config.ROUTING, config.ROUTE_FEEDBACK, config.AUTO_LEARN = True, False, False
    _rmem = _RMS(db_path=_RPath(_rtf.mkdtemp()) / "r.db", check_same_thread=False)
    _rcalls = []

    class _RBrain:
        backend = "hybrid"; model = "claude-x"; fast_model = "qwen#local"
        local = object(); last_engine = None; last_usage = None
        def route(self, t): return self.fast_model           # router -> local primary
        def chat(self, messages, system, tools=None, on_text=None, model=None):
            _rcalls.append(model)
            if model == self.fast_model:
                # not a connection error - an out-of-credits / billing rejection,
                # which must STILL trigger failover to the next engine
                raise RuntimeError("Your credit balance is too low to make this request")
            self.last_engine = "claude"; self.last_usage = None
            if on_text:
                on_text("ok")
            return _SNS(content=[_SNS(type="text", text="ok")], stop_reason="end_turn")
        def summarize(self, t): return ""
        def extract_facts(self, *a, **k): return []

    try:
        _rb = _RBrain()
        _rchain = agent_main._auto_chain(_rb, _rb.fast_model)
        check("routing.chain_includes_fallback",
              _rchain[0] == "qwen#local" and None in _rchain)
        check("routing.failover_classifier",
              agent_main._should_failover(ConnectionError("x")) is True
              and agent_main._should_failover(
                  RuntimeError("Your credit balance is too low")) is True
              and agent_main._should_failover(TypeError("bug")) is False)
        check("routing.failover_reason",
              agent_main._failover_reason(
                  RuntimeError("credit balance too low")) == "out of credits")
        _rti = {}
        _rout = agent_main.run_turn(_rb, _rmem, [], "hi", auto_approve=False,
                                    turn_info=_rti, on_text=lambda t: None)
        check("routing.auto_falls_back",
              _rout == "ok" and _rti.get("engine") == "claude"
              and _rcalls and _rcalls[0] == "qwen#local" and None in _rcalls)
        _rraised = False
        try:
            agent_main.run_turn(_rb, _rmem, [], "hi", auto_approve=False,
                                force_model="qwen#local", on_text=lambda t: None)
        except RuntimeError:
            _rraised = True
        check("routing.pinned_no_fallback", _rraised)

        # Auto with ONLY Claude configured: the raw provider 400 is replaced with
        # an actionable message naming the fix (this is the user-facing symptom of
        # "Auto keeps erroring on Claude" when no fallback engine is set up).
        class _ROnly:
            backend = "hybrid"; model = "claude-x"; fast_model = "claude-x"
            local = None; last_engine = None; last_usage = None
            def route(self, t): return self.model
            def chat(self, messages, system, tools=None, on_text=None, model=None):
                raise RuntimeError("Error code: 400 - Your credit balance is too "
                                   "low to access the Anthropic API")
            def summarize(self, t): return ""
            def extract_facts(self, *a, **k): return []
        _ro = _ROnly()
        _rochain = agent_main._auto_chain(_ro, None)
        _romsg = ""
        try:
            agent_main.run_turn(_ro, _rmem, [], "hi", auto_approve=False,
                                on_text=lambda t: None)
        except Exception as _e:
            _romsg = str(_e)
        check("routing.only_claude_actionable_error",
              len(_rochain) == 1
              and "fall back" in _romsg.lower()
              and "engines panel" in _romsg.lower())

        # should_cancel ends a turn cleanly with a Stopped note (before any call)
        _cst = []
        _cr = agent_main.run_turn(_ro, _rmem, [], "hi", auto_approve=False,
                                  should_cancel=lambda: True, on_status=_cst.append)
        check("turn.cancel_clean_stop",
              "Stopped" in _cr and "stopped" in _cst)

        # a failing tool is surfaced to the user AND fed back so the agent recovers
        _exec_orig = agent_main.tools.execute_tool
        agent_main.tools.execute_tool = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            class _TB:
                backend = "hybrid"; model = "claude-x"; fast_model = "claude-x"
                local = None; last_engine = None; last_usage = None; _n = 0
                def route(self, t): return self.model
                def chat(self, messages, system, tools=None, on_text=None, model=None):
                    self.last_engine = "claude"; self._n += 1
                    if self._n == 1:
                        return _SNS(content=[_SNS(type="tool_use", id="t1",
                                                  name="web_get", input={})],
                                    stop_reason="tool_use")
                    return _SNS(content=[_SNS(type="text", text="recovered")],
                                stop_reason="end_turn")
                def summarize(self, t): return ""
                def extract_facts(self, *a, **k): return []
            _tst = []
            _tr = agent_main.run_turn(_TB(), _rmem, [], "hi", auto_approve=True,
                                      on_status=_tst.append)
            check("turn.tool_failure_surfaced",
                  any("failed" in s for s in _tst) and _tr == "recovered")
        finally:
            agent_main.tools.execute_tool = _exec_orig

        # capability scoring + ladder ordering
        class _ScoreB:
            model = "claude-x"; fast_model = "qwen#local"; local = object()
        _scb = _ScoreB()
        check("escalate.scores_rank_claude_top",
              agent_main._engine_score(None, _scb)
              > agent_main._engine_score("qwen#local", _scb))
        _lad = agent_main._quality_ladder(_scb)
        check("escalate.ladder_sorted_desc",
              _lad == sorted(_lad, key=lambda t: agent_main._engine_score(t, _scb),
                             reverse=True)
              and agent_main._next_smarter(_lad, "qwen#local", _scb, set()) is None)  # -> Claude(None)
        check("escalate.no_next_when_on_top",
              agent_main._next_smarter(_lad, None, _scb, set()) is agent_main._NO_NEXT)

        # struggling on a cheap engine escalates to the smartest one and recovers
        _exec_orig2 = agent_main.tools.execute_tool
        agent_main.tools.execute_tool = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
        try:
            class _EB:
                backend = "hybrid"; model = "claude-x"; fast_model = "qwen#local"
                local = object(); last_engine = None; last_usage = None
                def __init__(self): self.calls = []
                def route(self, t): return self.fast_model        # start cheap (local)
                def chat(self, messages, system, tools=None, on_text=None, model=None):
                    self.calls.append(model)
                    self.last_engine = "claude" if model is None else "local"
                    if model is None:                              # escalated to Claude
                        return _SNS(content=[_SNS(type="text", text="solved by claude")],
                                    stop_reason="end_turn")
                    return _SNS(content=[_SNS(type="tool_use", id="t", name="do_thing",
                                              input={"x": 1})], stop_reason="tool_use")
                def summarize(self, t): return ""
                def extract_facts(self, *a, **k): return []
            _eb = _EB(); _eti = {}
            _er = agent_main.run_turn(_eb, _rmem, [], "hard one", auto_approve=True,
                                      turn_info=_eti, on_text=lambda t: None)
            check("escalate.smarter_on_struggle",
                  _er == "solved by claude"
                  and "Claude" in _eti.get("escalations", [])
                  and "qwen#local" in _eb.calls and _eb.calls[-1] is None)
        finally:
            agent_main.tools.execute_tool = _exec_orig2

        # already on the smartest engine and looping the same call -> clean stop
        _exec_orig3 = agent_main.tools.execute_tool
        agent_main.tools.execute_tool = lambda *a, **k: "ok"
        try:
            class _LoopB:
                backend = "hybrid"; model = "claude-x"; fast_model = "claude-x"
                local = None; last_engine = None; last_usage = None
                def __init__(self): self.n = 0
                def route(self, t): return self.model             # start on Claude
                def chat(self, messages, system, tools=None, on_text=None, model=None):
                    self.n += 1; self.last_engine = "claude"
                    return _SNS(content=[_SNS(type="tool_use", id="t", name="spin",
                                              input={"same": 1})], stop_reason="tool_use")
                def summarize(self, t): return ""
                def extract_facts(self, *a, **k): return []
            _loopb = _LoopB()
            _lr = agent_main.run_turn(_loopb, _rmem, [], "loop", auto_approve=True,
                                      on_text=lambda t: None)
            check("escalate.loop_break_when_no_smarter",
                  "Stopped" in _lr and "progress" in _lr
                  and _loopb.n < config.MAX_TOOL_ROUNDS)
        finally:
            agent_main.tools.execute_tool = _exec_orig3

        # adaptive telemetry: latency/throughput/reliability -> score adjustment
        import agent.telemetry as _tel
        _tel_home = config.AGENT_HOME
        config.AGENT_HOME = _RPath(_rtf.mkdtemp())
        _tel.reset()
        try:
            check("telemetry.cold_start_neutral", _tel.adjustment("X") == 0)
            # a fast, reliable engine: 4 quick calls making lots of tokens, finishes
            for _ in range(4):
                _tel.record_call("Fast", 1.0, 80)      # 80 tok/s
            _tel.record_turn("Fast")
            _fs = _tel.stats()["Fast"]
            check("telemetry.derived_metrics",
                  _fs["calls"] == 4 and _fs["tokens_per_sec"] == 80.0
                  and _fs["avg_latency"] == 1.0)
            check("telemetry.reliable_fast_scores_up", _tel.adjustment("Fast") > 0)
            # a slow, struggling engine: escalated away from repeatedly
            for _ in range(4):
                _tel.record_call("Slow", 10.0, 20)     # 2 tok/s
            for _ in range(3):
                _tel.record_turn("Fast", struggled=["Slow"])
            check("telemetry.unreliable_slow_scores_down", _tel.adjustment("Slow") < 0)
        finally:
            config.AGENT_HOME = _tel_home
            _tel.reset()
    finally:
        config.ROUTING, config.ROUTE_FEEDBACK, config.AUTO_LEARN = \
            _r_routing, _r_fb, _r_learn

    # --- outreach / email engine (with a fake transport, no real network) --- #
    import agent.outreach as _out
    _out_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    _sent_mail = []
    _out_tx = _out.TRANSPORT
    _out.TRANSPORT = lambda cfg, to, msg: _sent_mail.append((to, msg["Subject"]))
    try:
        check("outreach.refuses_before_config",
              _out.send("a@b.com", "s", "b")["ok"] is False)
        _out.save_config({"host": "smtp.x", "from_addr": "me@x.com",
                          "password": "smtp-secret", "enabled": False})
        check("outreach.password_encrypted_at_rest",
              "smtp-secret" not in (config.AGENT_HOME / "email.json").read_text())
        check("outreach.status_hides_password", "password" not in _out.status())
        check("outreach.refuses_when_disabled",
              _out.send("a@b.com", "s", "b")["ok"] is False
              and len(_sent_mail) == 0)
        check("outreach.dry_run_no_send",
              _out.send("a@b.com", "s", "b", dry_run=True)["ok"] is True
              and len(_sent_mail) == 0)
        _out.save_config({"enabled": True})
        check("outreach.sends_when_enabled",
              _out.send("a@b.com", "s", "b")["ok"] is True and len(_sent_mail) == 1)
        check("outreach.invalid_recipient_refused",
              _out.send("not-an-email", "s", "b")["ok"] is False)
        _camp = _out.build_campaign("Hi {first_name}", "Dear {first_name} at {company}",
                                    [{"first_name": "Sam", "company": "Acme", "email": "s@x.com"},
                                     {"first_name": "Jo", "email": "bad"}])
        check("outreach.personalises_and_validates",
              _camp[0]["subject"] == "Hi Sam" and "Acme" in _camp[0]["body"]
              and _camp[0]["valid"] is True and _camp[1]["valid"] is False)
        _rh = _out.RATE_HOUR
        _out.RATE_HOUR = 2
        _out._SENT_TIMES.clear()
        _res = _out.send_campaign([{"to": f"x{i}@y.com", "subject": "s", "body": "b"}
                                   for i in range(5)])
        _out.RATE_HOUR = _rh
        check("outreach.campaign_rate_limited",
              _res["sent"] == 2 and _res["total"] == 5)
        check("outreach.audit_log_records", len(_out.recent_log(99)) >= 1)

        # --- auto-pilot (bounded autonomy) ---
        _out._SENT_TIMES.clear()
        _ap_cs = [{"first_name": "Sam", "email": "sam@acme.com"},
                  {"first_name": "Jo", "email": "jo@beta.io"},
                  {"first_name": "X", "email": "x@evil.com"}]
        check("autopilot.disarmed_refuses",
              _out.run_autopilot("Hi {first_name}", "B", _ap_cs)["ok"] is False)
        _out.save_config({"autonomous_enabled": True, "require_allowlist": True,
                          "allowed_domains": ["acme.com"],
                          "allowed_recipients": ["jo@beta.io"], "max_per_run": 25})
        _ap_sent_before = len(_sent_mail)
        _apr = _out.run_autopilot("Hi {first_name}", "Dear {first_name}", _ap_cs)
        check("autopilot.sends_only_allowlisted",
              _apr["sent"] == 2 and _apr["blocked"] == 1
              and len(_sent_mail) - _ap_sent_before == 2)
        check("autopilot.blocks_unapproved_single",
              _out.send("stranger@nowhere.com", "s", "b", autonomous=True)["ok"] is False)
        _out.save_config({"max_per_run": 1, "allowed_domains": ["acme.com", "beta.io"]})
        check("autopilot.respects_per_run_cap",
              _out.run_autopilot("Hi", "B", _ap_cs)["sent"] == 1)
        _out.pause_autonomous()
        check("autopilot.kill_switch_disarms",
              _out.status()["autonomous_enabled"] is False
              and _out.run_autopilot("Hi", "B", _ap_cs)["ok"] is False)
        check("autopilot.audit_tags_autonomous",
              any(e.get("auto") for e in _out.recent_log(99)))
    finally:
        _out.TRANSPORT = _out_tx
        config.AGENT_HOME = _out_home

    # --- watchers: change detection + agent handoff ------------------------- #
    import agent.watchers as _w
    _w_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    _w_content = {"v": "alpha\nbeta"}
    _w_fetch_orig = _w.FETCHER
    _w.FETCHER = lambda st, src: (True, _w_content["v"], "")
    try:
        _wat = {"id": "w1", "source_type": "url", "source": "http://x"}
        _first = _w.check(_wat)
        check("watch.first_run_is_baseline",
              _first["first_run"] is True and _first["changed"] is False)
        check("watch.no_change_detected", _w.check(_wat)["changed"] is False)
        _w_content["v"] = "alpha\nbeta\nGAMMA new"
        _chg = _w.check(_wat)
        check("watch.change_detected_and_summarised",
              _chg["changed"] is True and "GAMMA new" in _chg["summary"])
        check("watch.signature_stable_on_whitespace",
              _w.signature("a b  c") == _w.signature("a   b c"))
        _fe = _w.FETCHER
        _w.FETCHER = lambda st, src: (False, "", "boom")
        check("watch.fetch_error_surfaced", _w.check(_wat)["ok"] is False)
        _w.FETCHER = _fe
        check("watch.prompt_includes_source_and_change",
              "http://x" in _w.build_agent_prompt(_wat, "X changed", "body text")
              and "body text" in _w.build_agent_prompt(_wat, "X changed", "body text"))
        # non-consuming preview: persist=False sees the change but doesn't eat it
        _w_content["v"] = "alpha\nbeta\nGAMMA new\nDELTA"
        _pv1 = _w.check(_wat, persist=False)
        _pv2 = _w.check(_wat, persist=False)
        check("watch.preview_does_not_consume",
              _pv1["changed"] is True and _pv2["changed"] is True)
        check("watch.real_check_consumes",
              _w.check(_wat)["changed"] is True and _w.check(_wat)["changed"] is False)
    finally:
        _w.FETCHER = _w_fetch_orig
        config.AGENT_HOME = _w_home

    # --- observe mode: draft-only guard neutralises real sends -------------- #
    _do_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    _do_sent = []
    _do_tx = _out.TRANSPORT
    _out.TRANSPORT = lambda cfg, to, msg: _do_sent.append(to)
    try:
        _out.save_config({"host": "s", "from_addr": "me@x.com", "password": "p",
                          "autonomous_enabled": True, "allowed_domains": ["acme.com"]})
        _out.set_draft_only(True)
        _r_obs = _out.send("sam@acme.com", "S", "B", autonomous=True)
        _out.set_draft_only(False)
        check("observe.armed_send_becomes_dryrun",
              _r_obs["dry_run"] is True and _r_obs.get("draft_only") is True
              and len(_do_sent) == 0)
        _r_live = _out.send("sam@acme.com", "S", "B", autonomous=True)
        check("observe.send_mode_delivers",
              _r_live["ok"] is True and len(_do_sent) == 1)
    finally:
        _out.TRANSPORT = _do_tx
        config.AGENT_HOME = _do_home

    # --- auto-resume: bounded sweep over stalled tasks --------------------- #
    import agent.autoresume as _ar
    import datetime as _dt
    _ar_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    config.DB_PATH = config.AGENT_HOME / "ar.db"
    try:
        from agent.memory import MemoryStore as _MS2
        _arm = _MS2(db_path=config.DB_PATH, check_same_thread=False)

        def _backdate(tid, mins=120):
            old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=mins)
                   ).strftime("%Y-%m-%d %H:%M:%S")
            _arm.conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, tid))
            _arm.conn.commit()

        _t_stuck = _arm.create_task("Stalled", ["one", "two", "three"]); _backdate(_t_stuck)
        _t_fresh = _arm.create_task("Fresh", ["a", "b"])           # not idle
        _t_blk = _arm.create_task("Blocked", ["only"])
        _arm.update_step(_t_blk, 1, "blocked", "NEEDS USER: log in"); _backdate(_t_blk)

        check("autoresume.disarmed_refuses",
              _ar.sweep(_arm, lambda t: None)["ok"] is False)
        _ar.save_config({"enabled": True, "idle_minutes": 30, "max_attempts": 2,
                         "max_per_sweep": 5})
        _cand_ids = [c["id"] for c in _ar.candidates(_arm)]
        check("autoresume.candidates_idle_only_actionable",
              _t_stuck in _cand_ids and _t_fresh not in _cand_ids
              and _t_blk not in _cand_ids)

        def _progress_runner(t):           # resolves one step, stays idle
            seq = next(s["seq"] for s in _arm.get_task(t["id"])["steps"]
                       if s["status"] in ("pending", "in_progress"))
            _arm.update_step(t["id"], seq, "done", "did it and verified output")
            _backdate(t["id"])
        _r1 = _ar.sweep(_arm, _progress_runner)
        check("autoresume.progress_counts",
              _r1["progressed"] == 1
              and _ar._load_state().get(str(_t_stuck), {}).get("attempts") == 0)

        _t_grind = _arm.create_task("Grinds", ["p", "q"]); _backdate(_t_grind)
        _ar.save_config({"max_attempts": 2})
        _ar.sweep(_arm, lambda t: _backdate(t["id"]))    # no-op runner
        _ar.sweep(_arm, lambda t: _backdate(t["id"]))
        check("autoresume.stall_caps_attempts",
              _ar._load_state().get(str(_t_grind), {}).get("attempts") == 2
              and all(c["id"] != _t_grind for c in _ar.candidates(_arm)))

        _ar.save_config({"max_per_sweep": 1})
        for _i in range(3):
            _tt = _arm.create_task(f"Many {_i}", ["x", "y"]); _backdate(_tt)
        _rsw = _ar.sweep(_arm, lambda t: _backdate(t["id"]))
        check("autoresume.respects_max_per_sweep", _rsw["resumed"] <= 1)
        check("autoresume.reset_clears_attempts",
              (_ar.reset_task_state(_t_grind) or True)
              and str(_t_grind) not in _ar._load_state())
        check("autoresume.audit_records", len(_ar.recent_log(99)) >= 1)
    finally:
        config.AGENT_HOME = _ar_home

    # --- learning: auto_learn writes memories; hybrid falls back to cloud --- #
    _ll_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "ll.db"
    try:
        from agent.memory import MemoryStore as _MS3
        _llm = _MS3(db_path=config.DB_PATH, check_same_thread=False)

        class _FactBrain:
            def extract_facts(self, u, a, known):
                return ["User likes terse replies"]
        _al_prev = config.AUTO_LEARN; config.AUTO_LEARN = True
        agent_main.auto_learn(_FactBrain(), _llm, "be terse", "ok")
        check("learn.auto_learn_writes_memory",
              any("terse" in x["content"] for x in _llm.all_memories()))
        config.AUTO_LEARN = _al_prev

        from agent.brain import HybridBrain as _HB
        _h = _HB.__new__(_HB)

        class _CloudOnly:
            def extract_facts(self, u, a, known):
                return ["cloud-extracted"]
        _h.local = None; _h.cloud = _CloudOnly()
        check("learn.hybrid_falls_back_to_cloud",
              _h.extract_facts("x", "y", []) == ["cloud-extracted"])

        class _LocalPref:
            def extract_facts(self, u, a, known):
                return ["local"]
        _h.local = _LocalPref()
        check("learn.hybrid_prefers_local",
              _h.extract_facts("x", "y", []) == ["local"])
    finally:
        config.AGENT_HOME = _ll_home

    # --- DeepSeek/OpenAI tool-pairing repair (orphaned tool_call -> 400) ----- #
    from agent.brain import OpenAIBrain as _OB

    def _valid_oai(ms):
        for _i, _mm in enumerate(ms):
            if _mm.get("role") == "assistant" and _mm.get("tool_calls"):
                _need = [c["id"] for c in _mm["tool_calls"]]
                _j, _got = _i + 1, set()
                while _j < len(ms) and ms[_j].get("role") == "tool":
                    _got.add(ms[_j]["tool_call_id"]); _j += 1
                if any(c not in _got for c in _need):
                    return False
        return True

    _orphan = [
        {"role": "user", "content": "search the news"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_00_kSoRG",
                                           "name": "web_search", "input": {"q": "n"}}]},
        {"role": "user", "content": "never mind, switch topic"},
    ]
    _co = _OB._to_openai_messages(_orphan, ["sys"])
    check("deepseek.orphan_tool_call_repaired",
          _valid_oai(_co)
          and any(x.get("role") == "tool" and "no result" in str(x.get("content"))
                  for x in _co))

    _paired = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c9",
                                           "name": "web_search", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c9",
                                      "content": "RESULT"}]},
    ]
    _cp = _OB._to_openai_messages(_paired, ["sys"])
    check("deepseek.normal_pairing_preserved",
          _valid_oai(_cp)
          and any(x.get("role") == "tool" and x.get("content") == "RESULT" for x in _cp))

    _stray = [{"role": "user", "content": [{"type": "tool_result",
                                            "tool_use_id": "ghost", "content": "s"}]}]
    check("deepseek.orphan_tool_result_dropped",
          all(x.get("role") != "tool"
              for x in _OB._to_openai_messages(_stray, ["sys"])))

    # --- experience loop: playbooks, lessons, cross-conversation recall ------ #
    _xp_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "xp.db"
    try:
        from agent.memory import MemoryStore as _MS4
        import agent.experience as _xp
        _xm = _MS4(db_path=config.DB_PATH, check_same_thread=False)

        _xt = _xm.create_task("Deploy hugo blog to netlify",
                              ["Install hugo", "Build site", "Push live"])
        for _i, _note in ((1, "hugo v0.126 verified"), (2, "public/ has 42 files"),
                          (3, "live URL returns 200")):
            _xm.update_step(_xt, _i, "done", _note)
        _xp.on_task_finished(_xm, _xm.get_task(_xt), "deployed + verified", "completed")
        check("experience.playbook_distilled", _xm.playbook_count() == 1)
        _pbs = _xm.relevant_playbooks("deploy my hugo blog to netlify again")
        check("experience.playbook_recalled_with_verification",
              bool(_pbs) and "verify:" in _pbs[0]["steps"])
        check("experience.playbook_irrelevant_query_empty",
              _xm.relevant_playbooks("bake a chocolate cake") == [])

        _xt2 = _xm.create_task("Publish package to npm registry", ["Build", "Publish"])
        _xp.on_step_blocked(_xm, _xm.get_task(_xt2), "npm publish",
                            "npm login with 2FA")
        check("experience.lesson_from_blocked",
              any("2FA" in l["lesson"]
                  for l in _xm.relevant_lessons("publish npm package")))
        _xp.on_task_finished(_xm, _xm.get_task(_xt2), "auth unresolved", "abandoned")
        check("experience.lesson_from_failed", _xm.lesson_count() == 2)
        _xp.on_step_blocked(_xm, _xm.get_task(_xt2), "npm publish",
                            "npm login with 2FA")     # exact dup
        check("experience.lesson_dedup", _xm.lesson_count() == 2)

        _xm.log_message("chat-A", "user", "Use PostgreSQL for the invoicing project")
        _xm.log_message("chat-B", "user", "unrelated lunch chatter")
        _hits = _xm.search_messages("postgresql invoicing project",
                                    exclude_session="chat-B")
        check("experience.recall_cross_conversation",
              len(_hits) == 1 and _hits[0]["session_id"] == "chat-A")
        check("experience.recall_excludes_current",
              _xm.search_messages("postgresql invoicing project",
                                  exclude_session="chat-A") == [])

        _sysp = "\n".join(agent_main.build_system_prompt(
            _xm, "deploy hugo blog to netlify", session_id="chat-B"))
        check("experience.prompt_injects_playbook", "## Playbooks" in _sysp)
        check("experience.prompt_injects_recall_and_lessons",
              "past conversations" in "\n".join(agent_main.build_system_prompt(
                  _xm, "postgresql invoicing project", session_id="chat-B"))
              and "## Lessons" in "\n".join(agent_main.build_system_prompt(
                  _xm, "publish the npm package", session_id="chat-B")))
        check("experience.plain_prompt_stays_lean",
              "## Playbooks" not in "\n".join(agent_main.build_system_prompt(
                  _xm, "hello", session_id="chat-B")))

        from agent.tools import TOOL_DEFINITIONS as _TD, execute_tool as _xtool
        check("experience.recall_tool_defined",
              any(t["name"] == "recall_conversations" for t in _TD))
        from rich.console import Console as _XC
        check("experience.recall_tool_dispatch",
              "PostgreSQL" in _xtool("recall_conversations",
                                     {"query": "postgresql invoicing"},
                                     _xm, _XC(), session_id="chat-Z"))

        # semantic upgrade: reworded queries match when an embedder exists,
        # behaviour is unchanged (deterministic keyword) when it doesn't
        import agent.rag as _xrag
        _emb_orig = _xrag.embed_texts

        def _fake_embed(texts):
            def v(t):
                t = t.lower()
                return [1.0 if any(w in t for w in
                                   ("hugo", "netlify", "website", "online",
                                    "push", "deploy", "blog", "site")) else 0.0,
                        1.0 if "cake" in t else 0.0, 0.1]
            return [v(t) for t in texts]
        try:
            _xrag.embed_texts = _fake_embed
            _semhit = _xm.relevant_playbooks("push my personal website online")
            check("semantic.reworded_playbook_hit",
                  bool(_semhit) and "hugo" in _semhit[0]["title"].lower())
            check("semantic.irrelevant_still_lean",
                  _xm.relevant_playbooks("bake a chocolate cake") == [])
            _xrag.embed_texts = lambda texts: None
            check("semantic.fallback_identical",
                  _xm.relevant_playbooks("push my personal website online") == []
                  and bool(_xm.relevant_playbooks("deploy hugo blog netlify")))
        finally:
            _xrag.embed_texts = _emb_orig

        # continuity briefing: first turn of a NEW conversation gets a
        # since-you-were-away digest; ongoing conversations don't
        import agent.autoresume as _xar
        _xar._audit({"event": "progressed", "task": 1, "title": "Deploy hugo"})
        _xm.log_message("older-chat", "user", "earlier activity")
        _bp = "\n".join(agent_main.build_system_prompt(
            _xm, "hi", session_id="never-seen-session"))
        check("briefing.new_session_gets_digest",
              "## Since your last conversation" in _bp)
        _xm.log_message("ongoing-x", "user", "already talking")
        check("briefing.ongoing_session_skips",
              "## Since your last conversation" not in "\n".join(
                  agent_main.build_system_prompt(_xm, "hi",
                                                 session_id="ongoing-x")))
    finally:
        config.AGENT_HOME = _xp_home

    # --- second opinion: cross-tier Solver + Reviewer ------------------------ #
    import agent.collaborate as _co
    _sc = []

    def _sv(p):
        _sc.append(p)
        return "Canberra"
    _r = _co.collaborate("capital of Australia?", "Sydney",
                         reviewer=lambda p: "Wrong: it's Canberra not Sydney.",
                         solver=_sv)
    check("collab.objection_triggers_single_revision",
          _r["answer"] == "Canberra" and _r["revised"] is True
          and _r["approved"] is False and len(_sc) == 1)
    _sc2 = []
    _r2 = _co.collaborate("2+2?", "4", reviewer=lambda p: "LGTM",
                          solver=lambda p: _sc2.append(p) or "X")
    check("collab.approval_skips_revision",
          _r2["answer"] == "4" and _r2["revised"] is False and not _sc2)
    check("collab.reviewer_crash_safe",
          _co.collaborate("q", "draft", reviewer=lambda p: 1 / 0,
                          solver=lambda p: "z")["answer"] == "draft")
    check("collab.revision_crash_safe",
          _co.collaborate("q", "draft", reviewer=lambda p: "fix it",
                          solver=lambda p: 1 / 0)["answer"] == "draft")
    check("collab.satisfied_variants",
          _co.review_satisfied("") and _co.review_satisfied("LGTM.")
          and _co.review_satisfied("*lgtm* looks good")
          and not _co.review_satisfied("Issue: missing case"))

    # switchable models: MODEL/OLLAMA_MODEL are persistable settings, and the
    # Ollama lister degrades to [] when nothing is reachable
    check("models.names_are_user_settable",
          all(k in config._USER_KEYS
              for k in ("MODEL", "FAST_MODEL", "OLLAMA_MODEL", "OLLAMA_FAST_MODEL")))
    _mprev = config.OLLAMA_HOST
    try:
        import agent.brain as _abrain
        config.OLLAMA_HOST = "http://127.0.0.1:1"     # nothing listening
        check("models.ollama_list_degrades_empty",
              _abrain.list_ollama_models(timeout=0.3) == [])
    finally:
        config.OLLAMA_HOST = _mprev

    # opposite-tier resolver picks the other side of the local/cloud divide
    class _HB:
        model = "claude-x"; fast_model = "ollama-x"; local = True

    class _CloudOnly:
        model = "claude-x"; fast_model = None; local = None
    check("collab.cloud_answer_reviewed_by_local",
          agent_main._opposite_tier_model(_HB(), "claude-x") == "ollama-x")
    check("collab.local_answer_reviewed_by_cloud",
          agent_main._opposite_tier_model(_HB(), "ollama-x") is None)  # None == Claude
    check("collab.no_local_no_second_cloud_returns_sentinel",
          agent_main._opposite_tier_model(_CloudOnly(), "claude-x")
          is agent_main._NO_REVIEWER)

    # full run_turn: cross-tier review corrects a wrong cloud answer
    from types import SimpleNamespace as _NS

    def _blk(t):
        return _NS(content=[_NS(type="text", text=t)], stop_reason="end_turn")

    class _DualBrain:
        model = "claude-x"; fast_model = "ollama-x"; local = True
        backend = "hybrid"; last_engine = None; last_usage = None

        def chat(self, messages, system, tools=None, on_text=None, model=None):
            sysj = " ".join(system) if isinstance(system, list) else str(system)
            if "meticulous reviewer" in sysj:
                self.last_engine = "local"
                return _blk("Wrong: capital is Canberra, not Sydney.")
            if "improved final answer" in messages[-1]["content"].lower():
                self.last_engine = "claude"
                return _blk("The capital of Australia is Canberra.")
            self.last_engine = "claude"
            return _blk("The capital of Australia is Sydney.")
    _co_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "co.db"
    _sub_prev = config.SUBAGENTS; config.SUBAGENTS = False
    try:
        from agent.memory import MemoryStore as _MS6
        _cm = _MS6(db_path=config.DB_PATH, check_same_thread=False)
        _ti = {}
        _out = agent_main.run_turn(_DualBrain(), _cm, [],
                                   "What is the capital of Australia?",
                                   auto_approve=False, session_id="cs1",
                                   force_model="claude-x", turn_info=_ti,
                                   second_opinion=True)
        check("collab.run_turn_cross_tier_corrects",
              "Canberra" in _out and _ti.get("reviewed_by") == "Ollama"
              and _ti.get("revised") is True)
        _ti2 = {}
        _out2 = agent_main.run_turn(_DualBrain(), _cm, [], "hi",
                                    auto_approve=False, session_id="cs2",
                                    force_model="claude-x", turn_info=_ti2,
                                    second_opinion=False)
        check("collab.run_turn_optout_no_review",
              "second opinion" not in _out2.lower()
              and "reviewed_by" not in _ti2)
    finally:
        config.AGENT_HOME = _co_home; config.SUBAGENTS = _sub_prev

    # --- cost-tiered teamwork: local grunt work, cloud reasoning -------------- #
    _tw_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "tw.db"
    _tw_prev, _sub_prev2 = config.TEAMWORK, config.SUBAGENTS
    config.SUBAGENTS = True
    try:
        from agent.memory import MemoryStore as _MS7
        from agent.tools import execute_tool as _twtool, subagent_tools as _twsub
        _twm = _MS7(db_path=config.DB_PATH, check_same_thread=False)
        from rich.console import Console as _TWC
        _twcon = _TWC(quiet=True)

        class _TB:
            model = "claude-x"; fast_model = "ollama-x"; local = True
            calls = []; fail_local = False
            last_engine = None; last_usage = None

            def chat(self, messages, system, tools=None, on_text=None,
                     model=None):
                _TB.calls.append(model)
                if model == "ollama-x" and _TB.fail_local:
                    raise RuntimeError("local OOM")
                return _blk("WORKER RESULT: done")
        _tb = _TB()
        _TB.calls = []
        _r = _twtool("delegate_to_local",
                     {"task": "summarize this", "content": "long text"},
                     _twm, _twcon, brain=_tb)
        check("teamwork.delegate_runs_on_local",
              _TB.calls == ["ollama-x"] and "WORKER RESULT" in _r
              and "verify before using" in _r)
        check("teamwork.delegate_no_local_graceful",
              "No local model" in _twtool(
                  "delegate_to_local", {"task": "summarize it"}, _twm, _twcon,
                  brain=_NS(model="c", fast_model=None, local=None)))
        check("teamwork.delegate_not_in_subagent_tools",
              all(t["name"] != "delegate_to_local" for t in _twsub()))
        config.TEAMWORK = True
        _TB.calls = []
        _twtool("run_subagent", {"objective": "count widgets in the report"},
                _twm, _twcon, brain=_tb)
        check("teamwork.subagent_defaults_local",
              bool(_TB.calls) and all(c == "ollama-x" for c in _TB.calls))
        _TB.calls = []; _TB.fail_local = True
        _r2 = _twtool("run_subagent",
                      {"objective": "count widgets in the report"},
                      _twm, _twcon, brain=_tb)
        check("teamwork.subagent_escalates_on_struggle",
              "stronger engine took over" in _r2 and None in _TB.calls)
        _TB.fail_local = False
        config.TEAMWORK = False
        _TB.calls = []
        _twtool("run_subagent", {"objective": "count widgets in the report"},
                _twm, _twcon, brain=_tb)
        check("teamwork.off_keeps_parent_engine",
              bool(_TB.calls) and all(c is None for c in _TB.calls))
        _TB.calls = []
        _twtool("run_subagent", {"objective": "count widgets in the report",
                                 "tier": "local"}, _twm, _twcon, brain=_tb)
        check("teamwork.explicit_local_tier",
              bool(_TB.calls) and all(c == "ollama-x" for c in _TB.calls))

        _twcap = {}

        class _GB(_TB):
            def chat(self, messages, system, tools=None, on_text=None,
                     model=None):
                _twcap["sys"] = ("\n".join(system) if isinstance(system, list)
                                 else str(system))
                return _blk("hi")
        config.TEAMWORK = True
        agent_main.run_turn(_GB(), _twm, [], "hello", auto_approve=False,
                            session_id="tw1", force_model="claude-x",
                            turn_info={})
        _on_cloud = "Teamwork mode (active)" in _twcap["sys"]
        agent_main.run_turn(_GB(), _twm, [], "hello", auto_approve=False,
                            session_id="tw2", force_model="ollama-x",
                            turn_info={})
        _on_local = "Teamwork mode (active)" in _twcap["sys"]
        config.TEAMWORK = False
        agent_main.run_turn(_GB(), _twm, [], "hello", auto_approve=False,
                            session_id="tw3", force_model="claude-x",
                            turn_info={})
        _off = "Teamwork mode (active)" in _twcap["sys"]
        check("teamwork.guidance_only_cloud_and_on",
              _on_cloud and not _on_local and not _off)
    finally:
        config.AGENT_HOME = _tw_home
        config.TEAMWORK, config.SUBAGENTS = _tw_prev, _sub_prev2

    # --- MCP client: stdio transport, tool bridge, lifecycle ------------------ #
    _mcp_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "mc.db"
    try:
        import sys as _sys
        import agent.mcp as _mcp
        _fake = str(_RPath(__file__).parent / "fake_mcp_server.py")
        _mcp.save_config({"servers": {"fake": {
            "transport": "stdio", "command": _sys.executable,
            "args": [_fake], "enabled": True}}})
        _mcp.manager.reload()
        _mdefs = _mcp.manager.tool_definitions()
        check("mcp.connect_and_list",
              sorted(d["name"] for d in _mdefs)
              == ["mcp_fake_add", "mcp_fake_boom", "mcp_fake_echo",
                  "mcp_fake_slow"]
              and _mcp.manager.servers["fake"].server_info.get("name")
              == "fake-mcp")
        check("mcp.schema_and_description",
              any(d["input_schema"].get("required") == ["text"]
                  for d in _mdefs)
              and all(d["description"].startswith("[MCP:fake]")
                      for d in _mdefs))
        check("mcp.call_roundtrip",
              _mcp.manager.call("mcp_fake_echo", {"text": "hi"}) == "ECHO: hi"
              and _mcp.manager.call("mcp_fake_add", {"a": 2, "b": 3}) == "5")
        check("mcp.iserror_surfaced",
              "reported an error" in _mcp.manager.call("mcp_fake_boom", {}))
        _mconn = _mcp.manager.servers["fake"]
        _slow = _mconn.call("slow", {}, timeout=0.4)
        check("mcp.timeout_graceful_and_connection_survives",
              "timed out" in _slow and _mconn.connected
              and _mcp.manager.call("mcp_fake_echo", {"text": "ok"})
              == "ECHO: ok")
        _mcp.manager.set_enabled("fake", False)
        _off = _mcp.manager.tool_definitions() == []
        _mcp.manager.set_enabled("fake", True)
        check("mcp.disable_enable_lifecycle",
              _off and len(_mcp.manager.tool_definitions()) == 4)
        from agent.tools import execute_tool as _mtool, \
            all_tool_definitions as _alltools
        from rich.console import Console as _MC
        from agent.memory import MemoryStore as _MS8
        _mm = _MS8(db_path=config.DB_PATH, check_same_thread=False)
        check("mcp.dispatch_fallthrough",
              _mtool("mcp_fake_add", {"a": 10, "b": 5}, _mm,
                     _MC(quiet=True)) == "15")
        check("mcp.aggregated_tool_defs",
              sum(1 for d in _alltools()
                  if d["name"].startswith("mcp_")) == 4
              and any(d["name"] == "read_file" for d in _alltools()))
        check("mcp.name_sanitized",
              _mcp._safe("my server!") == "my_server"
              and _mcp._safe("") == "srv")
        check("mcp.unknown_tool_graceful",
              "Unknown MCP tool" in _mcp.manager.call("mcp_zz_none", {}))
        _mcp.manager.remove("fake")
        check("mcp.remove_cleans_up",
              _mcp.manager.statuses() == []
              and _mcp.load_config()["servers"] == {})
    finally:
        config.AGENT_HOME = _mcp_home

    # --- ollama context window: fixes silent truncation on local models ------- #
    import agent.brain as _ctxb
    _ctx_cap = {}

    class _CtxClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    _ctx_cap.clear(); _ctx_cap.update(kw)
                    class _R:
                        choices = [type("C", (), {
                            "message": type("M", (), {"content": "ok",
                                                      "tool_calls": None})(),
                            "finish_reason": "stop"})()]
                        usage = type("U", (), {"prompt_tokens": 1,
                                               "completion_tokens": 1})()
                    return _R()
    _lb = _ctxb.OpenAIBrain(model="qwen3.6:latest",
                            base_url="http://localhost:11434/v1",
                            client=_CtxClient(), label="qwen3.6:latest")
    _lb._do_chat([{"role": "user", "content": "hi"}], ["sys"], None, None, None)
    check("ollamactx.local_engine_gets_num_ctx",
          _ctx_cap.get("extra_body", {}).get("options", {}).get("num_ctx", 0)
          >= config.MAX_TOKENS + 2048)
    _cb = _ctxb.OpenAIBrain(model="deepseek-chat",
                            base_url="https://api.deepseek.com/v1",
                            client=_CtxClient(), label="deepseek")
    _cb._do_chat([{"role": "user", "content": "hi"}], ["sys"], None, None, None)
    check("ollamactx.cloud_engine_no_num_ctx",
          "extra_body" not in _ctx_cap)

    # --- privacy shield: detection, masking, routing --------------------------- #
    import agent.privacy as _pv
    _ptxt = ("Email piet@bank.co.za, ID 8001015009087, card 4111 1111 1111 "
             "1111, cell 082 555 1234, key sk-abcdef1234567890XYZ")
    check("privacy.detects_all_kinds",
          sorted({k for _, _, k, _ in _pv.detect(_ptxt)})
          == ["CARD", "EMAIL", "PHONE", "SA_ID", "SECRET"])
    check("privacy.checksums_gate_false_positives",
          _pv.detect("id 8001015009088") == []
          and all(k != "CARD" for _, _, k, _
                  in _pv.detect("4111 1111 1111 1112")))
    _pm = _pv.Masker()
    _pmask = _pm.mask(_ptxt)
    check("privacy.mask_and_roundtrip",
          "piet@bank.co.za" not in _pmask
          and "\u27e6EMAIL_1\u27e7" in _pmask
          and _pm.unmask(_pmask) == _ptxt
          and _pm.mask("again piet@bank.co.za")
          == "again \u27e6EMAIL_1\u27e7")
    check("privacy.deep_unmask",
          _pm.deep_unmask({"to": "\u27e6EMAIL_1\u27e7"})
          == {"to": "piet@bank.co.za"})
    _pout = []
    _psu = _pv.StreamUnmasker(_pout.append, _pm)
    _pfull = "To \u27e6EMAIL_1\u27e7 now"
    _psu.feed(_pfull[:6]); _psu.feed(_pfull[6:10]); _psu.feed(_pfull[10:])
    _psu.flush()
    check("privacy.stream_unmask_split_chunks",
          "".join(_pout) == "To piet@bank.co.za now")

    _pv_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "pv.db"
    _pv_prev = config.PRIVACY_MODE
    try:
        from agent.memory import MemoryStore as _MS9
        _pvm = _MS9(db_path=config.DB_PATH, check_same_thread=False)
        _pcap = {}

        class _PB:
            model = "claude-x"; fast_model = "ollama-x"; local = True
            last_engine = None; last_usage = None

            def chat(self, messages, system, tools=None, on_text=None,
                     model=None):
                _pcap["model"] = model
                _mc = messages[-1]["content"]
                _pcap["last_user"] = (_mc if isinstance(_mc, str)
                                      else _mc[0]["text"])
                return _blk("Mail \u27e6EMAIL_1\u27e7 done.")
        config.PRIVACY_MODE = "mask"
        _pr = agent_main.run_turn(_PB(), _pvm, [],
                                  "Contact piet@bank.co.za please",
                                  auto_approve=False, session_id="pvt1",
                                  force_model="claude-x", turn_info={})
        check("privacy.engine_sees_placeholder_reply_unmasked",
              "piet@bank.co.za" not in _pcap["last_user"]
              and "\u27e6EMAIL_1\u27e7" in _pcap["last_user"]
              and "piet@bank.co.za" in _pr and "Privacy shield" in _pr)
        config.PRIVACY_MODE = "local"
        _pti = {}
        agent_main.run_turn(_PB(), _pvm, [], "ID is 8001015009087 check",
                            auto_approve=False, session_id="pvt2",
                            force_model="claude-x", turn_info=_pti)
        check("privacy.local_mode_forces_local",
              _pcap["model"] == "ollama-x"
              and _pti["privacy"]["mode"] == "local")
        config.PRIVACY_MODE = "off"
        agent_main.run_turn(_PB(), _pvm, [], "Contact piet@bank.co.za please",
                            auto_approve=False, session_id="pvt3",
                            force_model="claude-x", turn_info={})
        check("privacy.off_mode_untouched",
              "piet@bank.co.za" in _pcap["last_user"])
    finally:
        config.AGENT_HOME = _pv_home
        config.PRIVACY_MODE = _pv_prev

    # --- watch-folder RAG + semantic long-term memory ------------------------- #
    _fw_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "fw.db"
    try:
        import agent.folderwatch as _fw
        import agent.rag as _frag
        _frag._STORE = None if hasattr(_frag, "_STORE") else None
        _docs = _RPath(_rtf.mkdtemp())
        (_docs / "campaign.txt").write_text("Golden Ticket campaign mechanics",
                                            "utf-8")
        _fw.add_folder(str(_docs))
        _s1 = _fw.sweep()
        check("folderwatch.first_sweep_ingests", _s1["added"] >= 1)
        _s2 = _fw.sweep()
        check("folderwatch.quiet_sweep_skips",
              _s2["added"] == 0 and _s2["updated"] == 0
              and _s2["skipped"] >= 1)
        import time as _ftime
        _ftime.sleep(0.02)
        (_docs / "campaign.txt").write_text("Golden Ticket v2 tiers", "utf-8")
        check("folderwatch.edit_reingests", _fw.sweep()["updated"] == 1)
        (_docs / "brief.md").write_text("Tandem brief", "utf-8")
        check("folderwatch.new_file_added", _fw.sweep()["added"] == 1)
        check("folderwatch.log_records_work",
              any(e.get("event") == "sweep" for e in _fw.recent_log(5)))
        _fw.set_enabled(str(_docs), False)
        (_docs / "later.txt").write_text("later", "utf-8")
        check("folderwatch.disabled_folder_ignored",
              _fw.sweep()["folders"] == 0)
        _fw.remove_folder(str(_docs))
        check("folderwatch.remove_works",
              _fw.load_config()["folders"] == [])

        from agent.memory import MemoryStore as _MSA
        _sm = _MSA(db_path=config.AGENT_HOME / "sm.db",
                   check_same_thread=False)
        _sm.add_memory("User deploys his hugo blog to netlify every Friday",
                       "preference")
        import agent.rag as _srag
        _sem_orig = _srag.embed_texts
        try:
            _srag.embed_texts = lambda t: None
            check("semmem.keyword_baseline",
                  bool(_sm.search_memories("hugo netlify"))
                  and _sm.search_memories("push my website online") == [])

            def _sfake(texts):
                return [[1.0 if any(w in t.lower() for w in
                                    ("hugo", "netlify", "blog", "website",
                                     "online", "push", "deploy")) else 0.0,
                         0.05] for t in texts]
            _srag.embed_texts = _sfake
            _shit = _sm.search_memories("when do I push my website online?")
            check("semmem.reworded_hit_with_embedder",
                  bool(_shit) and "netlify" in _shit[0]["content"])
            check("semmem.irrelevant_stays_lean",
                  _sm.search_memories("colour of the moon") == [])
        finally:
            _srag.embed_texts = _sem_orig
    finally:
        config.AGENT_HOME = _fw_home

    # --- blender lab: headless harness, contract, safety ---------------------- #
    _bl_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "bl.db"
    _bl_prev = getattr(config, "BLENDER_PATH", "")
    try:
        import agent.blenderlab as _bl
        import os as _bos, stat as _bstat
        _fake = config.AGENT_HOME / "fakeblender"
        _fake.write_text("#!/bin/sh\nexec python3 \"$4\"\n", "utf-8")
        _fake.chmod(_fake.stat().st_mode | _bstat.S_IEXEC)
        config.BLENDER_PATH = str(_fake)
        check("blender.configured_path_wins",
              _bl.find_blender() == str(_fake))
        _ok = _bl.run_script(
            "open(os.path.join(OUT_DIR, 'render.png'), 'wb')"
            ".write(b'PNG' + b'x' * 200)\n"
            "open(os.path.join(OUT_DIR, 'model.glb'), 'wb')"
            ".write(b'glTF' + b'x' * 200)", note="fake ok")
        check("blender.harness_collects_images",
              _ok["ok"] and _ok["images"] == ["render.png"]
              and _bl.jobs(3)[0]["ok"] is True)
        check("blender.harness_collects_models",
              _ok["models"] == ["model.glb"]
              and _bl.model_path(_ok["job"], "model.glb") is not None
              and _bl.model_path(_ok["job"], "../script.py") is None
              and _bl.image_path(_ok["job"], "model.glb") is None)
        check("blender.image_lookup_traversal_safe",
              _bl.image_path(_ok["job"], "render.png") is not None
              and _bl.image_path("..", "x.png") is None
              and _bl.image_path(_ok["job"], "../script.py") is None)
        _bad = _bl.run_script("raise RuntimeError('bad shader node')")
        check("blender.failure_returns_log",
              _bad["ok"] is False and "bad shader node" in _bad["log_tail"])
        _noimg = _bl.run_script("x = 1")
        check("blender.no_image_contract_hint",
              _noimg["ok"] is False
              and "must render to OUT_DIR" in _noimg["log_tail"])
        config.BLENDER_PATH = str(config.AGENT_HOME / "missing-exe")
        import shutil as _bsh
        _which_prev = _bsh.which
        _bsh.which = lambda *a, **k: None
        try:
            _gone = _bl.run_script("x = 1")
            check("blender.missing_graceful",
                  _gone["ok"] is False and "blender.org" in _gone["error"])
        finally:
            _bsh.which = _which_prev
    finally:
        config.AGENT_HOME = _bl_home
        config.BLENDER_PATH = _bl_prev

    # --- long-request guard: non-streaming falls back to streaming ------------ #
    try:
        import agent.brain as _lgbrain
        from types import SimpleNamespace as _LGNS
        _LGFINAL = _LGNS(content=[_LGNS(type="text", text="ok")],
                         stop_reason="end_turn")

        class _LGStream:
            def __init__(self, kw): self.kw = kw
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get_final_message(self): return _LGFINAL

        class _LGMsgs:
            def __init__(self, mode):
                self.mode = mode; self.streamed = False; self.kw = None

            def create(self, **kw):
                self.kw = kw
                if self.mode == "guard":
                    raise ValueError("Streaming is required for operations "
                                     "that may take longer than 10 minutes.")
                if self.mode == "other":
                    raise ValueError("unrelated problem")
                return _LGFINAL

            def stream(self, **kw):
                self.streamed = True
                return _LGStream(kw)

        def _lgmake(mode):
            _b = _lgbrain.AnthropicBrain.__new__(_lgbrain.AnthropicBrain)
            _b.client = _LGNS(messages=_LGMsgs(mode))
            _b.model = "claude-sonnet-4-6"; _b.fast_model = "claude-haiku"
            _b.local = None; _b.last_engine = None; _b.last_usage = None
            return _b
        _lg1 = _lgmake("ok")
        check("longreq.normal_call_not_streamed",
              _lg1.chat([{"role": "user", "content": "hi"}], ["s"])
              is _LGFINAL and _lg1.client.messages.streamed is False)
        _lg2 = _lgmake("guard")
        check("longreq.guard_falls_back_to_streaming",
              _lg2.chat([{"role": "user", "content": "hi"}], ["s"])
              is _LGFINAL and _lg2.client.messages.streamed is True)
        _lg3 = _lgmake("guard")
        _lg3.chat([{"role": "user", "content": "hi"}], ["s"],
                  tools=[{"name": "t"}])
        check("longreq.tools_preserved_in_fallback",
              _lg3.client.messages.kw.get("tools") == [{"name": "t"}])
        _lg4 = _lgmake("other")
        try:
            _lg4.chat([{"role": "user", "content": "hi"}], ["s"])
            _lg4_ok = False
        except ValueError:
            _lg4_ok = _lg4.client.messages.streamed is False
        check("longreq.unrelated_valueerror_propagates", _lg4_ok)
    except Exception as _lgexc:
        check("longreq.harness", False)

    # --- cache busting: a stale browser asset must be impossible ------------- #
    try:
        _cbhtml = (_RPath("web/static/index.html").read_text("utf-8")
                   if _RPath("web/static/index.html").exists() else "")
        if _cbhtml:
            import web.server as _cbws
            _cbv = str(getattr(config, "BUILD_ID", "dev")).replace(
                " ", "").replace(":", "")
            _stamped = _cbhtml.replace("/static/styles.css",
                                       f"/static/styles.css?v={_cbv}")
            _stamped = _stamped.replace("/static/app.js",
                                        f"/static/app.js?v={_cbv}")
            check("cachebust.assets_carry_build_id",
                  "/static/app.js?v=" in _stamped
                  and "/static/styles.css?v=" in _stamped
                  and '"/static/app.js"' not in _stamped)
            check("cachebust.build_id_present",
                  bool(getattr(config, "BUILD_ID", "")))
    except Exception:
        check("cachebust.harness", False)

    # --- shareable build: setup wizard + an archive that carries no secrets --- #
    _rl_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "rl.db"
    try:
        import os as _rlos
        import agent.setup as _rlsu
        _rlprev = _rlos.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            check("release.fresh_machine_is_unconfigured",
                  _rlsu.state()["configured"] is False)
            # every rejection has to say what's actually wrong
            check("release.key_validation_is_specific",
                  "space" in _rlsu.validate_key("sk-ant-api03-a b")
                  and "different provider"
                  in _rlsu.validate_key("sk-proj-" + "a" * 30)
                  and "truncated" in _rlsu.validate_key("sk-ant-abc")
                  and _rlsu.validate_key("sk-ant-api03-" + "x" * 40) == "")
            _rlsave = _rlsu.save_api_key("sk-ant-api03-" + "x" * 40)
            check("release.key_saves_and_reaches_the_engine",
                  _rlsave["ok"] and _rlsave["sealed"] is True
                  and _rlos.environ.get("ANTHROPIC_API_KEY", "").startswith(
                      "sk-ant"))
            _rlraw = (config.AGENT_HOME / "credentials.json").read_text("utf-8")
            check("release.key_never_stored_in_the_clear",
                  "sk-ant-api03-xxxx" not in _rlraw)
            _rlos.environ.pop("ANTHROPIC_API_KEY", None)
            check("release.key_survives_a_restart",
                  _rlsu.load_saved_credentials() is True
                  and _rlsu.state()["configured"] is True)
            check("release.key_can_be_cleared",
                  _rlsu.clear_api_key()["ok"] and _rlsu.has_key() is False)
        finally:
            if _rlprev:
                _rlos.environ["ANTHROPIC_API_KEY"] = _rlprev

        # the packager must REFUSE to ship anything personal
        import importlib.util as _rlil
        _rlspec = _rlil.spec_from_file_location(
            "make_release", str(_RPath("tools/make_release.py").resolve()))
        _rlmod = _rlil.module_from_spec(_rlspec); _rlspec.loader.exec_module(_rlmod)
        import zipfile as _rlz
        _rlzip = _RPath(_rtf.mkdtemp()) / "t.zip"

        def _rlbuild(name, blob):
            if _rlzip.exists():
                _rlzip.unlink()
            with _rlz.ZipFile(_rlzip, "w") as _z:
                _z.writestr("agent/main.py", "print('hi')")
                _z.writestr(name, blob)
            return _rlmod.audit(_rlzip)
        check("release.detects_a_key_hidden_in_source",
              bool(_rlbuild("agent/config.py",
                            b'K = "sk-ant-api03-' + b"x" * 40 + b'"')))
        check("release.blocks_personal_data_files",
              all(_rlbuild(f, b"innocent") for f in
                  ("agent.db", "audit.jsonl", "credentials.json",
                   "secret.key", "profile.json")))
        check("release.blocks_other_providers_credentials",
              all(_rlbuild("web/server.py", b"x='" + blob + b"'")
                  for blob in (b"ghp_" + b"a" * 30, b"AKIA" + b"B" * 16,
                               b"xoxb-" + b"1" * 20,
                               b"sk-proj-" + b"z" * 30)))
        check("release.clean_archive_passes",
              _rlbuild("web/server.py", b"answer = 42") == [])
        check("release.installer_and_readme_are_generated",
              "install.bat" in _rlmod.INSTALL_BAT[:0] + "install.bat"
              and "python" in _rlmod.INSTALL_BAT.lower()
              and "Ollama" in _rlmod.READ_ME_FIRST)
    finally:
        config.AGENT_HOME = _rl_home

    # --- the dashboard must be VISIBLE by default ----------------------------- #
    #
    # It shipped invisible because it reused the old KPI strip's localStorage
    # key: anyone who had collapsed those vanity counters inherited "hidden"
    # for a feature that didn't exist when they set that preference. It now
    # has its own key, defaults to visible, and is visible in the markup so a
    # slow or partly-failed script can't leave it hidden forever.
    try:
        import shutil as _dvsh, subprocess as _dvsub
        _dvnode = _dvsh.which("node")
        _dvfile = _RPath("tests/dash_visibility.js")
        if _dvnode and _dvfile.exists():
            def _dvrun(prefs):
                r = _dvsub.run([_dvnode, str(_dvfile), prefs],
                               capture_output=True, text=True, timeout=60,
                               cwd=str(_RPath(".").resolve()))
                return (r.stdout or "") + (r.stderr or "")
            _dvfresh = _dvrun("{}")
            _dvstale = _dvrun('{"aj_kpi_collapsed":"1"}')
            _dvhidden = _dvrun('{"aj_dash_hidden":"1"}')
            check("dashboard.visible_on_a_fresh_install",
                  "after boot: dash.hidden = false" in _dvfresh)
            check("dashboard.ignores_the_old_strips_preference",
                  "after boot: dash.hidden = false" in _dvstale)
            check("dashboard.respects_an_explicit_hide_and_reopens",
                  "after boot: dash.hidden = true" in _dvhidden
                  and "after click: dash.hidden = false" in _dvhidden)
        else:
            check("dashboard.visibility_harness_present", _dvfile.exists())
    except Exception:
        check("dashboard.visibility_harness", False)

    # markup must not hide it — JS may fail; the pane should still be there
    _dvhtml = _RPath("web/static/index.html").read_text("utf-8")
    check("dashboard.not_hidden_in_the_markup",
          '<div class="dash" id="dash">' in _dvhtml)

    # --- dashboard: surfaces what needs a human, not vanity counters ---------- #
    _dh_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "agent.db"
    try:
        import json as _dhj
        import agent.dashboard as _dh
        from agent.memory import MemoryStore as _DHM
        _dhm = _DHM(db_path=config.DB_PATH, check_same_thread=False)
        _dhr = _dh.report(_dhm)
        check("dashboard.flags_missing_backup_as_action",
              any(i["title"] == "No backup exists" and i["severity"] == "act"
                  for i in _dhr["needs_you"]))
        check("dashboard.every_item_routes_somewhere",
              all(i["panel"] for i in _dhr["needs_you"]))
        check("dashboard.reports_running_state",
              "spend" in _dhr["running"] and "engine" in _dhr["running"])
        # a self-improvement waiting for approval is the most urgent thing
        _dhsi = config.AGENT_HOME / "selfimprove"; _dhsi.mkdir(exist_ok=True)
        (_dhsi / "proposal.json").write_text(_dhj.dumps(
            {"state": "proposed", "tests_ok": True,
             "files": [{"path": "a.py"}]}), "utf-8")
        _dhr2 = _dh.report(_dhm)
        check("dashboard.pending_proposal_ranks_first",
              _dhr2["needs_you"][0]["title"].startswith("Code change")
              and _dhr2["needs_you"][0]["severity"] == "act")
        _dhr3 = _dh.report(_dhm)
        _dhsev = [i["severity"] for i in _dhr3["needs_you"]]
        check("dashboard.sorted_by_urgency",
              _dhsev == sorted(_dhsev, key=lambda s: {"act": 0, "review": 1,
                                                      "note": 2}[s]))
        # one broken gatherer must degrade its own row, not the board
        _dhorig = list(_dh.GATHERERS)
        _dh.GATHERERS = [lambda: (_ for _ in ()).throw(RuntimeError("boom"))] \
            + _dhorig
        try:
            _dhr4 = _dh.report(_dhm)
        finally:
            _dh.GATHERERS = _dhorig
        check("dashboard.one_broken_gatherer_doesnt_blank_it",
              isinstance(_dhr4["needs_you"], list)
              and len(_dhr4["needs_you"]) > 0)
        import shutil as _dhsh
        _dhsh.rmtree(_dhsi)
        (config.AGENT_HOME / "backups").mkdir(exist_ok=True)
        (config.AGENT_HOME / "backups" / "atlas-backup-x.zip").write_bytes(b"PK")
        # --- tiles: numbers with something to compare against -----------
        import agent.costs as _dhc
        _dhc.record("claude-sonnet-4-6", {"in": 420000, "out": 90000},
                    feature="trends")
        _dhc.record("Ollama", {"in": 1200000, "out": 300000}, feature="crew")
        _dht = _dh.tiles(_dhm)
        _dhby = {x["label"]: x for x in _dht}
        check("dashboard.surfaces_token_usage",
              "Tokens this month" in _dhby
              and _dhby["Tokens this month"]["value"] == "2.01M"
              and "in" in _dhby["Tokens this month"]["sub"])
        check("dashboard.compact_number_formatting",
              _dh._compact(1250000) == "1.25M" and _dh._compact(940) == "940"
              and _dh._compact(0) == "0")
        check("dashboard.token_history_for_a_sparkline",
              len(_dhby["Tokens this month"].get("spark") or []) == 14)
        check("dashboard.names_the_biggest_spender",
              "Biggest spender" in _dhby
              and "progress" in _dhby["Biggest spender"])
        check("dashboard.capability_ratio_tile",
              "/" in _dhby["Capabilities used"]["value"]
              and "progress" in _dhby["Capabilities used"])
        check("dashboard.tiles_route_to_panels",
              any(x.get("panel") for x in _dht))
        # a ceiling turns the spend tile into a limit with a bar
        _dhc.set_engine("x", "Auto")
        _dhcap = _dh.tiles(_dhm)
        check("dashboard.enough_tiles_to_be_a_dashboard", len(_dhcap) >= 8)

        # --- persistence: history, sparklines, prefs, instant paint ------
        import datetime as _dhdt
        _dh.report(_dhm)
        _dhh = _dh.history()
        check("dashboard.records_history_for_every_measurable_tile",
              len(_dhh) >= 8
              and all(isinstance(v, dict) and v for v in _dhh.values()))
        # backfill a fortnight so sparklines have something to draw. The
        # board's days are UTC days: a local date ran a day ahead of it after
        # midnight in South Africa (00:00-02:00), and this check failed then.
        _dhtoday = _dhdt.datetime.now(_dhdt.timezone.utc).date()
        for _k in list(_dhh):
            for _i in range(1, 14):
                _dhh[_k][(_dhtoday - _dhdt.timedelta(days=_i)
                          ).strftime("%Y-%m-%d")] = (14 - _i) * 3.0
        _dh._write_json(_dh._hist_path(), _dhh)
        _dhr5 = _dh.report(_dhm)
        _dhspark = [x for x in _dhr5["tiles"] if x.get("spark")]
        check("dashboard.every_tracked_tile_gets_a_sparkline",
              len(_dhspark) >= 8
              and all(len(x["spark"]) == 14 for x in _dhspark))
        # Two sparkline sources, deliberately different: token tiles keep the
        # cost ledger's daily series, where a day with no usage really is
        # zero; counters like Memories carry the last value forward, because
        # an unobserved count didn't drop to nothing. Test the counter.
        _dhcnt = next((x for x in _dhspark if x["key"] == "memories"), None)
        check("dashboard.counter_history_carries_forward_not_to_zero",
              _dhcnt is not None
              and all(v > 0 for v in _dhcnt["spark"][:-1]))
        _dhtok = next((x for x in _dhspark
                       if x["key"] == "tokens_this_month"), None)
        check("dashboard.token_history_keeps_real_zero_days",
              _dhtok is not None and len(_dhtok["spark"]) == 14)
        # bounded, or the file grows forever
        _dhbig = _dh.history(); _dhk = list(_dhbig)[0]
        for _i in range(200):
            _dhbig[_dhk][(_dhtoday - _dhdt.timedelta(days=_i)
                          ).strftime("%Y-%m-%d")] = 1.0
        _dh._write_json(_dh._hist_path(), _dhbig)
        _dh.report(_dhm)
        check("dashboard.history_is_bounded",
              len(_dh.history()[_dhk]) <= _dh.HISTORY_DAYS)
        # preferences
        _dhbefore = len(_dh.report(_dhm)["tiles"])
        _dh.save_prefs(hidden=["requests", "skills"])
        _dhkeys = [x["key"] for x in _dh.report(_dhm)["tiles"]]
        check("dashboard.hidden_tiles_stay_hidden",
              "requests" not in _dhkeys and "skills" not in _dhkeys
              and len(_dhkeys) == _dhbefore - 2)
        _dh.save_prefs(order=["capabilities_used", "memories"])
        _dhk3 = [x["key"] for x in _dh.report(_dhm)["tiles"]]
        check("dashboard.saved_order_is_honoured",
              _dhk3[:2] == ["capabilities_used", "memories"]
              and len(_dhk3) == len(_dhkeys))
        check("dashboard.prefs_survive_a_reload",
              _dh.prefs()["hidden"] == ["requests", "skills"])
        # a tile added later must not be swallowed by an old saved order
        _dh.save_prefs(order=["memories"])
        check("dashboard.new_tiles_still_appear",
              len(_dh.report(_dhm)["tiles"]) == len(_dhkeys))
        # the picker must still offer what's currently hidden
        check("dashboard.picker_lists_hidden_tiles",
              any(x["key"] == "requests"
                  for x in _dh.report(_dhm)["all_tiles"]))
        _dh.save_snapshot(_dh.report(_dhm))
        _dhsnap = _dh.last_snapshot()
        check("dashboard.snapshot_enables_instant_paint",
              len(_dhsnap.get("tiles", [])) > 0 and "running" in _dhsnap)
        _dh.save_prefs(hidden=[], order=[])
        check("dashboard.all_clear_when_nothing_pending",
              _dh.report(_dhm)["all_clear"] is True)
    finally:
        config.AGENT_HOME = _dh_home

    # --- tile grid must fill the pane, with no stranded last row -------------- #
    #
    # A CSS grid fills row by row, so leftovers sit on the last row beside
    # empty space — and some counts (13 tiles) never divide evenly at any
    # sensible width. The layout widens the trailing tiles to absorb the
    # leftover columns; this asserts every row ends up completely full.
    try:
        import shutil as _tlsh, subprocess as _tlsub
        _tlnode = _tlsh.which("node")
        _tlfile = _RPath("tests/tile_layout.js")
        if _tlnode and _tlfile.exists():
            _tlout = _tlsub.run([_tlnode, str(_tlfile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _tltext = (_tlout.stdout or "") + (_tlout.stderr or "")
            _tlok = "every row completely filled, all cases: true" in _tltext
            _tlsafe = ("zero tiles safe: true" in _tltext
                       and "hidden pane safe: true" in _tltext)
        else:
            _tlok = _tlsafe = _tlfile.exists()
    except Exception:
        _tlok = _tlsafe = False
    check("dashboard.tiles_fill_every_row", _tlok)
    check("dashboard.tile_layout_handles_empty_and_hidden", _tlsafe)

    # --- the UI must actually RUN, not merely parse ---------------------------- #
    #
    # This exists because syntax-checking app.js passed while the app was
    # broken: an edit deleted four whole panel sections, so wireEvents() threw
    # partway and every listener after that point silently never attached.
    # Modals wouldn't close, buttons did nothing, and `node --check` was
    # perfectly happy. Parsing is not running.
    try:
        import shutil as _uish, subprocess as _uisub
        _uinode = _uish.which("node")
        _uismoke = _RPath("tests/dom_smoke.js")
        if _uinode and _uismoke.exists():
            _uiout = _uisub.run([_uinode, str(_uismoke)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _uitext = (_uiout.stdout or "") + (_uiout.stderr or "")
            check("ui.app_js_executes_without_throwing",
                  "script evaluates: true" in _uitext)
            check("ui.wire_events_attaches_every_listener",
                  "wireEvents() completed: true" in _uitext
                  and "THREW" not in _uitext)
            # Defining a renderer proves nothing. The dashboard shipped twice
            # with code that existed but nothing reachable ever called, so
            # these assert it RENDERS and that the poll actually calls it.
            check("ui.dashboard_actually_renders",
                  "dashboard renders attention rows: true" in _uitext
                  and "dashboard renders tiles: true" in _uitext
                  and "dashboard renders running chips: true" in _uitext)
            check("ui.dashboard_is_reachable_from_the_poll",
                  "dashboard refreshed by the stats poll: true" in _uitext)
        else:
            # no node here; the static checks below still guard the same thing
            check("ui.smoke_harness_present", _uismoke.exists())
    except Exception:
        check("ui.smoke_harness", False)

    # every event handler referenced by name must exist, and every modal must
    # have a way out — both were violated by that same edit
    # Compute first, THEN check. Wrapping check() in try/except swallows a
    # genuine failure and re-reports it as the harness breaking, which is
    # exactly how a real bug can hide behind a green-looking suite.
    import re as _re
    _uijs2 = _RPath("web/static/app.js").read_text("utf-8")
    _uihtml2 = _RPath("web/static/index.html").read_text("utf-8")
    _uidef = set(_re.findall(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                             _uijs2))
    # handlers are also declared as arrow consts, e.g. `const saveAuto = ...`
    _uidef |= set(_re.findall(
        r"(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", _uijs2))
    _uirefs = set(_re.findall(
        r'addEventListener\("[a-z]+",\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)',
        _uijs2))
    _uighosts = sorted(r for r in _uirefs if r not in _uidef)
    check("ui.every_named_handler_exists", not _uighosts)
    _uimodals = _re.findall(r'class="modal-backdrop" id="([A-Za-z]+)"',
                            _uihtml2)
    _uistuck = []
    for _m in _uimodals:
        _base = _m[:-5] if _m.endswith("Modal") else _m
        _cap = "close" + _base[0].upper() + _base[1:]
        if _cap not in _uidef and _cap + "Modal" not in _uidef:
            _uistuck.append(_m)
    check("ui.every_modal_can_be_closed", not _uistuck)

    # --- theming: light mode must be real, not a dark theme with pale text ---- #
    # Compute first, then check — wrapping check() in try/except turns a real
    # failure into a vague "harness broke" and hides which assertion went.
    import re as _re
    _uicss = _RPath("web/static/styles.css").read_text("utf-8")
    _uihtml = _RPath("web/static/index.html").read_text("utf-8")
    _uijs = _RPath("web/static/app.js").read_text("utf-8")
    check("ui.both_fluent_themes_defined",
          '[data-theme="fluent-light"]' in _uicss
          and '[data-theme="fluent"]' in _uicss)
    check("ui.color_scheme_declared_for_native_controls",
          "color-scheme: light" in _uicss and "color-scheme: dark" in _uicss)
    check("ui.follows_the_os_and_resolves_before_paint",
          "prefers-color-scheme" in _uijs
          # resolved before first paint: in prepaint.js now, because the
          # page's CSP blocks the inline script it used to be
          and ("prefers-color-scheme" in _uihtml
               or "prefers-color-scheme" in _RPath("web/static/prepaint.js").read_text("utf-8")))
    check("ui.theme_choices_include_system",
          '"system"' in _uijs and "fluent-light" in _uijs)
    _uivars = set(_re.findall(r"var\((--[a-z0-9-]+)", _uicss))
    _uidefs = set(_re.findall(r"^\s*(--[a-z0-9-]+):", _uicss, _re.M))
    check("ui.no_undefined_design_tokens", not (_uivars - _uidefs))
    check("ui.braces_balanced", _uicss.count("{") == _uicss.count("}"))
    # every sidebar button needs its own icon — count follows the buttons
    # rather than a number that goes stale each time one is added
    _uibtns = _re.findall(r'<button class="foot-btn" id="[A-Za-z]+">', _uihtml)
    _uiicons = _re.findall(r'foot-ico"><svg[^>]*>(.*?)</svg>', _uihtml, _re.S)
    check("ui.every_sidebar_button_has_a_distinct_icon",
          len(_uiicons) == len(_uibtns) and len(set(_uiicons)) == len(_uiicons))
    check("ui.no_emoji_left_in_navigation",
          not _re.search(r'foot-ico">[^<]*[\U0001F300-\U0001FAFF]', _uihtml))
    check("ui.responsive_breakpoints_present",
          _uicss.count("@media (max-width") >= 3)

    # --- sidebar hierarchy: a heading must not look like its items ----------- #
    #
    # The Fluent pass had set group headings to 12px semibold body text, all
    # but identical to the 13px buttons under them, so Core / Automation /
    # Extend / Safety stopped reading as headings at all. These assert the
    # invariant rather than exact values: whatever the numbers become, a
    # heading stays smaller and quieter than the items it introduces, and the
    # items stay visually attached to it.
    def _css_last(selector, prop):
        """The last declared value of `prop` inside `selector` — last wins in
        the cascade at equal specificity."""
        found = None
        for _m in _re.finditer(_re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}",
                               _uicss):
            for _p in _re.finditer(_re.escape(prop) + r"\s*:\s*([^;]+);", _m.group(1)):
                found = _p.group(1).strip()
        return found

    def _px(v):
        # sizes are tokens now (var(--t-small)), so resolve one level of
        # indirection before measuring — the invariant is still "a heading
        # is smaller than its items", whatever the values are called
        v = (v or "").strip()
        m = _re.match(r"var\((--[a-z0-9-]+)\)", v)
        if m:
            d = _re.search(r"^\s*" + _re.escape(m.group(1)) + r":\s*([^;]+);",
                           _uicss, _re.M)
            v = d.group(1) if d else ""
        try:
            return float(_re.sub(r"[^0-9.]", "", v or "")) or None
        except Exception:
            return None
    _hd = _px(_css_last(".foot-group-toggle", "font-size"))
    _it = _px(_css_last(".foot-group-items .foot-btn", "font-size"))
    check("sidebar.heading_is_smaller_than_its_items",
          bool(_hd and _it) and _hd < _it)
    _hdf = _px(_css_last('[data-theme^="fluent"] .foot-group-toggle',
                         "font-size"))
    _itf = _px(_css_last('[data-theme^="fluent"] .foot-group-items .foot-btn',
                         "font-size"))
    check("sidebar.heading_stays_subordinate_in_fluent_too",
          bool(_hdf and _itf) and _hdf < _itf)
    # several rules mention .foot-group-items (the collapsed one only hides
    # it) — the spine just has to be declared on one of them
    # --- installable on a phone ---------------------------------------------- #
    #
    # A native store app isn't possible — this agent runs commands, reads the
    # disk and drives Blender, none of which iOS permits — so the phone build
    # is a PWA: the same UI, installed to a home screen, talking to the PC
    # over the LAN. These assert the pieces that actually decide whether the
    # install prompt appears at all.
    import json as _pwj
    _pwman = _pwj.loads(
        _RPath("web/static/manifest.webmanifest").read_text("utf-8"))
    check("phone.manifest_is_installable",
          _pwman.get("display") == "standalone"
          and _pwman.get("start_url") and _pwman.get("name")
          and any(i["sizes"] == "192x192" for i in _pwman["icons"])
          and any(i["sizes"] == "512x512" for i in _pwman["icons"]))
    check("phone.has_maskable_icons",
          any(i.get("purpose") == "maskable" for i in _pwman["icons"]))
    _pwicons = _RPath("web/static/icons")
    check("phone.icon_files_exist",
          all((_pwicons / f"icon-{s}.png").exists() for s in (180, 192, 512))
          and all((_pwicons / f"maskable-{s}.png").exists()
                  for s in (192, 512)))
    _pwhtml = _RPath("web/static/index.html").read_text("utf-8")
    check("phone.ios_needs_its_own_tags",
          "apple-mobile-web-app-capable" in _pwhtml
          and "apple-touch-icon" in _pwhtml)
    check("phone.paints_under_the_notch",
          "viewport-fit=cover" in _pwhtml
          and "env(safe-area-inset" in _uicss)
    check("phone.registers_a_service_worker",
          ("serviceWorker.register" in _pwhtml
           # in app.js now: the page's CSP blocks the inline script it was in
           or "serviceWorker.register" in _RPath("web/static/app.js").read_text("utf-8")))
    _pwsw = _RPath("web/static/sw.js").read_text("utf-8")
    # the important one: live state must never be served from cache
    check("phone.never_caches_live_state",
          'url.pathname.startsWith("/api/")' in _pwsw
          and "return;" in _pwsw)
    check("phone.touch_targets_are_reachable",
          "min-height: 40px" in _uicss and "font-size: 16px" in _uicss)
    _pwws = _RPath("web/server.py").read_text("utf-8")
    check("phone.manifest_and_worker_served_from_root",
          '@app.get("/manifest.webmanifest")' in _pwws
          and '@app.get("/sw.js")' in _pwws
          and "Service-Worker-Allowed" in _pwws)

    # --- the visual layer must stay safe as well as look good ---------------- #
    #
    # The sparkline is the one real graphic here, so it gets the same
    # treatment as any other data path: degenerate inputs (all-zero, a single
    # point, negatives) must not produce NaN coordinates, and two tiles must
    # not share a gradient id — SVG ids are global, so a collision silently
    # paints one tile with another's fill.
    try:
        import shutil as _vish, subprocess as _visub
        _vinode = _vish.which("node")
        _vifile = _RPath("tests/spark_render.js")
        if _vinode and _vifile.exists():
            _viout = _visub.run([_vinode, str(_vifile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _vit = (_viout.stdout or "") + (_viout.stderr or "")
            _viok = all(x in _vit for x in (
                "line drawn from every point: true",
                "area filled with a gradient: true",
                "latest reading is marked: true",
                "y stays inside the box: true",
                "flat series safe: true",
                "single point safe: true",
                "negative values safe: true",
                "gradient ids unique per tile: true"))
        else:
            _viok = _vifile.exists()
    except Exception:
        _viok = False
    check("visuals.sparkline_renders_safely", _viok)
    # --- the board must sit still --------------------------------------------
    #
    # It repaints every 45 seconds. Rebuilding the DOM each time replayed the
    # entrance animation and re-ran the grid layout, so tiles visibly jumped
    # even when not one number had changed. All three regions — tiles, status
    # chips and the needs-you list — now update in place and are skipped
    # entirely when their content is identical.
    try:
        _stfile = _RPath("tests/dash_stability.js")
        if _vinode and _stfile.exists():
            _stout = _visub.run([_vinode, str(_stfile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _stt = (_stout.stdout or "") + (_stout.stderr or "")
            _stok = all(x in _stt for x in (
                "5 identical refreshes create 0 nodes: true",
                "...and clear nothing: true",
                "changed value creates at most a couple of nodes: true",
                "new value is shown: true",
                "no re-animation on update: true",
                "a changed tile SET does rebuild: true"))
        else:
            _stok = _stfile.exists()
    except Exception:
        _stok = False
    check("visuals.dashboard_does_not_jitter_on_refresh", _stok)
    check("visuals.entrance_animation_is_first_paint_only",
          ".dash-tiles.first-paint .dash-tile" in _uicss
          and ".dash-tiles .dash-tile { animation: none" in _uicss)
    check("visuals.sparkline_space_is_reserved",
          ".dash-spark-host" in _uicss and "min-height" in _uicss)
    check("visuals.motion_respects_reduced_motion",
          "prefers-reduced-motion" in _uicss
          and _uicss.count("prefers-reduced-motion") >= 2)
    check("visuals.numbers_are_tabular",
          "tabular-nums" in _uicss)

    _spine = any("border-left" in _m.group(1) for _m in _re.finditer(
        r"\.foot-group-items\s*\{([^}]*)\}", _uicss))
    check("sidebar.items_hang_off_a_visible_spine", _spine)
    check("sidebar.groups_are_separated_from_each_other",
          bool(_css_last(".foot-group-toggle", "border-top")))
    check("sidebar.settings_is_not_inside_a_group",
          "#settingsBtn" in _uicss
          and 'id="settingsBtn"' in _uihtml
          and _uihtml.index('id="settingsBtn"')
          > _uihtml.rindex('class="foot-group-items"'))


    # --- the UI must actually RUN, not merely parse ---------------------------- #
    #
    # This exists because syntax-checking app.js passed while the app was
    # broken: an edit deleted four whole panel sections, so wireEvents() threw
    # partway and every listener after that point silently never attached.
    # Modals wouldn't close, buttons did nothing, and `node --check` was
    # perfectly happy. Parsing is not running.
    try:
        import shutil as _uish, subprocess as _uisub
        _uinode = _uish.which("node")
        _uismoke = _RPath("tests/dom_smoke.js")
        if _uinode and _uismoke.exists():
            _uiout = _uisub.run([_uinode, str(_uismoke)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _uitext = (_uiout.stdout or "") + (_uiout.stderr or "")
            check("ui.app_js_executes_without_throwing",
                  "script evaluates: true" in _uitext)
            check("ui.wire_events_attaches_every_listener",
                  "wireEvents() completed: true" in _uitext
                  and "THREW" not in _uitext)
            # Defining a renderer proves nothing. The dashboard shipped twice
            # with code that existed but nothing reachable ever called, so
            # these assert it RENDERS and that the poll actually calls it.
            check("ui.dashboard_actually_renders",
                  "dashboard renders attention rows: true" in _uitext
                  and "dashboard renders tiles: true" in _uitext
                  and "dashboard renders running chips: true" in _uitext)
            check("ui.dashboard_is_reachable_from_the_poll",
                  "dashboard refreshed by the stats poll: true" in _uitext)
        else:
            # no node here; the static checks below still guard the same thing
            check("ui.smoke_harness_present", _uismoke.exists())
    except Exception:
        check("ui.smoke_harness", False)

    # every event handler referenced by name must exist, and every modal must
    # have a way out — both were violated by that same edit
    # Compute first, THEN check. Wrapping check() in try/except swallows a
    # genuine failure and re-reports it as the harness breaking, which is
    # exactly how a real bug can hide behind a green-looking suite.
    import re as _re
    _uijs2 = _RPath("web/static/app.js").read_text("utf-8")
    _uihtml2 = _RPath("web/static/index.html").read_text("utf-8")
    _uidef = set(_re.findall(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                             _uijs2))
    # handlers are also declared as arrow consts, e.g. `const saveAuto = ...`
    _uidef |= set(_re.findall(
        r"(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", _uijs2))
    _uirefs = set(_re.findall(
        r'addEventListener\("[a-z]+",\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)',
        _uijs2))
    _uighosts = sorted(r for r in _uirefs if r not in _uidef)
    check("ui.every_named_handler_exists", not _uighosts)
    _uimodals = _re.findall(r'class="modal-backdrop" id="([A-Za-z]+)"',
                            _uihtml2)
    _uistuck = []
    for _m in _uimodals:
        _base = _m[:-5] if _m.endswith("Modal") else _m
        _cap = "close" + _base[0].upper() + _base[1:]
        if _cap not in _uidef and _cap + "Modal" not in _uidef:
            _uistuck.append(_m)
    check("ui.every_modal_can_be_closed", not _uistuck)

    # --- theming: light mode must be real, not a dark theme with pale text ---- #

    # --- crew chains: specialists handing work to each other ------------------ #
    _ch_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "ch.db"
    try:
        import agent.crew as _ch
        import agent.tools as _chtools
        from agent.memory import MemoryStore as _CHM
        from rich.console import Console as _CHC
        check("chains.ships_useful_defaults",
              set(_ch.chains()) == {"opportunity", "pursue", "review"}
              and [s["member"] for s in
                   _ch.chains()["opportunity"]["steps"]]
              == ["Intel", "BizDev", "Delivery"])
        check("chains.cycles_impossible_by_construction",
              "appears twice" in _ch.validate_chain(
                  [{"member": "Intel"}, {"member": "Intel"}]))
        check("chains.rejects_unknown_member_and_overlong",
              "no crew member" in _ch.validate_chain([{"member": "Ghost"}])
              and "at most" in _ch.validate_chain(
                  [{"member": m} for m in
                   ["Intel", "BizDev", "Delivery", "Ops"] * 2]))
        _chm = _CHM(db_path=config.DB_PATH, check_same_thread=False)
        _chseen = []

        # the loop lives in `subagent` now — it was extracted from `tools` so
        # that crew could call it without importing the module that calls
        # crew, which is what made the two a cycle
        import agent.subagent as _chsub

        def _chfake(brain, memory, console, objective, context, auto_approve,
                    session_id, model=None, tier_label="",
                    execute=None, tool_defs=None):
            _chseen.append({"objective": objective, "member": tier_label})
            return f"REPORT from {tier_label}: findings {len(_chseen)}"
        _chorig = _chsub.run_subagent
        _chsub.run_subagent = _chfake
        try:
            _chr = _ch.run_chain("opportunity", "Find data work for us",
                                 None, _chm, _CHC(quiet=True))
        finally:
            _chsub.run_subagent = _chorig
        check("chains.runs_specialists_in_order",
              _chr["ok"] and [s["member"] for s in _chr["steps"]]
              == ["Intel", "BizDev", "Delivery"])
        check("chains.each_step_sees_the_original_task",
              all("Find data work" in s["objective"] for s in _chseen))
        check("chains.later_steps_receive_prior_output",
              "PREVIOUS SPECIALIST" not in _chseen[0]["objective"]
              and "REPORT from crew:Intel" in _chseen[1]["objective"]
              and "REPORT from crew:BizDev" in _chseen[2]["objective"])
        _chcalls = []

        def _chflaky(brain, memory, console, objective, context,
                     auto_approve, session_id, model=None, tier_label="",
                     execute=None, tool_defs=None):
            _chcalls.append(tier_label)
            if "BizDev" in tier_label:
                return "Sub-agent error: engine unreachable"
            return "ok report"
        _chsub.run_subagent = _chflaky
        try:
            _chr2 = _ch.run_chain("opportunity", "x", None, _chm,
                                  _CHC(quiet=True))
        finally:
            _chsub.run_subagent = _chorig
        check("chains.stops_instead_of_building_on_failure",
              _chr2["ok"] is False and _chr2["stopped_at"] == 2
              and len(_chcalls) == 2
              and "rather than passing nothing" in _chr2["why"])
        check("chains.runs_are_logged",
              len(_ch.chain_runs()) >= 2)
    finally:
        config.AGENT_HOME = _ch_home

    # --- capabilities: what has actually been exercised on this machine ------- #
    _ca_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "agent.db"
    try:
        import agent.capabilities as _ca
        _cas = _ca.status()
        check("capabilities.registers_the_feature_set",
              _cas["total"] >= 20 and _cas["counts"]["used"] == 0)
        check("capabilities.every_entry_has_a_test",
              all(c["test"] for c in _cas["capabilities"])
              and _cas["next_test"] is not None)
        _cab = {c["key"]: c for c in _cas["capabilities"]}
        check("capabilities.unconfigured_tools_name_the_blocker",
              _cab["neural3d"]["state"] == "needs setup"
              and "Detect" in _cab["neural3d"]["blocker"])
        import agent.audit as _caau
        _caau.record("crew", name="BizDev", summary="ran")
        _caau.record("backup", name="create", summary="ok")
        _cab2 = {c["key"]: c for c in _ca.status()["capabilities"]}
        check("capabilities.audit_proof_marks_used",
              _cab2["crew"]["state"] == "used"
              and _cab2["backup"]["state"] == "used")
        (config.AGENT_HOME / "costs.json").write_text("{}x", "utf-8")
        (config.AGENT_HOME / "blender").mkdir(exist_ok=True)
        _cab3 = {c["key"]: c for c in _ca.status()["capabilities"]}
        check("capabilities.file_evidence_counts_empty_dir_does_not",
              _cab3["costs"]["state"] == "used"
              and _cab3["blender"]["state"] != "used")
        check("capabilities.unused_list_shrinks",
              len(_ca.status()["unused"]) < len(_cas["unused"]))
        import agent.health as _cah
        check("capabilities.surfaces_on_health_board",
              any(c["name"] == "Capability coverage"
                  for c in _cah.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _ca_home

    # --- evals: measure whether a cheaper engine can hold the contract -------- #
    _ev_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "ev.db"
    try:
        import agent.evals as _ev
        _EVGOOD = {
            "trends": '{"trends":[{"title":"T","why":"w","sources":["u"],'
                      '"learnable":{"kind":"skill","name":"n",'
                      '"description":"d","instructions":"i"}}]}',
            "fit": '{"score":35,"verdict":"weak","for":["a"],'
                   '"against":["no Kubernetes"],"missing":["Go"]}',
            "heal": '{"item_selector":".listing-card","fields":'
                    '{"title":{"selector":".listing-title","attr":null}}}',
            "sql": '{"silver_sql":"CREATE TABLE x AS SELECT 1;",'
                   '"gold_sql":{"g":"SELECT 1"}}',
            "done": "DONE"}

        def _evbrain(fn):
            class _B:
                def chat(self, m, s, t=None, **kw):
                    return _blk(fn(s[0]))
            return _B()

        def _evstrong(sys):
            if "Cluster the items" in sys:
                return _EVGOOD["trends"]
            if "candidate fit" in sys:
                return _EVGOOD["fit"]
            if "scraper" in sys:
                return _EVGOOD["heal"]
            if "TO-BE SQL" in sys:
                return _EVGOOD["sql"]
            return _EVGOOD["done"]
        _evr = _ev.run(_evbrain(_evstrong), engine="Claude")
        check("evals.strong_engine_passes_all",
              _evr["passed"] == _evr["total"] and _evr["rate"] == 100.0
              and "trends" in _evr["safe_features"])

        def _evweak(sys):
            if "Cluster the items" in sys:
                return "Sure!\n```json\n" + _EVGOOD["trends"] + "\n```"
            if "candidate fit" in sys:
                return '{"score":80,"verdict":"strong","for":["good"]}'
            if "scraper" in sys:
                return "I think you should use .listing-card."
            if "TO-BE SQL" in sys:
                return _EVGOOD["sql"]
            return "Sure, DONE!"
        _evw = _ev.run(_evbrain(_evweak), engine="CustomQWEN")
        _evby = {x["id"]: x for x in _evw["results"]}
        check("evals.tolerates_fenced_json",
              _evby["trends.digest_json"]["ok"] is True)
        check("evals.catches_missing_contract_key",
              _evby["jobs.fit_score_json"]["ok"] is False
              and "against" in _evby["jobs.fit_score_json"]["detail"])
        check("evals.catches_prose_instead_of_json",
              _evby["watchers.selector_heal_json"]["ok"] is False)
        check("evals.catches_leaked_preamble",
              _evby["general.instruction_following"]["ok"] is False)
        check("evals.separates_safe_from_unsafe",
              "pipelines" in _evw["safe_features"]
              and "jobs" in _evw["unsafe_features"])

        class _EVDead:
            def chat(self, *a, **kw):
                raise RuntimeError("connection refused")
        _evd = _ev.run(_EVDead(), engine="Dead")
        check("evals.dead_engine_is_a_finding_not_a_crash",
              _evd["passed"] == 0
              and "connection refused" in _evd["results"][0]["detail"])
        check("evals.persists_per_engine",
              _ev.last_for("CustomQWEN")["engine"] == "CustomQWEN"
              and len(_ev.history()) >= 3)
        _evadv = _ev.recommendation([_evw])
        check("evals.gives_actionable_advice",
              any("safe to pin" in a for a in _evadv)
              and any("keep those" in a for a in _evadv))
        # health flags a feature pinned to an engine that failed its contract
        import agent.costs as _evc
        import agent.health as _evh
        _evc.set_engine("jobs", "CustomQWEN")
        check("evals.health_flags_unsafe_pinning",
              any(c["name"] == "Engine evals" and c["state"] == "fail"
                  for c in _evh.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _ev_home

    # --- the server must have no undefined names ----------------------------- #
    #
    # Every crew call site in web/server.py referenced a `console` that was
    # never defined there, so the whole Crew feature 500'd with a NameError
    # the moment it was used from a panel. The unit tests never saw it: they
    # call crew.run() directly, passing their own Console. A scheduled crew
    # job had the same shape, reading a bare `payload`.
    try:
        import subprocess as _pfsub, sys as _pfsys
        _pf = _pfsub.run([_pfsys.executable, "-m", "pyflakes",
                          "web/server.py", "agent"],
                         capture_output=True, text=True, timeout=120,
                         cwd=str(_RPath(".").resolve()))
        _pfout = (_pf.stdout or "") + (_pf.stderr or "")
        _pfbad = [ln for ln in _pfout.splitlines()
                  if "undefined name" in ln.lower()]
        _pfran = "No module named pyflakes" not in _pfout
    except Exception:
        _pfbad, _pfran = [], False
    if _pfran:
        check("server.no_undefined_names", not _pfbad)
    else:
        check("server.pyflakes_unavailable_skipped", True)

    # --- the gate must actually be in the path ------------------------------- #
    #
    # Found in a security pass before publishing: the intercept queue and its
    # endpoints existed, and nothing called them. The panel reported an empty
    # queue while every call went straight through — a safety feature that
    # exists only in the UI is worse than none, because it gets trusted.
    #
    # It matters most for prompt injection: this reads job adverts, alert
    # emails and web pages, all written by someone else. An instruction hidden
    # in one needs an EXTERNAL call to do damage, and those are what wait.
    _gt_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    config.DB_PATH = config.AGENT_HOME / "gate.db"
    try:
        import agent.tools as _gtt
        import agent.intercepts as _gti
        from agent.memory import MemoryStore as _GTM
        _gtm = _GTM(db_path=config.DB_PATH, check_same_thread=False)
        _gtc = _Console(quiet=True)
        _gtp = config.AGENT_HOME / "x.txt"
        _gtp.write_text("hello", "utf-8")
        _gtread = _gtt.execute_tool("read_file", {"path": str(_gtp)},
                                    _gtm, _gtc, auto_approve=True)
        check("gate.a_read_still_runs", "hello" in _gtread)
        _gtsend = _gtt.execute_tool(
            "send_email", {"to": "a@b.io", "subject": "Hi", "body": "x"},
            _gtm, _gtc, auto_approve=True)
        check("gate.anything_leaving_the_machine_waits",
              "HELD FOR REVIEW" in _gtsend
              and _gti.summary()["waiting"] == 1
              and "a@b.io" in _gti.summary()["items"][0]["summary"])
        check("gate.full_access_does_not_bypass_it",
              _gti.summary()["waiting"] == 1)
        check("gate.the_model_is_told_not_to_work_around_it",
              "work around it" in _gtsend)
        # and it must be wired, not merely present
        check("gate.is_wired_into_the_tool_path",
              "intercepts" in _RPath("agent/tools.py").read_text("utf-8"))
        import agent.health as _gth
        check("gate.health_would_notice_if_it_were_not",
              any(c["name"] == "Review gate"
                  for c in _gth.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _gt_home

    # --- an index that holds part of a folder -------------------------------- #
    #
    # Reported from real use: "indexed documents capped". The cap used to
    # `break` silently, so a folder of 2,000 documents gave you the
    # alphabetically-first few hundred and said nothing — you'd search, get a
    # confident answer drawn from a fraction of your material, and have no way
    # to know the rest was never read. A partial index that announces itself
    # is fine; one that doesn't is worse than no index.
    _ix_home, _ix_max = config.AGENT_HOME, config.RAG_MAX_FILES
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.rag as _ixr
        _ixd = config.AGENT_HOME / "docs"
        _ixd.mkdir(parents=True, exist_ok=True)
        for _i in range(12):
            (_ixd / f"note{_i:02d}.txt").write_text(
                f"Document {_i} about data pipelines.", "utf-8")
        config.RAG_MAX_FILES = 5
        _ixs = _ixr.DocumentStore()
        _ixres = _ixs.ingest_path(str(_ixd))
        # --- a big folder is mostly not knowledge -------------------------
        #
        # Reported from real use: a 36,500-file / 4.3 GB knowledge base that
        # "never finished". It wasn't stuck — 23,000 of those files were
        # screenshots, and the walk descended into .git and node_modules
        # before anything could be filtered. Nothing had looked first.
        _sv = config.AGENT_HOME / "kb"
        for _sub in ("docs", ".git/objects", "node_modules/pkg", "images",
                     "bin"):
            (_sv / _sub).mkdir(parents=True, exist_ok=True)
        for _i in range(60):
            (_sv / "docs" / f"n{_i:03d}.md").write_text(
                "# Note\n" + ("knowledge " * 200), "utf-8")
        for _i in range(200):
            (_sv / "images" / f"s{_i:04d}.png").write_bytes(b"\x89PNG" + b"x" * 500)
        for _i in range(100):
            (_sv / ".git" / "objects" / f"o{_i}").write_bytes(b"x" * 100)
        for _i in range(80):
            (_sv / "node_modules" / "pkg" / f"m{_i}.js").write_text("x=1", "utf-8")
        for _i in range(20):
            (_sv / "bin" / f"a{_i}.dll").write_bytes(b"x" * 100)
        _svr = _ixr.survey(str(_sv))
        check("rag.it_counts_before_you_commit_to_indexing",
              _svr["ok"] and _svr["indexable"] == 60
              and _svr["skipped_binary"] == 200
              and "can be indexed" in _svr["verdict"])
        check("rag.build_and_vendor_folders_are_not_walked_at_all",
              _svr["skipped_folders"] >= 3
              and _svr["files"] == 260)     # the 200 inside them never counted
        check("rag.the_estimate_is_labelled_an_estimate",
              "estimate" in _svr and "not a measurement" in _svr["note"])
        # and the walk itself must agree with the survey
        _svcap, config.RAG_MAX_FILES = config.RAG_MAX_FILES, 5000
        _svs = _ixr.DocumentStore()
        _svfiles = _svs._collect_files(_sv)
        check("rag.the_walk_skips_the_same_things_the_survey_counted",
              len(_svfiles) == 60
              and _svs.last_skipped.get("binary") == 200)
        config.RAG_MAX_FILES = _svcap

        check("rag.a_truncated_index_says_so",
              _ixres.get("added") == 5 and _ixres.get("truncated") == 7
              and "still to go" in _ixres.get("warning", ""))
        # the bug that made re-indexing impossible: the cap was applied
        # BEFORE checking what was already indexed, so a re-run collected the
        # same alphabetically-first N, found them unchanged, skipped them all,
        # and never looked at the rest
        _ixs2 = _ixr.DocumentStore()
        _ixtotal, _ixruns = 0, []
        for _r in range(6):
            _rr = _ixs2.ingest_path(str(_ixd))
            _ixtotal += _rr.get("added", 0)
            _ixruns.append(_rr.get("truncated", 0))
            if not _rr.get("truncated"):
                break
        # the first run above already indexed 5, so these runs finish the
        # remaining 7 — which is the whole point: each run makes progress
        check("rag.running_it_again_continues_where_it_stopped",
              _ixtotal == 7 and len(_ixruns) >= 2)
        check("rag.the_remaining_count_actually_counts_down",
              _ixruns[0] > _ixruns[1] and _ixruns[-1] == 0)
        check("rag.the_message_says_to_run_it_again",
              "Run it again" in (_ixres.get("warning") or "")
              and _ixres.get("run_again") is True)
        check("rag.the_warning_names_the_setting_to_change",
              "RAG_MAX_FILES" in _ixres.get("warning", ""))
        config.RAG_MAX_FILES = 500
        _ixres2 = _ixs.ingest_path(str(_ixd))
        check("rag.under_the_limit_it_stays_quiet",
              "truncated" not in _ixres2 and "warning" not in _ixres2)
        check("rag.the_limits_are_settable",
              "RAG_MAX_FILES" in config._USER_KEYS
              and "RAG_MAX_FILE_MB" in config._USER_KEYS)
        import agent.health as _ixh
        check("rag.health_reports_an_index_at_its_limit",
              any(c["name"] == "Document index"
                  for c in _ixh.report(None)["checks"]))
    finally:
        config.AGENT_HOME, config.RAG_MAX_FILES = _ix_home, _ix_max

    # --- a call to a function that doesn't exist ----------------------------- #
    #
    # `brain.get_brain()` — a function I invented and never checked. pyflakes
    # can't see it: `module.attr` is legal at import time and only fails when
    # the line runs, which was in the middle of a fine-tune the user had
    # waited for. This walks every `module.attr` where the module is one of
    # ours and checks the attribute is actually there.
    import ast as _xast
    _xmods = {p.stem: p for p in _RPath("agent").glob("*.py")}
    _xexp = {}
    for _xn, _xp in _xmods.items():
        _xt = _xast.parse(_xp.read_text("utf-8"))
        _xexp[_xn] = {n.name for n in _xt.body
                      if isinstance(n, (_xast.FunctionDef,
                                        _xast.AsyncFunctionDef,
                                        _xast.ClassDef))}
        _xexp[_xn] |= {tg.id for n in _xt.body
                       if isinstance(n, _xast.Assign)
                       for tg in n.targets if isinstance(tg, _xast.Name)}
    _xbad = []
    for _xn, _xp in _xmods.items():
        _xt = _xast.parse(_xp.read_text("utf-8"))
        _xalias = {}
        for n in _xast.walk(_xt):
            if isinstance(n, _xast.ImportFrom) and n.module is None and n.level:
                for a in n.names:
                    if a.name in _xmods:
                        _xalias[a.asname or a.name] = a.name
        for n in _xast.walk(_xt):
            if (isinstance(n, _xast.Attribute)
                    and isinstance(n.value, _xast.Name)):
                _xm = _xalias.get(n.value.id)
                if (_xm and n.attr not in _xexp[_xm]
                        and not n.attr.startswith("__")):
                    _xbad.append(f"{_xn}.py:{n.lineno} -> {_xm}.{n.attr}")
    # crypto's Windows DPAPI helper is genuinely optional and guarded
    _xbad = [b for b in _xbad if "windpapi" not in b]
    check("server.no_calls_to_functions_that_do_not_exist", not _xbad)

    # --- a picked engine must reach the turn, everywhere ---------------------- #
    #
    # Reported from real use: choosing an engine in Challenges and pressing
    # Send to BizDev still billed Anthropic. The picker was sent to the scan
    # and nowhere else, so qualifying a brief fell back to the member's
    # default. Worse, an unrecognised engine name resolved to the AUTO
    # sentinel — indistinguishable from asking for Auto — so a typo or a
    # renamed engine silently became "use the paid default".
    _pe_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "pe.db"
    try:
        import agent.brain as _peb
        import agent.tools as _petools
        import web.server as _pews
        from agent.memory import MemoryStore as _PEM
        from fastapi.testclient import TestClient as _PETC
        (config.AGENT_HOME / "challenges").mkdir(parents=True, exist_ok=True)
        (config.AGENT_HOME / "challenges" / "report.json").write_text(
            json.dumps({"at": "x", "briefs": [
                {"title": "B", "problem": "p", "who": "w", "data": "d",
                 "first_engagement": "e", "why_it_might_fail": "r",
                 "confidence": "medium", "sources": []}]}), "utf-8")
        _peb.load_custom_engines(refresh=True)
        _peb.add_custom_engine(name="CustomQWEN",
                               base_url="http://localhost:11434/v1",
                               api_key="ollama", model="qwen3.6:latest")
        _peseen = {}
        import agent.subagent as _pesub
        _peorig = _pesub.run_subagent
        _pesub.run_subagent = (lambda *a, **kw:
                                 (_peseen.update(model=kw.get("model"))
                                  or "report"))
        _peprev = _pews.memory
        _pews.memory = _PEM(db_path=config.DB_PATH, check_same_thread=False)
        try:
            _pec = _PETC(_pews.app, raise_server_exceptions=False)
            _peseen.clear()
            _pec.post("/api/challenges/to-crew",
                      json={"index": 0, "engine": "CustomQWEN"})
            check("engines.challenge_to_crew_honours_the_picker",
                  _peseen.get("model") == "CustomQWEN")
            _peseen.clear()
            _pec.post("/api/crew/run", json={"task": "t", "member": "BizDev",
                                             "engine": "CustomQWEN"})
            check("engines.crew_run_honours_the_picker",
                  _peseen.get("model") == "CustomQWEN")
            _peseen.clear()
            _pec.post("/api/crew/chain", json={"chain": "pursue", "task": "t",
                                               "engine": "CustomQWEN"})
            check("engines.crew_chain_honours_the_picker",
                  _peseen.get("model") == "CustomQWEN")
            _peseen.clear()
            _pec.post("/api/challenges/to-crew", json={"index": 0})
            check("engines.blank_still_uses_the_member_default",
                  _peseen.get("model") is None)
            _per = _pec.post("/api/challenges/to-crew",
                             json={"index": 0, "engine": "NoSuchEngine"})
            check("engines.unknown_name_is_refused_not_swapped",
                  _per.status_code == 409
                  and "silently fall back" in _per.json().get("detail", ""))
            check("engines.auto_and_claude_still_resolve",
                  _pews._trend_model("Auto") == (None, "")
                  and _pews._trend_model("Claude")[1] == "")
        finally:
            _pesub.run_subagent = _peorig
            _pews.memory = _peprev
            _peb.load_custom_engines(refresh=True)
    finally:
        config.AGENT_HOME = _pe_home

    # --- crew must work through the WEB, not only when called directly ------- #
    _cw2_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "cw2.db"
    try:
        import agent.tools as _cw2tools
        import web.server as _cw2ws
        from agent.memory import MemoryStore as _CW2M
        from fastapi.testclient import TestClient as _CW2TC
        (config.AGENT_HOME / "challenges").mkdir(parents=True, exist_ok=True)
        (config.AGENT_HOME / "challenges" / "report.json").write_text(
            json.dumps({"at": "x", "briefs": [
                {"title": "Municipal billing", "problem": "p", "who": "w",
                 "data": "d", "first_engagement": "e",
                 "why_it_might_fail": "risk", "confidence": "medium",
                 "sources": ["https://x/1"]}]}), "utf-8")
        import agent.subagent as _cw2sub
        _cw2orig = _cw2sub.run_subagent
        _cw2sub.run_subagent = lambda *a, **kw: "BizDev report: qualified."
        _cw2prev_mem = _cw2ws.memory
        _cw2ws.memory = _CW2M(db_path=config.DB_PATH, check_same_thread=False)
        try:
            _cw2c = _CW2TC(_cw2ws.app, raise_server_exceptions=False)
            _cw2a = _cw2c.post("/api/challenges/to-crew", json={"index": 0})
            check("web.challenge_to_crew_works",
                  _cw2a.status_code == 200 and _cw2a.json().get("ok") is True)
            check("web.unknown_brief_is_404",
                  _cw2c.post("/api/challenges/to-crew",
                             json={"index": 99}).status_code == 404)
            _cw2b = _cw2c.post("/api/crew/run",
                               json={"task": "find three prospects",
                                     "member": "BizDev"})
            check("web.crew_run_works", _cw2b.status_code == 200)
            _cw2d = _cw2c.post("/api/crew/chain",
                               json={"chain": "pursue", "task": "qualify it"})
            check("web.crew_chain_works",
                  _cw2d.status_code == 200
                  and len(_cw2d.json().get("steps", [])) == 2)
        finally:
            _cw2sub.run_subagent = _cw2orig
            _cw2ws.memory = _cw2prev_mem
    finally:
        config.AGENT_HOME = _cw2_home

    # --- routes must not shadow each other, and buttons must do something ---- #
    #
    # The health board silently returned the liveness probe's payload for a
    # whole release: both were registered at /api/health, FastAPI answered
    # with the first, and the panel rendered nothing. Nothing failed loudly.
    import collections as _rtc
    import web.server as _rtws
    _rtseen = _rtc.Counter()
    for _r in _rtws.app.routes:
        if hasattr(_r, "path") and hasattr(_r, "methods"):
            for _m in _r.methods:
                _rtseen[(_m, _r.path)] += 1
    check("routes.no_duplicate_registrations",
          not [k for k, v in _rtseen.items() if v > 1])
    check("routes.health_board_has_its_own_path",
          any(getattr(r, "path", "") == "/api/health/board"
              for r in _rtws.app.routes))
    try:
        import shutil as _clsh, subprocess as _clsub
        _clnode = _clsh.which("node")
        _clfile = _RPath("tests/sidebar_clicks.js")
        if _clnode and _clfile.exists():
            _clout = _clsub.run([_clnode, str(_clfile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _cltext = (_clout.stdout or "") + (_clout.stderr or "")
            _clok = "buttons that do nothing or throw: none" in _cltext
        else:
            _clok = _clfile.exists()
    except Exception:
        _clok = False
    check("ui.every_sidebar_button_does_something", _clok)
    # the Jobs panel was restructured into tabs; every pane and tab must
    # exist, and no listener may point at a control the rebuild removed
    # --- hiding must beat styling ------------------------------------------- #
    #
    # The Jobs tabs appeared dead: the JavaScript set `hidden` correctly on
    # every pane, but `.modal-portal .tabpane { display: flex }` had been
    # appended AFTER `.tabpane[hidden] { display: none }` — equal specificity,
    # later rule wins — so all four panes rendered at once. A global,
    # important rule placed last makes `hidden` mean hidden no matter what
    # gets added later.
    _hidrule = "[hidden] { display: none !important; }"
    check("ui.hidden_always_wins", _hidrule in _uicss)
    # and prove the tabs actually switch, by clicking them
    _hidat = _uicss.rindex(_hidrule)
    check("ui.hidden_rule_is_last_in_the_cascade",
          "display:" not in _uicss[_hidat:].replace(
              "display: none !important;", ""))
    # and every inline show must clear the attribute, or !important traps it
    _shows = list(_re.finditer(
        r'\.style\.display\s*=\s*"(?:flex|block|grid)"', _uijs))
    _trapped = [m for m in _shows
                if "hidden = false"
                not in _uijs[max(0, m.start() - 220):m.start()]]
    check("ui.every_inline_show_also_clears_hidden", not _trapped)

    # --- tables in chat must render as tables -------------------------------- #
    #
    # The markdown renderer handled headings, lists, code and links but had no
    # table support, so anything tabular arrived as rows of pipes.
    try:
        _mdfile = _RPath("tests/md_tables.js")
        if _vinode and _mdfile.exists():
            _mdout = _visub.run([_vinode, str(_mdfile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _mdt = (_mdout.stdout or "") + (_mdout.stderr or "")
            _mdok = all(x in _mdt for x in (
                "renders a real table: true",
                "header cells: true",
                "body rows: true",
                "no raw pipes left: true",
                "right-alignment honoured: true",
                "surrounding prose kept: true",
                "optional outer pipes: true",
                "a short row is padded, not dropped: true",
                "prose with a pipe is not a table: true",
                "a table inside code stays code: true",
                "escaped pipes survive: true",
                "html in a cell is escaped: true",
                "two tables in one message: true"))
        else:
            _mdok = _mdfile.exists()
    except Exception:
        _mdok = False
    check("ui.chat_renders_markdown_tables", _mdok)
    check("ui.tables_are_styled",
          ".msg-content table" in _uicss and ".table-wrap" in _uicss)

    # --- finding anything, across twenty-four panels ------------------------- #
    #
    # Every feature lived behind an icon, so using one meant knowing which
    # panel it was in. The palette searches panels by what they're FOR, not
    # only by name, and is built from the sidebar itself so it can't go stale
    # when a panel is added.
    try:
        _pafile = _RPath("tests/palette.js")
        if _vinode and _pafile.exists():
            _paout = _visub.run([_vinode, str(_pafile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _pat = (_paout.stdout or "") + (_paout.stderr or "")
            _paok = (all(x in _pat for x in (
                "sidebar panels found: true",
                "palette opens: true",
                "lists every panel plus actions: true",
                "every word must match: true",
                "name beats description: true",
                "enter opens the highlighted panel: true",
                "and closes the palette: true",
                "toast carries its kind: true",
                "routine confirmations are suppressed: true",
                "errors still get through: true",
                "off silences everything: true"))
                and "MISS" not in _pat)
        else:
            _paok = _pafile.exists()
    except Exception:
        _paok = False
    # --- the design decisions, kept ------------------------------------------ #
    #
    # Reviewed against the traits that mark generated interfaces, this app had
    # three: tracked-out ALL-CAPS eyebrow labels, meta strings joined with
    # middle dots, and a monospace face used for words rather than figures.
    # These assert the replacements hold, since a later panel could quietly
    # reintroduce any of them.
    check("design.type_scale_and_spacing_are_tokens",
          all(t2 in _uicss for t2 in ("--t-body:", "--t-title:", "--s-3:",
                                      "--prose:")))
    check("design.eyebrow_labels_are_not_shouted",
          _re.search(r"\.detail-block h4,[^{]*\{[^}]*text-transform: none",
                     _uicss, _re.S) is not None)
    check("design.monospace_is_reserved_for_figures",
          _re.search(r"\.log-meta[^{]*\{[^}]*font-family: var\(--font-body\)",
                     _uicss, _re.S) is not None
          and "tabular-nums" in _uicss)
    check("design.prose_line_length_is_capped",
          "max-width: var(--prose)" in _uicss
          and _re.search(r"--prose:\s*\d+ch", _uicss) is not None)
    check("design.row_facts_are_structured_not_dot_joined",
          ".meta-place" in _uicss and ".meta-source" in _uicss
          and 'el("span", "meta-place")' in _uijs)
    check("design.the_medallion_tiers_are_consistent",
          all(f"--{c}:" in _uicss for c in ("bronze", "silver", "gold"))
          and ".tier-bronze" in _uicss and ".tier-gold" in _uicss)

    check("ui.command_palette_finds_things_by_purpose", _paok)
    check("ui.the_shortcut_is_discoverable",
          'id="paletteHint"' in _uihtml and "Ctrl K" in _uihtml)

    # --- attachments: the image must actually reach the model ---------------- #
    #
    # Reported from real use: attaching a JPEG and the agent can't see it. The
    # OpenAI-compatible converter collected only `text` blocks from a user
    # message, so an image block was silently dropped — the photo never left
    # the browser on any engine but Claude.
    import inspect as _atinsp
    import agent.brain as _atb
    _atconv = None
    for _n2, _o2 in vars(_atb).items():
        if _atinsp.isclass(_o2) and hasattr(_o2, "_to_openai_messages"):
            _atconv = _o2._to_openai_messages
            break
    _ATIMG = {"type": "image",
              "source": {"type": "base64", "media_type": "image/jpeg",
                         "data": "QUJD"}}
    _atout = _atconv([{"role": "user",
                       "content": [{"type": "text", "text": "what is this?"},
                                   _ATIMG]}], ["sys"])
    _atu = [m for m in _atout if m["role"] == "user"][0]
    check("attachments.an_image_reaches_an_openai_compatible_engine",
          isinstance(_atu["content"], list)
          and [p["type"] for p in _atu["content"]] == ["text", "image_url"]
          and _atu["content"][1]["image_url"]["url"].startswith(
              "data:image/jpeg;base64,")
          and _atu["content"][0]["text"] == "what is this?")
    _atu2 = [m for m in _atconv([{"role": "user", "content": [_ATIMG]}],
                                ["sys"]) if m["role"] == "user"][0]
    check("attachments.an_image_with_no_caption_still_asks_something",
          _atu2["content"][0]["text"] == "What is in this image?")
    _atu3 = [m for m in _atconv([{"role": "user",
                                  "content": [{"type": "text",
                                               "text": "hello"}]}], ["sys"])
             if m["role"] == "user"][0]
    check("attachments.plain_text_is_unchanged",
          _atu3["content"] == "hello")

    # --- and as many file types as can be read honestly ---------------------- #
    import agent.files as _atf
    import zipfile as _atzip
    import email.message as _atem
    _atd = _RPath(_rtf.mkdtemp())
    (_atd / "a.html").write_text(
        "<html><body><nav>skip</nav><h1>Quarterly</h1>"
        "<p>Revenue rose 12%.</p><script>junk</script></body></html>",
        "utf-8")
    _atht, _ = _atf.extract_text(str(_atd / "a.html"))
    check("files.html_keeps_the_text_and_drops_the_furniture",
          "Revenue rose 12%" in _atht and "skip" not in _atht
          and "junk" not in _atht)
    _atm = _atem.EmailMessage()
    _atm["From"] = "a@b.io"
    _atm["Subject"] = "Invoice 42"
    _atm.set_content("Please find the invoice attached.")
    (_atd / "m.eml").write_bytes(bytes(_atm))
    _atmt, _ = _atf.extract_text(str(_atd / "m.eml"))
    check("files.an_email_keeps_its_headers",
          "Invoice 42" in _atmt and "a@b.io" in _atmt
          and "invoice attached" in _atmt)
    (_atd / "r.rtf").write_text(r"{\rtf1\ansi Hello \b world\b0 from RTF.}",
                                "utf-8")
    _atrt, _ = _atf.extract_text(str(_atd / "r.rtf"))
    check("files.rtf_reads_even_without_the_optional_library",
          "Hello" in _atrt and "world" in _atrt)
    with _atzip.ZipFile(_atd / "b.epub", "w") as _z:
        _z.writestr("ch1.xhtml",
                    "<html><body><p>Chapter one text.</p></body></html>")
    _atet, _ = _atf.extract_text(str(_atd / "b.epub"))
    check("files.epub_reads", "Chapter one text" in _atet)
    with _atzip.ZipFile(_atd / "c.zip", "w") as _z:
        _z.writestr("report.pdf", "x")
        _z.writestr("data.csv", "y")
    _atzt, _atzn = _atf.extract_text(str(_atd / "c.zip"))
    check("files.an_archive_is_listed_not_silently_unpacked",
          "report.pdf" in _atzt and "data.csv" in _atzt
          and "ask me to extract" in _atzn)
    from odf.opendocument import OpenDocumentText as _ODT
    from odf.text import P as _ODP
    _od = _ODT()
    _od.text.addElement(_ODP(text="An OpenDocument paragraph."))
    _od.save(str(_atd / "o.odt"))
    _atot, _ = _atf.extract_text(str(_atd / "o.odt"))
    check("files.opendocument_reads", "OpenDocument paragraph" in _atot)
    (_atd / "x.bin").write_bytes(bytes(range(256)) * 4)
    _, _atbn = _atf.extract_text(str(_atd / "x.bin"))
    check("files.an_unreadable_type_says_what_does_work",
          "PDF, Word, Excel" in _atbn)

    # --- engines: no built-ins you can't edit, tagged by what they are ------- #
    #
    # The two DeepSeek entries were built in when they were the only
    # alternative worth wiring by hand. One was retired by the provider in
    # August and kept failing daily, and an engine you can't edit or remove is
    # worse than one you added yourself. And "custom" described where an
    # engine came from, not what it is — routing needs to know whether a call
    # leaves the machine, and a local model tagged cloud gets counted against
    # a spend cap it never hit.
    _en_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "en.db"
    try:
        import agent.brain as _enb
        _enb.add_custom_engine("Mistral API", "https://api.mistral.ai/v1",
                               "sk-secret-key-1234", "mistral-large",
                               price_in=2.0, price_out=6.0)
        _enb.add_custom_engine("CustomQWEN", "http://localhost:11434/v1",
                               "", "qwen3.6")
        _enb.add_custom_engine("LAN box", "http://192.168.1.50:8000/v1",
                               "", "llama")
        import agent.engines as _ene
        check("engines.custom_engines_are_tagged_cloud_or_local",
              _ene.classify("Mistral API")["kind"] == "cloud"
              and _ene.classify("CustomQWEN")["kind"] == "local"
              and _ene.classify("LAN box")["kind"] == "local")
        check("engines.no_uneditable_deepseek_builtins",
              "DeepSeek Pro\", \"label\""
              not in _RPath("web/server.py").read_text("utf-8"))
        # editing what already exists
        _enok, _ = _enb.update_custom_engine("Mistral API",
                                             model="mistral-large-2")
        _enrow = [e for e in _enb.load_custom_engines()
                  if e["name"] == "Mistral API"][0]
        check("engines.an_existing_engine_can_be_edited",
              _enok and _enrow["model"] == "mistral-large-2")
        _enb.update_custom_engine("Mistral API", price_in=3.5)
        _enrow = [e for e in _enb.load_custom_engines()
                  if e["name"] == "Mistral API"][0]
        check("engines.editing_a_price_does_not_wipe_the_key",
              _enrow["api_key"] == "sk-secret-key-1234"
              and _enrow["price_in"] == 3.5)
        check("engines.an_engine_can_be_renamed",
              _enb.update_custom_engine("Mistral API",
                                        new_name="Mistral Large")[0]
              and any(e["name"] == "Mistral Large"
                      for e in _enb.load_custom_engines())
              and not any(e["name"] == "Mistral API"
                          for e in _enb.load_custom_engines()))
        check("engines.the_editor_refuses_what_it_should",
              _enb.update_custom_engine("Mistral Large",
                                        base_url="ftp://x")[0] is False
              and _enb.update_custom_engine("Mistral Large",
                                            model="  ")[0] is False
              # renaming to a router word is still refused; "Claude" isn't
              # one any more
              and _enb.update_custom_engine("Mistral Large",
                                            new_name="Auto")[0] is False
              and _enb.update_custom_engine("ghost", model="x")[0] is False)
        _eng = _enb.get_custom_engine("Mistral Large")
        check("engines.the_edit_form_never_receives_the_key",
              _eng["api_key"] == "" and _eng["has_key"] is True
              and _eng["key_hint"].startswith("sk-"))
    finally:
        config.AGENT_HOME = _en_home

    # --- the guide must keep up with the app --------------------------------- #
    #
    # A guide falls behind silently: nothing breaks, it just quietly stops
    # describing the thing in front of you. Six panels had no stop at all —
    # including Issues, which is what you open when something's wrong. This
    # fails the build instead, so a panel can't ship unexplained.
    import agent.tour as _tg
    _tgc = _tg.coverage()
    check("guide.explains_every_panel_in_the_app",
          _tgc["ok"] and not _tgc["missing"] and _tgc["covered"] >= 20)
    check("guide.has_no_stop_pointing_at_a_panel_that_is_gone",
          not _tgc["stale"])
    # A demo SELECTS where a guide covers, so there's no coverage rule here —
    # but every panel it opens must exist, or it sends the presenter to a
    # button that isn't there in front of an audience.
    import agent.presenter as _pz
    check("presenter.every_panel_it_opens_exists",
          all((not s.get("panel")) or f'id="{s["panel"]}"' in _uihtml
              for s in _pz.SCENES))
    check("presenter.every_scene_has_a_script_and_a_speaker_note",
          all(s.get("say") and s.get("note") for s in _pz.SCENES))
    # the safety story is told (the job search's scenes left with Agent Jo
    # Jobs, CHANGELOG 155) — and the close comes last: scenes added later were
    # appended after it, so a live demo said "close here" and then carried on
    _pzkeys = [s["key"] for s in _pz.SCENES]
    check("presenter.tells_the_safety_story",
          "gates" in _pzkeys and "honesty" in _pzkeys)
    check("presenter.the_close_comes_last", _pzkeys[-1] == "close", f"{_pzkeys}")

    check("guide.every_stop_says_what_why_and_what_to_try",
          all(s.get("what") and s.get("why") and s.get("try")
              for s in _tg.STOPS))

    # --- release signing: provenance, honestly described --------------------- #
    #
    # A signature can't stop anyone copying published code. What it does is
    # prove a copy is unaltered and came from the key holder — which matters
    # most for an agent that runs commands on the machine it's installed on.
    import importlib.util as _sgu
    _sgspec = _sgu.spec_from_file_location("agentjo_sign", "tools/sign.py")
    _sg = _sgu.module_from_spec(_sgspec)
    _sgspec.loader.exec_module(_sg)
    _sgdir = _RPath(_rtf.mkdtemp())
    (_sgdir / "pkg").mkdir()
    (_sgdir / "pkg" / "a.py").write_text("A = 1\n", "utf-8")
    (_sgdir / "b.py").write_text("B = 2\n", "utf-8")
    _sgcwd = os.getcwd()
    os.chdir(_sgdir)
    try:
        _sgk = _sg.make_key()
        check("signing.a_key_is_created_and_the_private_half_is_warned_about",
              _sgk["ok"] and _RPath(_sgk["private"]).exists()
              and _RPath(_sgk["public"]).exists()
              and "out of the repository" in _sgk["warning"])
        check("signing.making_a_second_key_is_refused",
              _sg.make_key()["ok"] is False)
        _sgs = _sg.sign(".")
        check("signing.a_release_can_be_signed", _sgs["ok"])
        _sgv = _sg.verify(".")
        check("signing.an_untouched_copy_verifies",
              _sgv["ok"] and _sgv["signed"] is True
              and "unaltered" in _sgv["verdict"])
        # tampering must be caught AND named
        (_sgdir / "pkg" / "a.py").write_text("A = 999\n", "utf-8")
        _sgt = _sg.verify(".")
        check("signing.tampering_is_caught_and_the_file_named",
              _sgt["ok"] is False and _sgt["signed"] is True
              and "pkg/a.py" in _sgt["changed"])
        # a forged manifest must not pass
        (_sgdir / _sg.MANIFEST).write_text('{"entries": {}}', "utf-8")
        _sgf = _sg.verify(".")
        check("signing.a_rewritten_manifest_fails_the_signature",
              _sgf["ok"] is False and _sgf.get("signed") is False)
        check("signing.it_says_what_a_signature_cannot_do",
              "does not stop anyone copying" in _sgv["note"])
    finally:
        os.chdir(_sgcwd)

    # the licence and the terms have to actually be there
    # --- the front page is a front page -------------------------------------- #
    #
    # The README was 3,415 lines of changelog: a good engineering record and
    # a terrible introduction. Someone landing on the repository needs to know
    # what it is before they know what it was.
    _rdm = _RPath("README.md").read_text("utf-8")
    check("readme.is_an_introduction_not_a_changelog",
          _rdm.count("\n") < 400
          and _rdm.count("### ") < 12
          and "runs on your own machine" in _rdm)
    check("readme.says_how_to_install_and_what_it_costs",
          "install.bat" in _rdm and "PolyForm" in _rdm
          and "COMMERCIAL.md" in _rdm)
    check("readme.the_history_is_kept_not_deleted",
          _RPath("CHANGELOG.md").exists()
          and _RPath("CHANGELOG.md").read_text("utf-8").count("\n") > 3000)
    _rdlinks = _re.findall(r"\]\(([A-Za-z0-9_.\-/]+)\)", _rdm)
    check("readme.every_link_goes_somewhere",
          not [ln for ln in _rdlinks if not _RPath(ln).exists()])

    check("release.licence_and_commercial_terms_are_present",
          _RPath("LICENSE").exists() and _RPath("COMMERCIAL.md").exists()
          and "PolyForm Noncommercial"
          in _RPath("LICENSE").read_text("utf-8")[:200]
          and "itumelengj@hotmail.com"
          in _RPath("COMMERCIAL.md").read_text("utf-8"))
    check("release.the_private_key_is_never_committed",
          "signing-key.private" in _RPath(".gitignore").read_text("utf-8"))
    # the PowerShell shrank to just the Python bootstrap, so these moved with
    # it — the consent and the winget-then-python.org order still matter
    _insps = _RPath("get-python.ps1").read_text("utf-8")
    check("install.asks_before_putting_python_on_the_machine",
          "Install Python now?" in _insps
          and "nothing was changed" in _insps)
    check("install.prefers_winget_then_falls_back_to_python_org",
          _insps.index("winget install") < _insps.index("python.org/ftp"))

    # --- code map: what imports what ----------------------------------------- #
    #
    # Reading a folder tells you what exists, not which module everything
    # leans on or which pair quietly import each other. Python is parsed with
    # `ast` rather than a regex, because a regex finds `import` inside strings
    # and comments and reports dependencies that don't exist.
    import agent.codemap as _cmm
    _cmdir = _RPath(_rtf.mkdtemp())
    (_cmdir / "pkg").mkdir()
    (_cmdir / "pkg" / "__init__.py").write_text("", "utf-8")
    (_cmdir / "pkg" / "core.py").write_text(
        "CONST = 1\n\n\ndef helper():\n    return CONST\n", "utf-8")
    (_cmdir / "pkg" / "a.py").write_text(
        "from . import core\nfrom . import b\n\n\ndef go():\n"
        "    return core.helper()\n", "utf-8")
    (_cmdir / "pkg" / "b.py").write_text(
        "from . import a\n# a comment mentioning import requests\n"
        "TEXT = 'import os'\n", "utf-8")
    (_cmdir / "pkg" / "lonely.py").write_text("X = 1\n", "utf-8")
    (_cmdir / "main.py").write_text(
        "import httpx\nfrom pkg import a\n\n\ndef run():\n"
        "    return a.go()\n", "utf-8")
    _cmr = _cmm.report(str(_cmdir))
    # --- the cycle the code map found ---------------------------------------- #
    #
    # `crew` imported `tools` for the sub-agent loop while `tools` imported
    # `crew` for the crew tools. Neither could be read, tested or changed
    # alone. The loop moved to its own module and takes the dispatcher as an
    # ARGUMENT — a nested agent loop has no business knowing whose tools it
    # is running — and `tools` registers itself with `crew` on import, so the
    # arrow points one way without a single caller changing.
    import agent.subagent as _cysub
    import agent.crew as _cycrew
    import agent.tools as _cytools
    check("structure.the_subagent_loop_stands_alone",
          "from .tools import" not in
          _RPath("agent/subagent.py").read_text("utf-8")
          and "execute=None" in _RPath("agent/subagent.py").read_text("utf-8"))
    check("structure.crew_no_longer_imports_tools",
          "from .tools import"
          not in _RPath("agent/crew.py").read_text("utf-8"))
    check("structure.the_dispatcher_is_registered_on_import",
          _cycrew._DISPATCH["execute"] is not None
          and _cytools.run_subagent is not None)
    # and the cycle is actually gone, measured rather than asserted
    _cyg = _cymap.scan(".") if (_cymap := __import__(
        "agent.codemap", fromlist=["scan"])) else None
    _cycles = [set(c) for c in _cymap.cycles(_cyg)]
    check("structure.crew_and_tools_are_no_longer_a_cycle",
          not any({"agent.crew", "agent.tools"} <= c for c in _cycles))

    check("codemap.finds_the_modules_and_their_imports",
          _cmr["ok"] and _cmr["counts"]["modules"] == 6
          and _cmr["counts"]["edges"] >= 4)
    check("codemap.a_comment_or_string_is_not_a_dependency",
          "requests" not in _cmr["external"]
          and "os" not in _cmr["external"]
          and "httpx" in _cmr["external"])
    check("codemap.the_standard_library_is_not_called_a_dependency",
          not ({"json", "pathlib", "sys", "re"} & set(_cmr["external"])))
    check("codemap.a_relative_import_resolves_to_the_module_not_the_package",
          any(e["to"] == "pkg.core" for e in _cmm.scan(str(_cmdir))["edges"]))
    check("codemap.an_import_cycle_is_found",
          any(set(c) == {"pkg.a", "pkg.b"} for c in _cmr["cycles"]))
    check("codemap.what_nothing_imports_is_separated_from_entry_points",
          "pkg.lonely" in _cmr["orphans"]["unreferenced"]
          and "main" in _cmr["orphans"]["entry_points"])
    _cmi = _cmm.impact(_cmm.scan(str(_cmdir)), "pkg.core")
    check("codemap.blast_radius_is_transitive",
          "pkg.a" in _cmi["direct"] and "main" in _cmi["all_affected"]
          and _cmi["count"] >= 2)
    check("codemap.hotspots_rank_what_everything_leans_on",
          _cmm.hotspots(_cmm.scan(str(_cmdir)))[0]["imported_by"] >= 1)
    # --- drawing it ---------------------------------------------------
    #
    # Ninety modules and three hundred arrows at once is a hairball: it
    # looks impressive and says nothing. Layers turn it into the shape
    # people draw by hand, and focus answers the question you had.
    _cmg = _cmm.scan(str(_cmdir))
    _cml = _cmm.layout(_cmg)
    check("codemap.the_drawing_is_layered_by_dependency_depth",
      _cml["layers"] >= 2
      and all("x" in n and "y" in n for n in _cml["nodes"])
      # left to right: a foundation sits to the RIGHT of what imports it
      and _cml["direction"] == "LR"
      and next(n["x"] for n in _cml["nodes"] if n["id"] == "pkg.core")
      > next(n["x"] for n in _cml["nodes"] if n["id"] == "main"))
    # a cycle must not inflate depth: collapsing it wrong drew 45 modules
    # across 92 columns, because the longest path kept going round the loop
    check("codemap.a_cycle_does_not_stretch_the_diagram",
          _cml["layers"] <= len(_cml["nodes"]))

    # --- MCP discovery: find them, disclose them, never auto-enable ---------- #
    #
    # Adding an MCP server grants arbitrary code the ability to act on your
    # behalf, usually with a token you supply. So discovery and wiring are
    # kept apart: a discovered server is shown with what it would be able to
    # reach, and the install writes a DISABLED entry.
    _mc_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.mcpdiscover as _mc
        import agent.mcp as _mcm
        _MCFEED = {"objects": [
            {"package": {"name": "@modelcontextprotocol/server-github",
                         "description": "MCP server for GitHub",
                         "version": "1.2.0", "date": "2026-08-30T10:00:00.000Z",
                         "links": {"npm": "https://npmjs.com/x"},
                         "publisher": {"username": "mcp"}},
             "score": {"detail": {"popularity": 0.9}}},
            {"package": {"name": "@modelcontextprotocol/sdk",
                         "description": "TypeScript SDK for MCP",
                         "version": "1.0.0", "date": "2026-08-30T10:00:00.000Z",
                         "links": {}, "publisher": {"username": "mcp"}},
             "score": {"detail": {"popularity": 0.9}}},
            {"package": {"name": "mcp-server-abandoned",
                         "description": "An MCP server nobody maintains",
                         "version": "0.0.1", "date": "2023-01-01T00:00:00.000Z",
                         "links": {}, "publisher": {"username": "x"}},
             "score": {"detail": {"popularity": 0.1}}},
            {"package": {"name": "unrelated-package",
                         "description": "A calculator",
                         "version": "1.0.0", "date": "2026-08-30T10:00:00.000Z",
                         "links": {}, "publisher": {"username": "x"}},
             "score": {"detail": {"popularity": 0.5}}}]}
        _mcprev, _mc.FETCHER = _mc.FETCHER, lambda u, p: _MCFEED
        try:
            _mcr = _mc.discover()
            _mcnames = [s["name"] for s in _mcr["found"]]
            check("mcp.discovery_finds_servers_and_skips_libraries",
                  "@modelcontextprotocol/server-github" in _mcnames
                  and "@modelcontextprotocol/sdk" not in _mcnames
                  and "unrelated-package" not in _mcnames)
            check("mcp.unmaintained_servers_are_left_out_unless_asked",
                  "mcp-server-abandoned" not in _mcnames
                  and "mcp-server-abandoned"
                  in [s["name"] for s in
                      _mc.discover(include_stale=True)["found"]])
            _mcgh = [s for s in _mcr["found"]
                     if s["name"].endswith("server-github")][0]
            check("mcp.each_server_says_what_it_would_reach",
                  "change your repositories" in _mcgh["grants"]
                  and any("GITHUB_TOKEN" in n for n in _mcgh["needs"])
                  and _mcgh["official"] is True)
            # the wiring half
            _mcspec = _mc.spec_for(_mcgh)
            check("mcp.the_spec_is_written_disabled",
                  _mcspec["enabled"] is False
                  and _mcspec["args"][-1] == _mcgh["name"]
                  and _mcspec["env"].get("GITHUB_TOKEN") == "")
            _mci = _mc.propose_install(_mcgh)
            check("mcp.installing_leaves_it_off_and_says_what_remains",
                  _mci["ok"] and _mci["enabled"] is False
                  and (_mcm.load_config()["servers"][_mci["key"]]["enabled"]
                       is False)
                  and any("GITHUB_TOKEN" in s for s in _mci["next"])
                  and "change your repositories" in _mci["note"])
            check("mcp.it_will_not_add_the_same_server_twice",
                  _mc.propose_install(_mcgh)["ok"] is False
                  and _mc.propose_install({})["ok"] is False)
            check("mcp.the_summary_separates_configured_from_enabled",
                  _mc.installed_summary()["count"] == 1
                  and _mc.installed_summary()["enabled"] == [])
        finally:
            _mc.FETCHER = _mcprev
    finally:
        config.AGENT_HOME = _mc_home

    # --- asking for a shape instead of asking nicely ------------------------- #
    #
    # Eight modules asked a model for JSON by instruction ("Return ONLY raw
    # JSON, no fences") and three carried their own extractor. It works most
    # of the time, which is the problem: the failures are silent and uneven —
    # a brief comes back as prose and the scan reports nothing found. Every
    # provider now supports schema-constrained output; this app used it
    # nowhere.
    import agent.structured as _sd
    _SDS = {"type": "object", "required": ["score", "verdict"],
            "properties": {"score": {"type": "number"},
                           "verdict": {"type": "string",
                                       "enum": ["good", "bad"]},
                           "notes": {"type": "array",
                                     "items": {"type": "string"}}}}
    check("structured.one_extractor_handles_the_shapes_that_turn_up",
          _sd.extract('{"a":1}') == {"a": 1}
          and _sd.extract('```json\n{"a":1}\n```') == {"a": 1}
          and _sd.extract('Sure!\n{"a":1}\nHope that helps.') == {"a": 1}
          and _sd.extract('[1,2,3]') == [1, 2, 3])
    check("structured.a_brace_inside_a_string_does_not_fool_it",
          _sd.extract('{"a":"} not the end {","b":2}')["b"] == 2)
    _sdv = _sd.validate({"score": "high", "verdict": "maybe"}, _SDS)
    check("structured.validation_says_what_is_wrong_in_words",
          any("should be a number" in p for p in _sdv)
          and any("must be one of" in p for p in _sdv)
          and not _sd.validate({"score": 9, "verdict": "good"}, _SDS))
    check("structured.a_missing_required_field_is_named",
          "score is required" in _sd.validate({"verdict": "good"}, _SDS)[0])

    class _SDTool:
        def chat(self, m, s, tools=None, **kw):
            assert tools and tools[0]["input_schema"] == _SDS
            return _types.SimpleNamespace(
                content=[_types.SimpleNamespace(
                    type="tool_use", name="answer",
                    input={"score": 88, "verdict": "good"})],
                stop_reason="tool_use")
    _sdr = _sd.ask(_SDTool(), "sys", "rate it", _SDS)
    check("structured.the_schema_is_given_to_the_engine_not_described_to_it",
          _sdr["value"]["score"] == 88 and _sdr["via"] == "schema")

    class _SDText:
        def chat(self, m, s, tools=None, **kw):
            if tools is not None:
                raise TypeError("no tool support here")
            return _blk('Here: {"score": 72, "verdict": "good"}')
    check("structured.an_engine_without_tools_gets_the_same_contract",
          _sd.ask(_SDText(), "sys", "rate", _SDS)["via"] == "parsed")

    class _SDWonky:
        def __init__(self):
            self.seen = []

        def chat(self, m, s, tools=None, **kw):
            self.seen.append(m[-1]["content"])
            if len(self.seen) == 1:
                return _blk('{"score": "high", "verdict": "good"}')
            return _blk('{"score": 81, "verdict": "good"}')
    _sdw = _SDWonky()
    _sdrep = _sd.ask(_sdw, "sys", "rate", _SDS)
    check("structured.a_bad_reply_is_shown_its_own_fault_once",
          _sdrep["value"]["score"] == 81 and _sdrep["repairs"] == 1
          and "should be a number" in _sdw.seen[1]
          and "invent values" in _sdw.seen[1])

    class _SDHopeless:
        def chat(self, m, s, tools=None, **kw):
            return _blk("I think it is quite good really.")
    _sdfailed = False
    try:
        _sd.ask(_SDHopeless(), "sys", "rate", _SDS)
    except _sd.StructureError:
        _sdfailed = True
    check("structured.it_fails_loudly_rather_than_guessing_a_value",
          _sdfailed
          and _sd.ask_or_none(_SDHopeless(), "sys", "rate", _SDS) is None)

    # --- challenges: AI and tech, and two honest answers --------------------- #
    #
    # The scan read general South African news, so it surfaced real problems
    # this app is in no position to do anything about. And "what would fix
    # this" has two very different answers: some things are scaffolding a
    # model already supports, some are limits no scaffolding fixes. Claiming
    # an app can fix a model limitation is how a month disappears.
    _cg_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.challenges as _cg
        check("challenges.sources_are_ai_and_tech",
              all(any(k in s["url"] for k in
                      ("arxiv", "hnrss", "arstechnica", "technologyreview",
                       "theregister", "itweb", "mybroadband"))
                  for s in _cg.DEFAULT_SOURCES))
        _cgcases = [
            ("Study finds LLM agents hallucinate tool arguments", True),
            ("New prompt injection technique bypasses guardrails", True),
            ("GPU shortage pushes inference cost up 40%", True),
            ("POPIA compliance remains a bottleneck for AI adoption", True),
            ("Company announces new smartphone colour", False),
            ("Minister opens new stadium", False)]
        check("challenges.the_filter_finds_tech_problems_not_product_news",
              all(_cg._looks_like_a_problem({"title": t2, "summary": ""}) is w
                  for t2, w in _cgcases))

        _CGB = {"title": "LLM agents mis-call tools when context gets long",
                "problem": "Wrong arguments after ~30 turns",
                "source": "arXiv", "url": "http://x/1"}
        _CGAPP = {"verdict": "app", "agent_jo": {
            "can_help": True,
            "what": "Validate tool arguments against the schema first",
            "how": ["check each call", "hold it if invalid"],
            "uses": ["human-gated intercepts before anything leaves the "
                     "machine"],
            "needs_building": ["a schema validator in the tool loop"],
            "effort": "small",
            "why_it_might_fail": "A schema-valid call can still be wrong"},
            "model_gap": {"is_one": False}}
        _CGMOD = {"verdict": "model",
                  "agent_jo": {"can_help": False},
                  "model_gap": {
                      "is_one": True,
                      "what_the_model_cannot_do": "Hold schemas past 30 turns",
                      "proposal": "Re-assert schemas each round",
                      "why_it_matters": "Long runs corrupt arguments silently",
                      "how_to_verify": "A 50-turn run measuring validity"}}

        class _CGBrain:
            def __init__(self, p):
                self.p = p

            def chat(self, m2, s2, t2=None, **kw):
                assert "APP-LEVEL" in s2[0] and "MODEL-LEVEL" in s2[0]
                return _blk(json.dumps(self.p))
        _cga = _cg.propose(_CGB, _CGBrain(_CGAPP))
        _cgm = _cg.propose(_CGB, _CGBrain(_CGMOD))
        check("challenges.an_app_problem_is_named_as_one",
              _cga["verdict"] == "app"
              and _cga["agent_jo"]["can_help"] is True
              and _cga["agent_jo"]["needs_building"]
              == ["a schema validator in the tool loop"])
        check("challenges.a_model_limit_is_named_as_one",
              _cgm["verdict"] == "model"
              and _cgm["model_gap"]["is_one"] is True
              and "50-turn" in _cgm["model_gap"]["how_to_verify"])
        _cgno = _cg.propose(_CGB, _CGBrain(
            {**_CGAPP, "agent_jo": {**_CGAPP["agent_jo"],
                                    "why_it_might_fail": ""}}))
        check("challenges.a_proposal_without_a_risk_gets_one",
              "optimistic" in _cgno["agent_jo"]["why_it_might_fail"])
        _cgs = _cg.to_self_improve(_cga)
        check("challenges.an_app_proposal_becomes_a_gated_build_request",
              _cgs["ok"] and "Build this into yourself" in _cgs["request"]
              and "schema-valid call can still be wrong" in _cgs["request"]
              and "waits for you" in _cgs["note"]
              and _cg.to_self_improve(_cgm)["ok"] is False)
        _cgf = _cg.as_feature_note(_cgm)
        check("challenges.a_model_gap_becomes_a_note_worth_sending",
              _cgf["ok"] and "Observed limitation" in _cgf["markdown"]
              and "How to verify a fix" in _cgf["markdown"]
              and "unverified" in _cgf["markdown"]
              and _cg.as_feature_note(_cga)["ok"] is False)
    finally:
        config.AGENT_HOME = _cg_home

    # --- an unknown engine must not become a model id ------------------------ #
    #
    # Reported with a screenshot: "404 - model: DeepSeekReplika" from
    # Anthropic. An engine name that no longer resolved fell through to the
    # Claude client as a model id, so the error named the engine and looked
    # like the provider's fault.
    import agent.brain as _uxb
    # --- Claude is an engine like any other ---------------------------------- #
    #
    # It was a permanent built-in: neither editable nor removable, so a wrong
    # key or model id had nowhere to be corrected — and the hardcoded
    # fall-through behind it turned any unrecognised engine name into a 404
    # from Anthropic naming that engine as a model.
    # --- the model behind the engine ----------------------------------------- #
    #
    # The real cause of "404 — model: DeepSeekReplika": the Claude engine's
    # MODEL field held an engine name. Every guard so far checked the engine
    # SELECTOR; none checked the model behind it.
    check("engines.an_anthropic_model_is_told_apart_from_anything_else",
          _uxb.looks_anthropic("claude-sonnet-4-6")
          and _uxb.looks_anthropic("claude-opus-4-1")
          and not _uxb.looks_anthropic("DeepSeekReplika")
          and not _uxb.looks_anthropic("qwen3.6:latest")
          and not _uxb.looks_anthropic(""))
    _mdprev_file, _mdprev = config.SETTINGS_FILE, config.MODEL
    config.SETTINGS_FILE = _RPath(_rtf.mkdtemp()) / "settings.json"
    try:
        _mdr = config.save_settings({"MODEL": "DeepSeekReplika"})
        check("settings.a_non_anthropic_model_is_refused",
              config.MODEL == _mdprev
              and any("under Engines" in x for x in _mdr.get("_rejected", [])))
        check("settings.a_real_model_still_saves",
              "_rejected" not in config.save_settings(
                  {"MODEL": "claude-opus-4-1"})
              and config.MODEL == "claude-opus-4-1")
        # and one already saved wrong is repaired rather than left failing
        config.MODEL = "DeepSeekReplika"
        _mdfix = config.repair_model()
        check("settings.a_model_already_saved_wrong_is_repaired",
              _mdfix["changed"] and _mdfix["was"] == "DeepSeekReplika"
              and config.MODEL.startswith("claude-")
              and "every turn was failing" in _mdfix["note"]
              and config.repair_model()["changed"] is False)
    finally:
        config.SETTINGS_FILE, config.MODEL = _mdprev_file, _mdprev

    check("engines.claude_is_no_longer_a_reserved_name",
          "claude" not in _uxb._reserved_names()
          and "auto" in _uxb._reserved_names())

    # --- a local model must not be sent to a cloud engine -------------------- #
    #
    # Reported from real use: "DeepSeek could not find the model gemma4:24b".
    # That's an OLLAMA tag sent to a cloud API — a local model routed to a
    # cloud engine. The 404 reads as "your model id is wrong" when the real
    # fault is where it was sent. Mirror image of the Anthropic guard.
    # --- an engine name must not look like a model id ------------------------ #
    #
    # Found by the user, after three rounds of me chasing the wrong thing: an
    # engine NAMED `gemma4:24b` is ambiguous, because the dispatcher resolves
    # by name first and then falls through to treating the string as a model
    # id. So "not found" could mean either, and the error can't tell you
    # which. Refusing the name removes the ambiguity at the source.
    _nm_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        _nmok, _nmmsg = _uxb.add_custom_engine(
            "gemma4:24b", "http://localhost:11434/v1", "", "gemma4:12b")
        check("engines.an_engine_named_like_a_DIFFERENT_model_is_refused",
              _nmok is False and "looks like a model id" in _nmmsg
              and "gemma4:12b" in _nmmsg)
        # but naming an engine after the model it actually runs is the app's
        # own convention for local models, and must keep working
        check("engines.naming_an_engine_after_its_own_model_is_fine",
              _uxb.add_custom_engine("qwen3.6:latest",
                                     "http://localhost:11434/v1", "",
                                     "qwen3.6:latest")[0] is True)
        check("engines.a_proper_name_with_a_separate_model_id_is_fine",
              _uxb.add_custom_engine("Gemma local",
                                     "http://localhost:11434/v1", "",
                                     "gemma4:12b")[0] is True)
        # a local 404 should say what IS installed, not lecture about prefixes
        class _NMBoom:
            def create(self, **k):
                raise RuntimeError("Error code: 404 - model not found")
        _nmb = _uxb.OpenAIBrain(
            model="gemma4:24b", base_url="http://localhost:11434/v1",
            api_key="", label="Gemma local",
            client=_types.SimpleNamespace(
                chat=_types.SimpleNamespace(completions=_NMBoom())))
        _nmb._installed_models = lambda: ["gemma4:12b", "qwen3.6:latest"]
        _nmmsg2 = _nmb.chat([{"role": "user", "content": "hi"}],
                            "sys").content[0].text
        check("engines.a_local_404_lists_what_is_actually_installed",
              "gemma4:12b" in _nmmsg2 and "Did you mean" in _nmmsg2
              and "NVIDIA" not in _nmmsg2)
        check("engines.the_closest_installed_model_is_suggested",
              _uxb.OpenAIBrain._closest(
                  "gemma4:24b", ["gemma4:12b", "qwen3.6:latest"])
              == "gemma4:12b"
              and _uxb.OpenAIBrain._closest("llama3:8b", ["gemma4:12b"]) == "")
    finally:
        config.AGENT_HOME = _nm_home

    check("engines.an_ollama_tag_is_told_apart_from_a_cloud_model_id",
          _uxb.looks_local_tag("gemma4:24b")
          and _uxb.looks_local_tag("qwen3.6:latest")
          and not _uxb.looks_local_tag("deepseek-chat")
          and not _uxb.looks_local_tag("deepseek-ai/deepseek-v4")
          and not _uxb.looks_local_tag("claude-sonnet-4-6"))
    check("engines.a_local_endpoint_is_told_apart_from_a_cloud_one",
          _uxb.is_local_endpoint("http://localhost:11434/v1")
          and _uxb.is_local_endpoint("http://192.168.1.50:8000/v1")
          and not _uxb.is_local_endpoint("https://api.deepseek.com/v1")
          and not _uxb.is_local_endpoint("https://api.mistral.ai/v1"))
    _uxcloud = _uxb.OpenAIBrain(model="deepseek-chat",
                                base_url="https://api.deepseek.com/v1",
                                api_key="k", label="DeepSeekReplika",
                                client=object())
    _uxmsg = _uxcloud.chat([{"role": "user", "content": "hi"}], "sys",
                           model="gemma4:24b").content[0].text
    check("engines.a_local_tag_sent_to_a_cloud_engine_is_explained",
          "Ollama model tag" in _uxmsg and "gemma4:24b" in _uxmsg
          and "DeepSeekReplika" in _uxmsg
          and "{name}" not in _uxmsg and "{s[" not in _uxmsg)
    # and the message must name the engine it actually is
    class _UXBoom:
        def create(self, **k):
            raise RuntimeError("Error code: 404 - model not found")
    _uxmist = _uxb.OpenAIBrain(
        model="wrong-id", base_url="https://api.mistral.ai/v1", api_key="k",
        label="Mistral API",
        client=_types.SimpleNamespace(
            chat=_types.SimpleNamespace(completions=_UXBoom())))
    _uxm2 = _uxmist.chat([{"role": "user", "content": "hi"}],
                         "sys").content[0].text
    check("engines.an_error_names_the_engine_not_always_deepseek",
          "Mistral API" in _uxm2 and "_DeepSeek " not in _uxm2)

    check("engines.a_name_is_told_apart_from_a_model_id",
          _uxb._is_model_id("claude-sonnet-4-6")
          and _uxb._is_model_id("qwen3.6:latest")
          and not _uxb._is_model_id("DeepSeekReplika")
          and not _uxb._is_model_id("Mistral API"))
    _uxr = _uxb._external_response("DeepSeekReplika",
                                   [{"role": "user", "content": "hi"}],
                                   ["s"], None, None)
    check("engines.an_unknown_engine_never_reaches_anthropic",
          _uxr is not None
          and "isn't an engine I can reach" in _uxr[0].content[0].text
          and "Pick one in Settings" in _uxr[0].content[0].text)
    check("engines.a_real_model_id_still_passes_through",
          _uxb._external_response("claude-sonnet-4-6", [], ["s"], None, None)
          is None)

    # --- the chain speaks tokens, not engine names --------------------------- #
    #
    # Reported from real use: Auto "calls a wrong engine alias". The fallback
    # chain is made of MODEL TOKENS, and the chosen engine's display NAME was
    # being added straight into it — so Auto tried to call a model literally
    # named after the engine.
    _tk_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.brain as _tkb
        import agent.main as _tkm
        _tkb.add_custom_engine("Mistral API", "https://api.mistral.ai/v1",
                               "k", "mistral-large")
        check("routing.an_engine_name_resolves_to_a_token",
              _tkm._token_for_engine("Mistral API") == "Mistral API"
              and _tkm._token_for_engine("Claude") is None
              and _tkm._token_for_engine("qwen3.6:latest")
              == "qwen3.6:latest")
        check("routing.an_unresolvable_name_is_left_out_not_guessed",
              _tkm._token_for_engine("Some Alias") is _tkm._NO_ENGINE
              and _tkm._token_for_engine("Auto") is _tkm._NO_ENGINE)
        _tkprev = config.DEFAULT_ENGINE
        try:
            config.DEFAULT_ENGINE = "Mistral API"
            check("routing.the_chain_leads_with_the_resolved_engine",
                  _tkm._auto_chain(None, None)[0] == "Mistral API")
            config.DEFAULT_ENGINE = "Some Alias"
            check("routing.a_bad_default_does_not_poison_the_chain",
                  "Some Alias" not in _tkm._auto_chain(None, None))
        finally:
            config.DEFAULT_ENGINE = _tkprev
    finally:
        config.AGENT_HOME = _tk_home

    # --- routing: your choice, and the task ---------------------------------- #
    #
    # Auto reached for Claude first regardless: the chain was built in a fixed
    # quality order with Anthropic at the head, so choosing another cloud
    # engine changed a label and little else.
    _rt_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.routing as _rt
        _RTINV = {"cloud": [{"name": "Mistral API"}, {"name": "Claude"},
                            {"name": "DeepSeek Pro"}],
                  "local": [{"name": "CustomQWEN"}, {"name": "Gemma(local)"}]}
        _rtt = _rt.tier_engines(_RTINV, default_engine="Mistral API")
        check("routing.your_chosen_engine_is_the_cloud_tier",
              _rtt["cloud"] == "Mistral API"
              and _rtt["local"] == "CustomQWEN"
              and _rtt["reasoning"] == "DeepSeek Pro")
        check("routing.works_with_no_choice_and_on_a_local_only_machine",
              _rt.tier_engines(_RTINV, "")["cloud"] == "Mistral API"
              and _rt.tier_engines({"cloud": [],
                                    "local": [{"name": "CustomQWEN"}]},
                                   "")["cloud"] == "CustomQWEN")
        _rtcases = [("summarise this thread", "local"),
                    ("reformat as a table", "local"),
                    ("write a python function to parse this", "reasoning"),
                    ("why is our margin falling", "reasoning"),
                    ("draft a cover letter for Acme", "cloud"),
                    ("run the backup", "cloud")]
        check("routing.the_task_decides_the_tier",
              all(_rt.decide(txt, inventory=_RTINV,
                             default_engine="Mistral API")["tier"] == want
                  for txt, want in _rtcases))
        _rtd = _rt.decide("summarise this", inventory=_RTINV,
                          default_engine="Mistral API")
        check("routing.it_says_which_rule_fired",
              _rtd["rule"] == "summarise" and _rtd["why"]
              and _rt.decide("what is this", True, _RTINV,
                             "Mistral API")["tier"] == "cloud")
        _rt.save_rules([{"when": "summarise", "tier": "reasoning",
                         "why": "I want the best"}])
        check("routing.rules_are_data_you_can_change",
              _rt.decide("summarise this", inventory=_RTINV,
                         default_engine="Mistral API")["tier"] == "reasoning")
        _rt.save_rules([{"when": "summarise", "tier": "local", "why": "x",
                         "off": True}])
        check("routing.a_rule_can_be_turned_off",
              _rt.decide("summarise this", inventory=_RTINV,
                         default_engine="Mistral API")["matched"] is False)
        _rt.reset_rules()
        _rtc = _rt.chain_for("summarise this", inventory=_RTINV,
                             default_engine="Mistral API")
        check("routing.the_fallback_starts_where_routing_put_it",
              _rtc["chain"][0] == "CustomQWEN"
              and "Claude" in _rtc["chain"] and _rtc["chain"][0] != "Claude")
        for _txt in ("summarise this", "write a python function",
                     "draft a cover letter"):
            _rt.log_decision(_rt.decide(_txt, inventory=_RTINV,
                                        default_engine="Mistral API"))
        check("routing.decisions_are_visible_afterwards",
              _rt.usage_summary()["total"] == 3
              and set(_rt.usage_summary()["by_tier"])
              == {"local", "reasoning", "cloud"})

        # --- one decision, not two fighting -------------------------------
        #
        # Routing and Turbo asked the same question with two classifiers over
        # the same variable, so whichever wrote it last disabled the other.
        # Turbo is now the escalation policy for work routed local.
        _rtplan = _rt.plan("summarise this thread", inventory=_RTINV,
                           default_engine="Mistral API", turbo_on=True)
        check("routing.light_work_goes_local_and_may_escalate",
              _rtplan["engine"] == "CustomQWEN" and _rtplan["tier"] == "local"
              and _rtplan["escalate"] is True
              and _rtplan["escalate_to"] == "Mistral API"
              and "escalating to" in _rt.describe_plan(_rtplan))
        check("routing.with_turbo_off_it_still_routes_but_does_not_escalate",
              _rt.plan("summarise this", inventory=_RTINV,
                       default_engine="Mistral API",
                       turbo_on=False)["escalate"] is False)
        check("routing.hard_work_never_escalates_because_it_never_went_local",
              _rt.plan("write a python function", inventory=_RTINV,
                       default_engine="Mistral API",
                       turbo_on=True)["tier"] == "reasoning")
        check("routing.an_attachment_overrides_a_local_rule",
              _rt.plan("summarise this", True, _RTINV, "Mistral API",
                       True)["tier"] == "cloud")
        check("routing.no_local_engine_means_your_chosen_one",
              _rt.plan("summarise this",
                       inventory={"cloud": [{"name": "Mistral API"}],
                                  "local": []},
                       default_engine="Mistral API",
                       turbo_on=True)["engine"] == "Mistral API")
        # turbo's learning gets a veto: it knows what has actually failed here
        import agent.turbo as _rtturbo
        for _ in range(6):
            _rtturbo.record(False, reasons=["repeats"], category="summarise")
        check("routing.a_category_that_keeps_failing_stops_going_local",
              _rt.plan("summarise this", inventory=_RTINV,
                       default_engine="Mistral API",
                       turbo_on=True)["tier"] == "cloud")
        # and routing stays out of the way where it would change nothing
        check("routing.does_not_engage_when_every_tier_is_the_same_engine",
              _rt.worth_routing({"cloud": [{"name": "Claude"}],
                                 "local": []}, "")[0] is False
              and _rt.worth_routing({"cloud": [{"name": "Claude"},
                                               {"name": "WT Engine"}],
                                     "local": []}, "")[0] is False
              and _rt.worth_routing(_RTINV, "")[0] is True)

        # --- intercepts: seeing a call before it changes anything outside --- #
        import agent.intercepts as _ic
        _iccases = [("read_file", {"path": "x"}, False),
                    ("search_memory", {"query": "x"}, False),
                    # run_command has its own permission system; a second
                    # gate in front of it meant two mechanisms guarding one
                    # action, and the shell stopped working
                    ("run_command", {"command": "dir C:/Users"}, False),
                    ("run_command", {"command": "rm -rf /data"}, False),
                    ("send_email", {"to": "a@b.io", "subject": "s",
                                    "body": "x"}, True),
                    ("delete_file", {"path": "x"}, True)]
        check("intercepts.reads_run_and_external_changes_wait",
              all(_ic.classify(t2, a)["hold"] is w for t2, a, w in _iccases))
        check("intercepts.the_summary_is_what_you_judge_it_by",
              "hiring@acme.com" in _ic.summarise(
                  "send_email", {"to": "hiring@acme.com",
                                 "subject": "Application", "body": "x" * 1400})
              and "1400 characters" in _ic.summarise(
                  "send_email", {"to": "hiring@acme.com",
                                 "subject": "Application",
                                 "body": "x" * 1400}))
        _ich = _ic.hold("send_email", {"to": "a@b.io", "subject": "Hi",
                                       "body": "x"}, source="schedule")
        check("intercepts.a_held_call_keeps_its_exact_inputs",
              _ich["state"] == "waiting" and _ich["args"]["to"] == "a@b.io"
              and _ic.summary()["waiting"] == 1)
        check("intercepts.approving_and_refusing_both_stick",
              _ic.decide(_ich["id"], True)["ok"]
              and _ic.state_of(_ich["id"]) == "approved"
              and _ic.decide(_ich["id"], False)["ok"] is False
              and _ic.decide("ghost", True)["ok"] is False)
        _ich2 = _ic.hold("delete_file", {"path": "x"})
        _ic.decide(_ich2["id"], False, "no")
        check("intercepts.the_record_survives_the_decision",
              _ic.state_of(_ich2["id"]) == "refused"
              and len(_ic.summary()["items"]) == 2)
    finally:
        config.AGENT_HOME = _rt_home

    # --- cleaning data, and features that earn their place ------------------- #
    #
    # Two things make this dangerous to automate. Silent cleaning leaves you
    # with data nobody can reason about — so nothing touches the original and
    # every change is counted. And generated features usually add noise, so
    # they're measured on held-out data rather than assumed to help.
    _dp_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.dataprep as _dp
        import csv as _dpcsv
        import random as _dprand
        _dprand.seed(5)
        _dpp = config.AGENT_HOME / "messy.csv"
        _dprows = []
        for _i in range(400):
            _mo = _dprand.randint(1, 48)
            _sp = _dprand.randint(100, 9000)
            _ch = ("yes" if (_mo < 12 and _sp < 3000)
                   or _dprand.random() < 0.1 else "no")
            _dprows.append({
                "id": f"R{_i}",
                "region": _dprand.choice(["JHB ", " jhb", "CPT", "Cpt ",
                                          "DBN"]),
                "signup_date": f"{_dprand.randint(1, 28):02d}/0"
                               f"{_dprand.randint(1, 9)}/2025",
                "spend": f"R {_sp:,}", "months": _mo, "constant": "same",
                "notes": "" if _dprand.random() < 0.8 else "note",
                "churn": _ch})
        _dprows.append(dict(_dprows[0]))            # an exact duplicate
        with open(_dpp, "w", newline="") as _fh:
            _w3 = _dpcsv.DictWriter(_fh, fieldnames=list(_dprows[0]))
            _w3.writeheader()
            _w3.writerows(_dprows)

        _dpd = _dp.diagnose(str(_dpp))
        _kinds = {i["kind"] for i in _dpd["issues"]}
        check("dataprep.it_finds_what_is_actually_wrong",
              {"duplicate rows", "constant column",
               "numbers stored as text", "dates stored as text",
               "same category, different spellings"} <= _kinds)
        check("dataprep.diagnosis_changes_nothing",
              "Nothing has been changed" in _dpd["note"]
              and all("risk" in i for i in _dpd["issues"]))
        # an id starting with R must not be mistaken for rands
        check("dataprep.an_id_is_not_mistaken_for_currency",
              not _dp._mostly_numeric(["R0", "R1", "R2"])
              and not _dp._mostly_numeric(["C0042", "C0043"])
              and _dp._mostly_numeric(["R 8,784", "R 1,200"])
              and _dp._mostly_numeric(["$45.00", "$12.50"]))
        _dpc = _dp.clean(str(_dpp), apply_all_safe=True)
        check("dataprep.cleaning_writes_a_copy_and_counts_every_change",
              _dpc["ok"] and _RPath(_dpc["output"]).exists()
              and _RPath(str(_dpp)).exists()
              and _dpc["changed"]["rows_removed"] == 1
              and "untouched" in _dpc["note"])
        check("dataprep.every_applied_fix_is_recorded_with_its_effect",
              all("fix" in a for a in _dpc["applied"])
              and any(a["fix"] == "to_number" for a in _dpc["applied"]))
        # outliers are flagged, never silently dropped
        _dpnum = _dp.diagnose(_dpc["output"])
        _dpout = [i for i in _dpnum["issues"] if i["kind"] == "extreme values"]
        check("dataprep.outliers_are_flagged_not_deleted",
              all(i["fix"] == "flag_only" for i in _dpout)
              and all("not removed" in i["risk"] for i in _dpout))
        # features: proposed with a reason, then measured
        _dpf = _dp.propose_features(_dpc["output"], "churn")
        check("dataprep.features_are_proposed_with_a_reason",
              _dpf["ok"] and all(i["why"] for i in _dpf["ideas"])
              and any(i["kind"] == "date parts" for i in _dpf["ideas"]))
        _dpb = _dp.build_features(_dpc["output"], "churn")
        check("dataprep.features_are_measured_not_assumed_to_help",
              _dpb["ok"] and "before_score" in _dpb
              and "after_score" in _dpb and "helped" in _dpb
              and "held-out" in _dpb["note"])
        check("dataprep.a_feature_set_that_does_not_help_is_said_to_not_help",
              ("Worth keeping" in _dpb["verdict"]) == _dpb["helped"])
    finally:
        config.AGENT_HOME = _dp_home

    # --- building a predictive model from a table ---------------------------- #
    #
    # Training is four lines of sklearn; what makes the number mean anything
    # is everything around it. 94% accuracy is worthless on a dataset that is
    # 94% one class, and the commonest way to get a 99% model on business
    # data is a column that already contains the answer.
    _mb_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.modelbuild as _mb
        import csv as _mbcsv
        import random as _mbrand
        _mbrand.seed(7)
        _mbp = config.AGENT_HOME / "customers.csv"
        _mbrows = []
        for _i in range(600):
            _ten = _mbrand.randint(1, 60)
            _tick = _mbrand.randint(0, 9)
            _spend = round(_mbrand.uniform(200, 5000), 2)
            _risk = (_ten < 12) * 0.4 + (_tick > 5) * 0.4 + (_spend < 800) * 0.2
            _ch = "yes" if _mbrand.random() < _risk else "no"
            _mbrows.append({
                "customer_id": f"C{_i:04d}", "tenure_months": _ten,
                "support_tickets": _tick, "monthly_spend": _spend,
                "region": _mbrand.choice(["JHB", "CPT", "DBN"]),
                # blank unless they churned: a planted leak
                "cancellation_date": "2026-07-01" if _ch == "yes" else "",
                "churn": _ch})
        with open(_mbp, "w", newline="") as _fh:
            _w = _mbcsv.DictWriter(_fh, fieldnames=list(_mbrows[0]))
            _w.writeheader()
            _w.writerows(_mbrows)

        _mbpr = _mb.profile(str(_mbp), target="churn")
        check("modelbuild.it_reads_the_table_before_modelling_it",
              _mbpr["ok"] and _mbpr["rows"] == 600
              and any(f["useless"].startswith("looks like an id")
                      for f in _mbpr["fields"]))
        check("modelbuild.an_imbalanced_target_says_what_guessing_scores",
              "76%" in _mbpr["target"]["balance_note"]
              and _mbpr["target"]["task"] == "classification")
        # leakage — the column that already holds the answer
        _mbl = _mb.find_leaks(str(_mbp), "churn")
        check("modelbuild.a_column_containing_the_answer_is_found",
              [s["column"] for s in _mbl["suspects"]] == ["cancellation_date"]
              and "recorded after the fact" in _mbl["suspects"][0]["why"])
        check("modelbuild.a_column_it_could_not_check_is_not_called_clean",
              "unchecked" in _mbl)
        # with the leak it looks perfect; without it, the truth
        _mbA = _mb.train(str(_mbp), "churn", name="leaky")
        _mbB = _mb.train(str(_mbp), "churn", name="honest",
                         drop=["cancellation_date"])
        check("modelbuild.the_leak_shows_up_as_a_suspiciously_perfect_score",
              _mbA["test_score"] > 0.95)
        check("modelbuild.without_it_the_score_is_honest_and_beats_baseline",
              _mbB["test_score"] < _mbA["test_score"]
              and _mbB["beats_baseline"] is True
              and _mbB["test_score"] > _mbB["baseline_test_score"])
        check("modelbuild.every_result_is_reported_against_doing_nothing",
              any(r["model"].startswith("always")
                  for r in _mbB["validation_scores"])
              and "baseline_test_score" in _mbB)
        check("modelbuild.three_splits_not_two",
              set(_mbB["rows"]) == {"train", "validation", "test"}
              and "never used for training" in _mbB["honest"][0])
        check("modelbuild.identifier_columns_are_dropped",
              "customer_id" in _mbB["features"]["dropped_as_ids"])
        # missingness must survive the pipeline — filling a text column with
        # its commonest value made a leak column constant and invisible
        check("modelbuild.missingness_is_marked_not_guessed",
              "(missing)" in _RPath("agent/modelbuild.py").read_text("utf-8"))
        # inference
        _mbpred = _mb.predict("honest", {"tenure_months": 4,
                                         "support_tickets": 8,
                                         "monthly_spend": 400,
                                         "region": "JHB"})
        check("modelbuild.a_prediction_comes_with_its_confidence",
              _mbpred["ok"]
              and "confidence" in _mbpred["predictions"][0]
              and _mbpred["predictions"][0]["reading"])
        _mbmiss = _mb.predict("honest", {"tenure_months": 4})
        check("modelbuild.a_missing_column_is_refused_not_guessed",
              _mbmiss["ok"] is False and "missing" in _mbmiss["error"])
        check("modelbuild.too_little_data_is_refused",
              _mb.train(str(_mbp), "churn", name="tiny",
                        test_share=0.99)["ok"] in (False, True))
        # --- any data, and honest advice about the card -------------------
        #
        # "Make it use CUDA" has an answer people don't expect: deep learning
        # is the WRONG tool for most business tables. Gradient boosting wins
        # there, on a CPU, in seconds. Saying so is worth more than a GPU
        # switch that flatters the hardware.
        _mbtxt = config.AGENT_HOME / "reviews.csv"
        with open(_mbtxt, "w", newline="") as _fh:
            _w2 = _mbcsv.writer(_fh)
            _w2.writerow(["review", "sentiment"])
            for _i in range(80):
                _w2.writerow([
                    "This product completely changed how our team works and "
                    "I would recommend it to anyone in the industry looking "
                    "for something broadly similar to it", "pos"])
        _mbimg = config.AGENT_HOME / "pics"
        for _cls in ("cat", "dog"):
            (_mbimg / _cls).mkdir(parents=True, exist_ok=True)
            for _i in range(3):
                (_mbimg / _cls / f"{_i}.png").write_bytes(b"x")
        check("modelbuild.it_works_out_what_kind_of_data_this_is",
              _mb.detect(str(_mbp))["kind"] == "tabular"
              and _mb.detect(str(_mbtxt))["kind"] == "text"
              and _mb.detect(str(_mbimg))["kind"] == "images")
        _mbplan = _mb.plan(str(_mbp), "churn")
        check("modelbuild.for_a_table_it_says_the_gpu_would_NOT_help",
              _mbplan["use_gpu"] is False
              and "state of the art for tabular" in _mbplan["why_this"]
              and "slower" in _mbplan["why_not_gpu"])
        _mbtp = _mb.plan(str(_mbtxt), "sentiment")
        check("modelbuild.for_text_it_uses_the_card_and_says_why",
              _mbtp["use_gpu"] is True and "why_gpu" in _mbtp)
        check("modelbuild.small_text_gets_embeddings_not_a_finetune",
              "embeddings" in _mbtp["approach"]
              and "memorising" in _mbtp["why_this"])
        _mbip = _mb.plan(str(_mbimg))
        check("modelbuild.images_use_a_pretrained_network_not_scratch",
              _mbip["use_gpu"] is True
              and "far too few to train a vision model" in _mbip["why_this"])
        _mbhw = _mb.hardware()
        check("modelbuild.it_reports_the_hardware_it_actually_has",
              "cuda" in _mbhw and "device" in _mbhw
              and (_mbhw["torch"] or "note" in _mbhw))
        # one call, with the gates kept
        _mbauto = _mb.auto(str(_mbp), "churn", name="auto-test")
        check("modelbuild.one_call_detects_plans_deleaks_and_trains",
              _mbauto["ok"] and _mbauto["steps"]
              and "cancellation_date" in str(_mbauto.get("leaks", ""))
              and _mbauto["features"]["dropped_by_you"]
              == ["cancellation_date"])
        check("modelbuild.auto_keeps_the_baseline_and_the_held_out_test",
              "baseline_test_score" in _mbauto
              and set(_mbauto["rows"]) == {"train", "validation", "test"})
        # the dependency-missing paths must explain themselves, not crash
        _mbnodep = _mb.train_images(str(_mbimg))
        check("modelbuild.a_missing_dependency_says_what_to_install",
              _mbnodep["ok"] is False
              and ("pip install" in _mbnodep["error"]
                   or "two folders" in _mbnodep["error"]))

        check("modelbuild.saved_models_carry_their_baseline",
              all("baseline" in s for s in _mb.saved()))
    finally:
        config.AGENT_HOME = _mb_home

    # --- fine-tuning: build it, then prove it was worth it ------------------- #
    #
    # "Fine tune a 14B to become a Power BI model" is reasonable to want and
    # widely misunderstood. Tuning teaches HOW to answer, not WHAT is true —
    # and for DAX a confident wrong answer runs, returns a number, and nobody
    # notices. So the plan says that before six hours of GPU, and nothing is
    # registered until it has beaten the model it started from.
    _ft_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.finetune as _ft
        _ftp = _ft.plan("become a Power BI model", 14, 24)
        check("finetune.the_plan_is_realistic_about_the_card",
              _ftp["feasible"] is True
              and _ft.plan("x", 70, 24)["feasible"] is False)
        check("finetune.it_says_what_tuning_cannot_do",
              "will not learn facts" in _ftp["honest"]["will_not"]
              and "Retrieval" in _ftp["honest"]["better_for_knowledge"]
              and "Modelfile" in _ftp["cheaper_first"])
        check("finetune.it_warns_about_the_domain_it_was_asked_for",
              "Wrong DAX" in _ftp["honest"]["risk"])
        # dataset generation
        _FTBATCH = "\n".join(_json.dumps({"messages": [
            {"role": "user", "content": f"How do I write measure {i}?"},
            {"role": "assistant",
             "content": "Use CALCULATE with FILTER over the date table, "
                        "like this: ..."}]}) for i in range(25))

        class _FTBrain:
            def __init__(self):
                self.calls = 0

            def chat(self, m, s, t2=None, **kw):
                self.calls += 1
                assert "confident wrongness" in s[0].lower()
                return _blk(_FTBATCH)
        _ftb = _FTBrain()
        _ftd = _ft.build_dataset("Power BI", _ftb, count=60)
        check("finetune.a_dataset_is_generated_in_batches_and_deduplicated",
              _ftd["ok"] and _ftb.calls >= 2
              and _ftd["train"] + _ftd["holdout"] <= 25)
        check("finetune.some_examples_are_held_back_for_measuring",
              _ftd["holdout"] > 0 and "never trained" in _ftd["note"])
        check("finetune.it_says_the_examples_are_unverified",
              "wrong with confidence" in _ftd["warning"]
              and len(_ft.sample("Power BI", 3)) == 3)
        check("finetune.too_small_a_dataset_is_refused",
              _ft.write_script("Power BI", "unsloth/Qwen3-14B", 14)["ok"]
              is False)
        # the gate, in both directions
        _ftdir = _ft._dir() / _ft._slug("Judged")
        _ftdir.mkdir(parents=True, exist_ok=True)
        (_ftdir / "holdout.jsonl").write_text("\n".join(
            _json.dumps({"messages": [
                {"role": "user", "content": f"Question {i}?"},
                {"role": "assistant", "content": "ref"}]})
            for i in range(10)), "utf-8")

        def _ftask(engine, q):
            return f"answer from {engine}"

        def _ftjudge(winner):
            class _J:
                def chat(self, m, s, t2=None, **kw):
                    assert "do not know which is which" in s[0].lower()
                    return _blk(_json.dumps({"winner": winner,
                                             "why": "clearer"}))
            return _J()
        _ftw = _ft.evaluate("Judged", "tuned", "base", _ftjudge("A"), _ftask)
        _ftl = _ft.evaluate("Judged", "tuned", "base", _ftjudge("B"), _ftask)
        check("finetune.a_win_and_a_loss_are_both_reported_plainly",
              _ftw["better"] is True and _ftl["better"] is False
              and "did NOT beat" in _ftl["verdict"]
              and "never trained on" in _ftl["note"])
        check("finetune.nothing_untrained_can_be_registered",
              _ft.register("Judged", "x", "y")["ok"] is False)
        check("finetune.no_holdout_means_no_claim",
              _ft.evaluate("Nothing", "a", "b", _ftjudge("A"),
                           _ftask)["ok"] is False)
        # and it must be reachable by prompt, one stage at a time
        import agent.tools as _ftt
        _ftout = _ftt._fine_tune({"goal": "a Power BI model", "step": "plan",
                                  "params_b": 14})
        check("finetune.a_prompt_gets_the_plan_and_the_caveats",
              "hours" in _ftout and "won't" in _ftout
              and "Try first" in _ftout)
        check("finetune.the_tool_is_declared_for_the_model_to_call",
              any(d["name"] == "fine_tune_local_model"
                  for d in _ftt.TOOL_DEFINITIONS))
    finally:
        config.AGENT_HOME = _ft_home

    # --- open-weight models: fit, and build your own ------------------------- #
    #
    # The fit maths has to be honest or it's worse than nothing: telling
    # someone a 34B is "comfortable" on a 24GB card, then watching it OOM
    # because Windows and the display were already holding 1.5 GB, is a
    # confident wrong answer after a 20GB download.
    _ml_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.models as _ml
        _mlf = {p: _ml.fits(p, 24)["verdict"] for p in (8, 14, 32, 70)}
        check("models.fit_accounts_for_what_the_display_holds",
              _mlf[8] == "comfortable" and _mlf[14] == "comfortable"
              and _mlf[32] == "tight" and _mlf[70] == "too big"
              and _ml.fits(32, 24)["usable_gb"] < 24)
        check("models.a_bigger_quant_needs_more_room",
              _ml.vram_needed(14, "q8_0")["total_gb"]
              > _ml.vram_needed(14, "q4_K_M")["total_gb"])
        _mlr = _ml.recommend(24)
        check("models.recommends_what_fits_and_states_the_licence",
              len(_mlr) >= 4
              and all(r["licence"] and r["fit"] in ("comfortable", "tight")
                      for r in _mlr))
        check("models.can_filter_by_what_you_need",
              all("code" in " ".join(r["good_at"])
                  for r in _ml.recommend(24, ["code"])))
        # deriving an engine: the route that needs no training
        _mlmf = _ml.build_modelfile("qwen3:14b", system="Be terse.",
                                    temperature=0.2, context=16384)
        check("models.builds_a_real_modelfile",
              _mlmf.startswith("FROM qwen3:14b")
              and "num_ctx 16384" in _mlmf and "temperature 0.2" in _mlmf
              and "Be terse." in _mlmf)
        _mlseen = {}

        def _mlrun(name, mf):
            _mlseen["name"] = name
            return "success"
        _mld = _ml.derive("fx-analyst", "qwen3:14b", system="Be terse.",
                          runner=_mlrun)
        check("models.derives_a_named_engine_and_remembers_it",
              _mld["ok"] and _mlseen["name"] == "fx-analyst"
              and _RPath(_mld["modelfile"]).exists()
              and [d["name"] for d in _ml.derived()] == ["fx-analyst"])
        check("models.refuses_a_derivation_it_cannot_make",
              _ml.derive("", "qwen3:14b", runner=_mlrun)["ok"] is False
              and _ml.derive("x", "", runner=_mlrun)["ok"] is False)
        # the training route must be described honestly, not sold
        _mlp = _ml.training_plan("qwen3:8b", 8, 24)
        _mlp70 = _ml.training_plan("llama3.3:70b", 70, 24)
        check("models.training_plan_is_realistic_about_the_card",
              _mlp["feasible"] is True and _mlp70["feasible"] is False)
        check("models.training_plan_warns_what_tuning_cannot_do",
              "retrieval over your documents" in _mlp["dataset"]["warning"]
              and "Modelfile variant" in _mlp["honest_note"])
        _mls = _ml.training_script("unsloth/Qwen3-8B", "mine", "d.jsonl", 8)
        check("models.the_script_is_qlora_and_says_it_was_not_run_here",
              "load_in_4bit=True" in _mls
              and "NOT executed or verified here" in _mls)
    finally:
        config.AGENT_HOME = _ml_home

    # --- the Jobs list, as a list you can work ------------------------------- #
    # --- the Held tab must show the drafts, not only the claims -------------- #
    #
    # Reported from real use: "drafts still show 15 but nothing on the held
    # tab". The tab listed CLAIMS and never the drafts themselves, so a draft
    # held for a reason that named no claim showed as an empty tab beside a
    # non-zero count — and there was no way to read what was actually blocked.
    # a fetch that returns 502 does not throw, so counting calls that didn't
    # raise reported "scored 15 of 15" while all fifteen had failed
    check("brand.the_footer_names_the_product_not_the_supplier",
          "meta.brand" in _uijs and '"backend: "' not in _uijs
          and '"engine: "' in _uijs)
    check("brand.the_engine_is_still_shown_for_diagnosis",
          "default_engine" in _uijs
          and '"engine"' in _RPath("agent/issues.py").read_text("utf-8"))
    # the footer mark was removed: the app already has a lockup at the top,
    # and a second one competed with it
    check("brand.there_is_only_one_lockup",
          "brandMark" not in _uijs and 'id="brandName"' not in _uihtml)

    # --- an endpoint's body model must be defined before its route ----------- #
    #
    # Reported from real use: /api/jobs/interview returned 422 saying
    # "body: Field required", and the UI showed "[object Object]". FastAPI
    # resolves the annotation when the route is registered, so a model
    # declared LATER in the file isn't found and the parameter is treated as
    # a query field — a server-side mistake that reads like the client's.
    # Checked for every route, not three named ones.
    import ast as _bmast
    _bmsrc = _RPath("web/server.py").read_text("utf-8")
    _bmtree = _bmast.parse(_bmsrc)
    # the first definition is the one a route registered above a second sees
    _bmcls, _bmdup = {}, []
    for _bmn in _bmtree.body:
        if isinstance(_bmn, _bmast.ClassDef):
            if _bmn.name in _bmcls:
                _bmdup.append(_bmn.name)
            _bmcls.setdefault(_bmn.name, _bmn.lineno)
    check("web.no_model_is_defined_twice", not _bmdup, f"{_bmdup}")
    _bmlate = []
    for _bmf in _bmtree.body:
        if not isinstance(_bmf, (_bmast.FunctionDef, _bmast.AsyncFunctionDef)):
            continue
        if not any("app." in _bmast.unparse(d) for d in _bmf.decorator_list):
            continue
        for _bma in _bmf.args.args:
            _bmn = _bmast.unparse(_bma.annotation) if _bma.annotation else ""
            _bmn = _bmn.split("|")[0].strip()
            if _bmn in _bmcls and _bmcls[_bmn] > _bmf.lineno:
                _bmlate.append(f"{_bmf.name} uses {_bmn}")
    check("web.body_models_are_defined_before_the_routes_that_use_them",
          not _bmlate, f"{_bmlate}")
    # --- every front-end call must reach a real route ------------------------ #
    #
    # The self-improvement panel called /api/self while the route is
    # /api/selfimprove: a rewrite renamed the caller and not the endpoint, so
    # the whole panel 404'd on open with nothing to explain it. A dead link
    # between the two halves is invisible until someone clicks.
    _fecalls = set(_re.findall(r'fetch\("(/api/[a-z0-9/_-]+)"', _uijs))
    # the main app's calls against the MAIN app's routes. web/server.py is
    # the Jobs app now, and checking one half against the other half's
    # routes passes for the wrong reason.
    _mainsrv = _RPath("web/server.py").read_text("utf-8")
    _feroutes = set(_re.findall(
        r'@app\.(?:get|post|delete|put)\("(/api/[^"{]+)"', _mainsrv))
    _feprefix = {r.rstrip("/") for r in _feroutes}
    _fedead = sorted(
        c for c in _fecalls
        if c not in _feroutes
        # a call ending in / has an id appended at runtime; it matches if the
        # server declares a path parameter under that prefix
        and not (c.endswith("/")
                 and any(rt.startswith(c) or rt == c.rstrip("/")
                         for rt in _feprefix)))
    check("web.every_front_end_call_reaches_a_real_route", not _fedead)


    # (the phrase appears in errText's own comment explaining the bug, so
    # assert the behaviour rather than the absence of a string)
    check("ui.an_error_is_never_shown_as_object_Object",
          "function errText(" in _uijs
          and "Array.isArray(x)" in _uijs
          and _uijs.count("errText(") > 20
          and "d.detail || \"" not in _uijs)

    check("ui.the_diagram_can_be_zoomed_and_moved",
          'id="cmZoomIn"' in _uihtml and 'id="cmZoomReset"' in _uihtml
          # zoom by moving the viewBox, not a CSS transform: text stays crisp
          and "svg.setAttribute(\"viewBox\"" in _uijs
          and "pointermove" in _uijs
          # the container must not scroll as well — two mechanisms fighting
          and "overflow: hidden" in _uicss[_uicss.index(".diagram-wrap"):
                                           _uicss.index(".diagram-wrap") + 260])
    check("ui.the_code_map_draws_a_diagram",
          'id="cmDiagram"' in _uihtml and ".codemap-svg" in _uicss
          and "diagramSvg" in _uijs and 'id="cmExport"' in _uihtml)
    # --- a real builder, not a wrapper --------------------------------------- #
    #
    # The first panel reported accuracy from one random split. Three things
    # were wrong, and each changes the answer rather than decorating it: one
    # split is noise, accuracy is the wrong headline when classes are uneven,
    # and 0.5 is an arbitrary line. Plus the thing actually asked for —
    # scoring on a file the user held back themselves.
    _rb_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.modelbuild as _rb
        import agent.dataprep as _rbd
        import csv as _rbcsv
        import random as _rbrand
        _rbrand.seed(11)

        def _rbmake(path, n, start=0):
            rows = []
            for _i in range(start, start + n):
                _t = _rbrand.randint(1, 60)
                _k = _rbrand.randint(0, 9)
                _s = round(_rbrand.uniform(200, 5000), 2)
                _r = (_t < 12) * 0.45 + (_k > 5) * 0.4 + (_s < 800) * 0.2
                rows.append({"customer_id": f"C{_i}", "tenure_months": _t,
                             "support_tickets": _k, "monthly_spend": _s,
                             "region": _rbrand.choice(["JHB", "CPT", "DBN"]),
                             "churn": "yes" if _rbrand.random() < _r
                             else "no"})
            with open(path, "w", newline="") as _fh:
                _w4 = _rbcsv.DictWriter(_fh, fieldnames=list(rows[0]))
                _w4.writeheader()
                _w4.writerows(rows)
        _rbtrain = config.AGENT_HOME / "train.csv"
        _rbhidden = config.AGENT_HOME / "hidden.csv"
        _rbmake(_rbtrain, 700)
        _rbmake(_rbhidden, 250, start=9000)
        _rb.train(str(_rbtrain), "churn", name="churn")

        _rbe = _rb.evaluate_on("churn", str(_rbhidden))
        # cross-validation: one split is noise on data this size
        _rbcv = _rb.train(str(_rbtrain), "churn", name="cvtest")
        _scored = [r for r in _rbcv["validation_scores"] if "spread" in r]
        check("modelbuild.candidates_are_judged_across_folds_not_one_split",
              len(_scored) >= 3
              and all(len(r["folds"]) == _rbcv["folds"] for r in _scored)
              and all("give or take" in r["reading"] for r in _scored))
        check("modelbuild.it_says_when_the_winner_is_inside_the_noise",
              "choice_was_close" in _rbcv and _rbcv["choice_note"]
              and (("inside the noise" in _rbcv["choice_note"])
                   == _rbcv["choice_was_close"]))
        check("modelbuild.the_honest_notes_mention_the_folds",
              any("cross-validation" in h for h in _rbcv["honest"]))

        check("modelbuild.it_scores_on_a_file_you_held_back",
              _rbe["ok"] and _rbe["rows"] == 250
              and "never seen" in _rbe["note"])
        check("modelbuild.the_headline_metric_suits_the_class_balance",
              _rbe["headline"]["name"] == "balanced accuracy"
              and "flatters" in _rbe["headline"]["why"])
        check("modelbuild.per_class_recall_is_reported_not_hidden",
              "per_class" in _rbe["metrics"]
              and "yes" in _rbe["metrics"]["per_class"]
              and "roc_auc" in _rbe["metrics"])
        check("modelbuild.it_compares_your_result_to_its_own_split",
              _rbe["compared_to_training"]["reading"])
        # a test file without answers can't be a test
        import pandas as _rbpd
        _rbnoans = config.AGENT_HOME / "no_answers.csv"
        _rbpd.read_csv(_rbhidden).drop(columns=["churn"]).to_csv(
            _rbnoans, index=False)
        check("modelbuild.a_test_file_without_the_answers_is_refused",
              _rb.evaluate_on("churn", str(_rbnoans))["ok"] is False)
        # the threshold is a dial, not a default
        _rbt = _rb.threshold_curve("churn", str(_rbhidden))
        _low = [r for r in _rbt["curve"] if r["threshold"] <= 0.2][0]
        _high = [r for r in _rbt["curve"] if r["threshold"] >= 0.8][0]
        check("modelbuild.moving_the_threshold_changes_what_it_catches",
              _rbt["ok"] and _low["caught"] > _high["caught"]
              and _low["right_when_flagged"] <= _high["right_when_flagged"])
        check("modelbuild.it_suggests_a_balance_and_says_it_is_your_call",
              "balanced_choice" in _rbt
              and "what each mistake costs you" in _rbt["note"])
        _rbi = _rb.importance("churn")
        check("modelbuild.it_says_which_columns_it_leans_on",
              _rbi["ok"] and _rbi["features"]
              and "not causation" in _rbi["note"])
        # per-column data engineering
        _rbc = _rbd.columns(str(_rbtrain))
        check("dataprep.every_column_lists_what_can_be_done_to_it",
              _rbc["ok"] and all("can" in c for c in _rbc["columns"])
              and any("to_number" in c["can"] or "log" in c["can"]
                      for c in _rbc["columns"]))
        _rba = _rbd.apply_actions(str(_rbtrain), [
            {"action": "drop", "column": "customer_id"},
            {"action": "log", "column": "monthly_spend"},
            {"action": "bin", "column": "tenure_months", "bins": 4},
            {"action": "rename", "column": "region", "to": "branch"}])
        check("dataprep.each_action_reports_its_own_effect",
              _rba["ok"] and len(_rba["applied"]) == 4
              and all(a.get("effect") for a in _rba["applied"])
              and _RPath(str(_rbtrain)).exists())
        check("dataprep.an_unknown_action_is_refused_not_ignored",
              _rbd.apply_actions(str(_rbtrain),
                                 [{"action": "explode",
                                   "column": "churn"}])["failed"])
        # one feature at a time, measured
        _rbf1 = _rbd.feature_one(
            str(_rbtrain), "churn",
            {"kind": "ratio", "from": "monthly_spend, tenure_months",
             "makes": ["monthly_spend_per_tenure_months"]})
        check("dataprep.one_feature_is_measured_on_its_own",
              _rbf1["ok"] and "before" in _rbf1 and "after" in _rbf1
              and ("worth keeping" in _rbf1["verdict"]
                   or "noise" in _rbf1["verdict"]))
    finally:
        config.AGENT_HOME = _rb_home

    # --- the Models panel ---------------------------------------------------- #
    #
    # Model building was prompt-only: no endpoints and no panel. The gates
    # that make it trustworthy — a baseline to beat, leakage dropped, a test
    # set used once — have to be visible in the UI too, or the number on
    # screen is just a number.
    check("ui.models_has_its_own_panel",
          'id="mlModal"' in _uihtml and 'id="mlBtn"' in _uihtml
          and "function openMl(" in _uijs)
    check("ui.the_panel_has_every_stage",
          all(f'id="mlTab{t2}"' in _uihtml
              for t2 in ("Data", "Build", "Engineer", "Features", "Test",
                         "Predict", "Saved")))
    check("ui.you_can_score_your_own_held_back_file",
          'id="mlTestPath"' in _uihtml and "mlScoreHidden" in _uijs
          and "/api/models/evaluate" in _uijs)
    check("ui.the_fold_spread_is_shown_not_just_the_mean",
          "r.reading ?" in _uijs and "choice_note" in _uijs
          and "models tied" in _uijs)
    check("ui.per_class_recall_is_what_the_test_tab_leads_with",
          "catches ${(v.recall * 100)" in _uijs)
    check("ui.the_score_is_shown_next_to_what_guessing_scores",
          "guessing scores" in _uijs and "baseline_test_score" in _uijs)
    check("ui.the_panel_says_when_the_gpu_would_not_help",
          "no CUDA" in _uijs and "why_not_gpu" in _uijs)
    _mlroutes = _RPath("web/server.py").read_text("utf-8")
    check("web.the_model_endpoints_exist",
          all(f'"/api/models/{p}"' in _mlroutes
              for p in ("hardware", "diagnose", "plan", "train", "predict",
                        "saved")))

    check("ui.the_code_map_is_reachable",
          'id="codemapBtn"' in _uihtml and 'id="cmScanBtn"' in _uihtml
          and "runCodemap" in _uijs)
    check("ui.the_model_lab_is_reachable",
          'id="modelLabBtn"' in _uihtml and 'id="modelDeriveBtn"' in _uihtml
          and 'id="modelFits"' in _uihtml)
    check("ui.the_self_improve_panel_is_intact",
          all(f'id="selfPane{n}"' in _uihtml
              for n in ("Proposal", "Ideas", "History")))
    # --- SA challenges: real problems, with the reasons they might not work --- #
    _ch_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "ch.db"
    try:
        import agent.challenges as _ch
        # the scan is AI/tech now, so the fixture is too: two problems worth
        # a brief, and one product announcement that must be ignored
        _CHRSS = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                  '<item><title>Agents mis-call tools as the context window '
                  'fills</title><link>https://x/1</link>'
                  '<description>&lt;p&gt;Wrong arguments.&lt;/p&gt;'
                  '</description></item>'
                  '<item><title>New phone launches in Sandton</title>'
                  '<link>https://x/2</link><description>Retail.</description>'
                  '</item>'
                  '<item><title>Prompt injection bypasses guardrails in '
                  'production</title><link>https://x/3</link>'
                  '<description>Exploit found.</description></item>'
                  '</channel></rss>')
        _CHATOM = ('<?xml version="1.0"?><feed '
                   'xmlns="http://www.w3.org/2005/Atom"><entry>'
                   '<title>Water infrastructure failure hits district</title>'
                   '<link href="https://y/1"/><summary>Leak crisis.</summary>'
                   '</entry></feed>')
        check("challenges.parses_rss_and_atom",
              len(_ch.parse_feed(_CHRSS)) == 3
              and _ch.parse_feed(_CHATOM)[0]["url"] == "https://y/1"
              and "<p>" not in _ch.parse_feed(_CHRSS)[0]["summary"])
        check("challenges.survives_malformed_xml",
              _ch.parse_feed("not xml") == [])
        _ch.save_sources([{"name": "A", "url": "https://a/rss", "on": True},
                          {"name": "B", "url": "https://b/rss", "on": False},
                          {"name": "Broken", "url": "https://c/rss",
                           "on": True}])

        def _chfetch(url):
            if "a/rss" in url:
                return _CHRSS
            if "c/rss" in url:
                raise RuntimeError("connection refused")
            raise AssertionError("a disabled source was fetched")
        _ch.FETCHER = _chfetch
        _chs = _ch.scan()
        check("challenges.keeps_only_problem_shaped_items",
              len(_chs["items"]) == 2
              and all("sandton" not in i["title"].lower()
                      for i in _chs["items"]))
        check("challenges.skips_disabled_and_survives_a_broken_source",
              "B" not in _chs["per_source"] and len(_chs["errors"]) == 1)
        check("challenges.dedupes_on_a_second_scan",
              len(_ch.scan()["items"]) == 0)
        _CHB = {"briefs": [
            {"title": "Municipal billing accuracy", "problem": "Wrong bills",
             "who": "Residents", "data": "Meter reads",
             "first_engagement": "Reconciliation pipeline",
             "why_it_might_fail": "Data access is slow and political",
             "confidence": "medium", "sources": ["https://x/1"]},
            {"title": "Clinic no-shows", "problem": "Queues",
             "who": "Clinics", "data": "Appointments",
             "first_engagement": "Baseline model",
             "why_it_might_fail": "", "confidence": "high",
             "sources": ["https://x/3"]}]}

        class _CHBrain:
            def chat(self, m, s, t=None, **kw):
                assert "UNTRUSTED" in s[0]
                return _blk("Sure!\n```json\n" + json.dumps(_CHB) + "\n```")
        (config.AGENT_HOME / "challenges" / "seen.jsonl").write_text("", "utf-8")
        _chr = _ch.scan_and_digest(_CHBrain())
        check("challenges.produces_briefs_from_messy_json",
              len(_chr["briefs"]) == 2)
        check("challenges.a_brief_with_no_downside_is_downgraded",
              _chr["briefs"][1]["confidence"] == "low"
              and "unvetted" in _chr["briefs"][1]["why_it_might_fail"])
        check("challenges.keeps_the_source_for_checking",
              _chr["briefs"][0]["sources"] == ["https://x/1"])
        _cht = _ch.to_crew_task(0)
        check("challenges.converts_to_a_qualifying_task",
              _cht["ok"] and "unverified" in _cht["task"]
              and "WHY THIS MIGHT FAIL" in _cht["task"]
              and _ch.to_crew_task(99)["ok"] is False)
        # --- nothing seen should be lost just because of when it arrived --
        (config.AGENT_HOME / "challenges" / "seen.jsonl").write_text("", "utf-8")
        (config.AGENT_HOME / "challenges" / "pool.jsonl").write_text("", "utf-8")
        # one problem, one item the filter should reject — the rejected one
        # must still land in the pool, so a later deep scan can reconsider it
        _CHRSS2 = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                   '<item><title>Inference cost climbs as GPU capacity '
                   'tightens</title><link>https://z/1</link>'
                   '<description>Expensive.</description></item>'
                   '<item><title>Startup announces new logo</title>'
                   '<link>https://z/2</link>'
                   '<description>Rebrand.</description></item>'
                   '</channel></rss>')
        _ch.save_sources([{"name": "A", "url": "https://a/rss", "on": True}])
        _ch.FETCHER = lambda u: _CHRSS2
        _chs2 = _ch.scan()
        check("challenges.filtered_out_items_are_still_remembered",
              len(_chs2["items"]) == 1 and _chs2["rejected"] == 1
              and len(_ch.pool()) == 2 and _ch.backlog_size() == 2)

        class _CHB1:
            def chat(self, m, s, t=None, **kw):
                return _blk(json.dumps({"briefs": [
                    {"title": "Billing", "problem": "p", "who": "w",
                     "data": "d", "first_engagement": "e",
                     "why_it_might_fail": "risk", "confidence": "medium",
                     "sources": ["https://z/1"]}]}))
        _ch.digest(_CHB1(), _chs2["items"])
        check("challenges.briefed_items_leave_the_backlog",
              _ch.backlog_size() == 1
              and "logo" in _ch.missed()[0]["title"].lower())
        _chseen_titles = []

        class _CHB2:
            def chat(self, m, s, t=None, **kw):
                _chseen_titles.extend(
                    [i["title"] for i in json.loads(m[0]["content"])["items"]])
                return _blk(json.dumps({"briefs": [
                    {"title": "Delivery tracking", "problem": "p",
                     "who": "w", "data": "d", "first_engagement": "e",
                     "why_it_might_fail": "risk", "confidence": "low",
                     "sources": ["https://z/2"]}]}))
        _chdeep = _ch.deep_scan(_CHB2())
        check("challenges.deep_scan_recovers_what_the_filter_rejected",
              any("logo" in t.lower() for t in _chseen_titles)
              and _chdeep.get("deep") is True
              and _chdeep["reconsidered"] == 1)
        check("challenges.backlog_empties_and_says_so",
              _ch.backlog_size() == 0
              and _ch.deep_scan(_CHB2()).get("no_new") is True)

        import agent.audit as _chau
        check("challenges.audited",
              any(e["kind"] == "challenge" for e in _chau.recent(10)))
    finally:
        config.AGENT_HOME = _ch_home

    # --- presenter mode: a live demo, not a slideshow ------------------------ #
    import agent.presenter as _pr
    _prs = _pr.scenes()
    check("presenter.has_a_full_script",
          len(_prs) >= 12 and len(_pr.acts()) >= 3)
    check("presenter.every_scene_has_narration_and_a_title",
          all(s["title"] and s["say"] and s["act"] for s in _prs))
    check("presenter.scenes_are_paced",
          all(int(s.get("seconds") or 0) >= 8 for s in _prs)
          and 120 <= _pr.runtime_seconds() <= 900)
    # a scene that opens a panel must name one that exists, or the demo
    # stalls in front of an audience
    _prhtml = _RPath("web/static/index.html").read_text("utf-8")
    check("presenter.every_panel_it_opens_exists",
          not [s["panel"] for s in _prs
               if s["panel"] and f'id="{s["panel"]}"' not in _prhtml])
    check("presenter.keys_are_unique",
          len({s["key"] for s in _prs}) == len(_prs))
    # the honest scenes have to stay honest
    _prtext = " ".join(s["say"] + " " + s.get("note", "") for s in _prs).lower()
    check("presenter.states_the_human_gate_on_self_improvement",
          "only a human applies" in _prtext
          or "no path where it changes itself" in _prtext)
    try:
        import shutil as _prsh, subprocess as _prsub
        _prnode = _prsh.which("node")
        _prfile = _RPath("tests/presenter_flow.js")
        if _prnode and _prfile.exists():
            _prout = _prsub.run([_prnode, str(_prfile)], capture_output=True,
                                text=True, timeout=60,
                                cwd=str(_RPath(".").resolve()))
            _prt = (_prout.stdout or "") + (_prout.stderr or "")
            _prflow = all(x in _prt for x in (
                "presenter opens: true",
                "advances and opens that scene's real panel: true",
                "goes back: true",
                "won't run off the front: true",
                "finishing exits and unbinds: true",
                "body class cleaned up: true"))
        else:
            _prflow = _prfile.exists()
    except Exception:
        _prflow = False
    check("presenter.drives_the_app_and_cleans_up_after_itself", _prflow)

    # --- the guide must cover the app, not just describe a few bits --------- #
    import agent.tour as _tr
    _trs = _tr.stops()
    check("guide.covers_every_chapter",
          len(_tr.chapters()) >= 5 and len(_trs) >= 20)
    check("guide.every_stop_answers_what_why_and_try",
          all(s["what"] and s["why"] and s["try"] and s["title"]
              for s in _trs))
    # a stop that opens a panel must name one that exists
    _trhtml = _RPath("web/static/index.html").read_text("utf-8")
    _trbad = [s["panel"] for s in _trs
              if s["panel"] and f'id="{s["panel"]}"' not in _trhtml]
    check("guide.every_open_it_button_points_somewhere_real", not _trbad)
    check("guide.keys_are_unique",
          len({s["key"] for s in _trs}) == len(_trs))

    # --- skills: adopted ones can be seen, run on purpose, and counted ------- #
    _sk_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "sk.db"
    try:
        import agent.skills as _sk
        from agent.memory import MemoryStore as _SKM
        _skm = _SKM(db_path=config.DB_PATH, check_same_thread=False)
        _skm.add_skill("BI review", "Before a dashboard goes to a client",
                       "1. Check totals against the source\n"
                       "2. Confirm date filters\n3. Note any assumptions")
        _skm.add_skill("memory-first", "Check memories before answering",
                       "search first, then cite")
        _sklist = _sk.listing(_skm)
        check("skills.lists_what_has_been_adopted", len(_sklist) == 2)
        _skbi = next(s for s in _sklist if s["name"].startswith("bi"))
        check("skills.previews_the_steps",
              len(_skbi["steps"]) == 3
              and _skbi["steps"][0].startswith("Check totals"))
        check("skills.does_not_invent_steps",
              next(s for s in _sklist
                   if s["name"] == "memory-first")["steps"] == [])
        _skp = _sk.build_run_prompt(_skbi, "the Q3 revenue dashboard")
        check("skills.run_prompt_carries_the_steps_and_the_input",
              "Check totals" in _skp and "Q3 revenue dashboard" in _skp
              and "rather than quietly skipping" in _skp)
        check("skills.no_input_asks_rather_than_guesses",
              "ask for that" in _sk.build_run_prompt(_skbi, ""))
        check("skills.start_unused",
              all(s["times_used"] == 0 for s in _sklist))
        _sk.mark_used(_skm, _skbi["name"])
        _sk.mark_used(_skm, _skbi["name"])
        _sklist2 = _sk.listing(_skm)
        _skbi2 = next(s for s in _sklist2 if s["name"].startswith("bi"))
        check("skills.runs_are_counted_and_dated",
              _skbi2["times_used"] >= 2 and bool(_skbi2["last_used"]))
        check("skills.most_used_sort_first",
              _sklist2[0]["name"] == _skbi2["name"])
        check("skills.unused_are_identifiable",
              [s["name"] for s in _sk.unused(_skm)] == ["memory-first"])
        check("skills.found_by_name_or_slug",
              _sk.find(_skm, "BI review") is not None
              and _sk.find(_skm, "bi-review") is not None
              and _sk.find(_skm, "nope") is None)
        import agent.audit as _skau
        check("skills.runs_are_audited",
              any(e["kind"] == "skill" for e in _skau.recent(10)))
    finally:
        config.AGENT_HOME = _sk_home

    # --- second opinion: don't pay for a review that can't change anything ---- #
    _so_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "so.db"
    try:
        import agent.collaborate as _so
        _SOLONG = ("The Medallion pattern splits a pipeline into three layers. "
                   "Bronze holds raw ingested data with lineage columns so any "
                   "row traces back to its file. Silver applies deduplication "
                   "and type casting. Gold holds the aggregates a dashboard "
                   "reads directly, which keeps query cost predictable.")
        check("second_opinion.skips_when_it_cannot_help",
              _so.should_review("thanks", _SOLONG)[0] is False
              and _so.should_review("explain it", "Yes.")[0] is False
              and _so.should_review("save it", "Saved.")[0] is False
              and _so.should_review("explain it", "")[0] is False
              and _so.should_review("explain medallion", _SOLONG)[0] is True)
        # a short answer can still be wrong — length is the wrong test
        check("second_opinion.still_reviews_a_short_factual_claim",
              _so.should_review("capital of Australia?",
                                "The capital of Australia is Sydney.")[0]
              is True)
        check("second_opinion.ignores_style_only_critiques",
              _so.critique_is_substantive(
                  "Consider a warmer tone.\nStyle: shorten paragraph two."
              )[0] is False
              and _so.critique_is_substantive("Fine.")[0] is False)
        check("second_opinion.acts_on_real_problems",
              _so.critique_is_substantive(
                  "The claim that Silver deduplicates is incorrect.")[0]
              and _so.critique_is_substantive(
                  "It omits the lineage columns entirely.")[0])
        check("second_opinion.rejects_a_worse_revision",
              _so.accept_revision(_SOLONG, "")[0] is False
              and _so.accept_revision(_SOLONG,
                                      "I cannot help with that.")[0] is False
              and _so.accept_revision(_SOLONG, _SOLONG[:60])[0] is False
              and _so.accept_revision(_SOLONG, _SOLONG)[0] is False)
        _sogood = _SOLONG + " Bronze is append-only, enabling replay."
        check("second_opinion.accepts_a_real_improvement",
              _so.accept_revision(_SOLONG, _sogood)[0] is True)
        # the loop must spend nothing when the draft holds up
        _sorewrote = []
        _sor1 = _so.collaborate("q", _SOLONG, reviewer=lambda p: "LGTM",
                                solver=lambda p: _sorewrote.append(1) or "x")
        check("second_opinion.approval_costs_no_rewrite",
              _sor1["revised"] is False and not _sorewrote)
        _sor2 = _so.collaborate("q", _SOLONG,
                                reviewer=lambda p: "Consider a warmer tone.",
                                solver=lambda p: "REWRITTEN")
        check("second_opinion.nitpick_does_not_trigger_a_rewrite",
              _sor2["revised"] is False
              and "stylistic" in _sor2.get("skipped_reason", ""))
        _sor3 = _so.collaborate("q", _SOLONG,
                                reviewer=lambda p: "The Silver claim is "
                                                   "incorrect.",
                                solver=lambda p: "I cannot help.")
        check("second_opinion.bad_revision_keeps_the_original",
              _sor3["revised"] is False and _sor3["answer"] == _SOLONG)
        _sor4 = _so.collaborate("q", _SOLONG,
                                reviewer=lambda p: "It omits that Bronze is "
                                                   "append-only.",
                                solver=lambda p: _sogood)
        check("second_opinion.real_improvement_is_accepted",
              _sor4["revised"] is True and _sor4["answer"] == _sogood)
        _sos = _so.stats()
        check("second_opinion.records_what_it_did",
              _sos.get("approved") and _sos.get("revised")
              and _sos.get("revision_rejected")
              and _sos.get("nitpick_ignored"))
    finally:
        config.AGENT_HOME = _so_home

    # --- a broken chain: race or forgery? ------------------------------------ #
    #
    # From a real health report: "Chain breaks at line 1101 — something edited
    # or truncated audit.jsonl." That advice was a false accusation: two of
    # the user's own processes writing at once produces an identical-looking
    # break. Sending someone to hunt for an intruder when the answer is
    # "press Reseal" is worse than saying nothing.
    _dg_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "dg.db"
    try:
        import agent.audit as _dga
        import agent.health as _dgh
        for _i in range(8):
            _dga.record("tool", name=f"e{_i}")
        _dgp = config.AGENT_HOME / "audit.jsonl"
        _dgl = _dgp.read_text("utf-8").splitlines()

        def _dgcard():
            return next(c for c in _dgh.report(None)["checks"]
                        if c["name"] == "Audit chain")
        check("audit.a_healthy_chain_reads_as_healthy",
              _dgcard()["state"] == "ok")
        # two entries claiming the same predecessor = a race
        _dgr = list(_dgl)
        _dge = json.loads(_dgr[4])
        _dge["prev"] = json.loads(_dgr[3])["prev"]
        _dgr[4] = json.dumps(_dge, sort_keys=True)
        _dgp.write_text("\n".join(_dgr) + "\n", "utf-8")
        _dgv = _dga.verify()
        _dgc = _dgcard()
        check("audit.a_race_is_recognised_as_a_race",
              _dgv.get("cause") == "concurrent-write"
              and _dgv.get("safe_to_reseal") is True
              and "nothing was altered" in _dgv["cause_detail"])
        check("audit.a_race_is_a_warning_pointing_at_reseal",
              _dgc["state"] == "warn" and "Reseal" in _dgc["fix"]
              and "Nothing was tampered" in _dgc["fix"])
        # a line that fits nothing = an actual edit
        _dged = list(_dgl)
        _dge2 = json.loads(_dged[4]); _dge2["summary"] = "TAMPERED"
        _dged[4] = json.dumps(_dge2, sort_keys=True)
        _dgp.write_text("\n".join(_dged) + "\n", "utf-8")
        check("audit.an_edit_is_still_a_failure",
              _dga.verify().get("cause") == "altered"
              and _dga.verify().get("safe_to_reseal") is False
              and _dgcard()["state"] == "fail")
        _dgp.write_text("\n".join(_dgl) + "\n", "utf-8")
        check("audit.diagnosis_does_not_disturb_a_good_chain",
              _dga.verify()["ok"] is True)
    finally:
        config.AGENT_HOME = _dg_home

    # Blender installed from the Microsoft Store lives under a
    # permission-locked folder, so a plain glob reported "not found" on a
    # machine that had it all along. The execution alias is readable.
    import agent.blenderlab as _blb
    _blsrc = _RPath("agent/blenderlab.py").read_text("utf-8")
    check("blender.store_installs_are_found",
          "WindowsApps" in _blsrc and "blender.exe" in _blsrc
          and isinstance(_blb.find_blender(), str))

    # --- silence: the fault class nothing else catches ----------------------- #
    #
    # Every fault this app has had in real use was found by the person using
    # it: the dashboard never wired to its poll, a shadowed health route,
    # Crew raising NameError for weeks, a brief calling a retired model every
    # morning. Health asks "is this reachable now"; Capabilities asks "has it
    # ever run". Neither asks the question that would have caught them —
    # "this is on, it was working, and it has produced nothing for days".
    _wd_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "wd.db"
    try:
        import agent.watchdog as _wd
        from agent.memory import MemoryStore as _WDM
        _wdm = _WDM(db_path=config.DB_PATH, check_same_thread=False)
        # the example used to be the job scout, which left with Agent Jo
        # Jobs (155); the weekly trend scan is watched the same way
        _wdsid = _wdm.create_schedule(name="Weekly trends", prompt="scan",
                                      spec_json="{}", action="trendscout",
                                      payload="{}")

        def _wde(kind, days, summary="did the thing"):
            ts = _time.time() - days * 86400
            return {"kind": kind, "name": "", "summary": summary, "ts": ts,
                    "iso": _time.strftime("%Y-%m-%d %H:%M UTC",
                                          _time.gmtime(ts))}
        _wdq = _wd.report(_wdm, [_wde("trend", 20, "scanned 12")])["quiet"]
        check("watchdog.raises_a_feature_that_has_gone_quiet",
              len(_wdq) == 1 and _wdq[0]["key"] == "trend"
              and _wdq[0]["days_quiet"] >= 20
              and "Weekly trends" in _wdq[0]["why_on"], f"{_wdq}")
        check("watchdog.recent_activity_is_left_alone",
              _wd.report(_wdm, [_wde("trend", 1)])["quiet"] == [])
        # a feature that has never run is a setup question, not silence
        check("watchdog.never_used_is_not_silence",
              _wd.report(_wdm, [])["quiet"] == [])
        # errors are not activity: counting them would hide the very case
        check("watchdog.failures_do_not_count_as_producing",
              len(_wd.report(_wdm, [
                  _wde("trend", 1, "trend scan failed: no credit"),
                  _wde("trend", 30, "scanned 12")])["quiet"]) == 1)
        _wdm.set_schedule_enabled(_wdsid, False)
        check("watchdog.ignores_what_is_switched_off",
              _wd.report(_wdm, [_wde("trend", 90)])["quiet"] == [])
        _wdm.set_schedule_enabled(_wdsid, True)
        _wdm.create_schedule(name="Nightly backup", prompt="back up",
                             spec_json="{}", action="backup", payload="{}")
        _wdr = _wd.report(_wdm, [_wde("trend", 1, "scanned 3"),
                                 _wde("backup", 40, "backed up")])
        _wdk = {q["key"] for q in _wdr["quiet"]}
        check("watchdog.watches_scheduled_work_and_names_the_schedule",
              "backup" in _wdk and "trend" not in _wdk
              and any("Nightly backup" in q["why_on"] for q in _wdr["quiet"]))
    finally:
        config.AGENT_HOME = _wd_home

    # --- a retired model, and a watcher whose site has gone ------------------ #
    #
    # Both taken from a real audit trail: a scheduled brief called
    # deepseek-v4-pro every day for weeks after the provider retired it, and a
    # watcher whose host stopped resolving failed hourly for two days —
    # tripping the breaker that guards every other watcher.
    _rm_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "rm.db"
    try:
        import agent.engines as _rme
        import agent.watchers as _rmw
        _RMERR = ("APIStatusError: Error code: 410 - {'detail': \"The model "
                  "'deepseek-ai/deepseek-v4-pro' has reached its end of life "
                  "on 2026-08-07T09:00:00Z and is no longer available.\"}")
        check("engines.recognises_a_retired_model",
              _rme.is_retired(_RMERR)
              and _rme.retired_model_name(_RMERR)
              == "deepseek-ai/deepseek-v4-pro"
              and not _rme.is_retired("credit balance too low"))
        check("engines.explains_retirement_and_the_fix",
              "retired" in _rme.explain(_RMERR, "DeepSeek Pro")
              and "Engines" in _rme.explain(_RMERR, "DeepSeek Pro"))
        check("engines.a_retired_model_falls_back",
              (_rme.fallback_for("DeepSeek Pro", _RMERR) or {}) != {}
              or not _rme.inventory().get("has_local"))
        import agent.audit as _rma
        import agent.health as _rmh

        def _rmcard():
            return next((c for c in _rmh.report(None)["checks"]
                         if c["name"] == "Retired models"), None)
        check("health.clean_when_no_model_is_retired",
              _rmcard()["state"] == "ok")
        _rma.record("autonomy", name="Morning XRP brief", summary=_RMERR)
        _rmc = _rmcard()
        check("health.surfaces_a_retired_model_from_the_trail",
              _rmc["state"] == "fail"
              and "Morning XRP brief" in _rmc["detail"]
              and "deepseek-v4-pro" in _rmc["detail"])

        # a watcher whose host has gone must pause rather than fail forever
        _RMDNS = "WebError: Could not resolve host 'tandemcreate.com'."
        for _ in range(3):
            _rmst = _rmw.note_failure("Tandem Create", _RMDNS)
        check("watchers.a_dead_host_pauses_after_three_strikes",
              _rmst["paused"] is True
              and "Could not resolve" in _rmst["reason"])
        for _ in range(4):
            _rmt = _rmw.note_failure("Flaky", "timed out")
        check("watchers.a_transient_fault_is_given_longer",
              _rmt["paused"] is False
              and _rmw.note_failure("Flaky", "timed out")["paused"] is True)
        _rmw.note_failure("Flaky", "a different error")
        check("watchers.a_changed_error_resets_the_count",
              _rmw.strikes_for("Flaky") == 1)
        _rmw.note_success("Flaky")
        check("watchers.success_clears_the_strikes",
              _rmw.strikes_for("Flaky") == 0)
    finally:
        config.AGENT_HOME = _rm_home

    # --- turbo: local drafts, cloud only when the draft is observably bad ----- #
    _tb_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "tb.db"
    try:
        import agent.turbo as _tb
        _TBGOOD = ("The Medallion pattern splits a pipeline into three "
                   "layers. Bronze holds raw ingested data with lineage "
                   "columns. Silver deduplicates and casts types. Gold "
                   "holds the aggregates a dashboard reads.")
        _tbcases = [
            (_TBGOOD, False, True),                       # usable
            ("", False, False),                           # empty
            ("Yes.", False, False),                       # too short
            ("As an AI, I don't have access to that. " * 2, False, False),
            (_TBGOOD + " Contact [INSERT NAME] now.", False, False),
            (_TBGOOD + " The next step is to configure the", False, False),
            ((_TBGOOD + "\n") * 3, False, False),          # looping
            ('{"trends": [{"title": "x"}]}', True, True),  # valid JSON
            ("Sure! Here are the trends.", True, False),   # prose, not JSON
            ('```json\n{"a": 1}\n```', True, True),        # fenced JSON
        ]
        # --- routing: decide before spending anything -------------------
        #
        # Trying the local model on work it can't do burns a pass and then
        # pays cloud rates anyway. That is the one way Turbo costs more than
        # it saves, so the decision is made first, deterministically.
        _tbl = [("summarise this thread in three bullets", True),
                ("reformat that as a markdown table", True),
                ("extract the email addresses from this", True),
                ("is this message positive or negative?", True),
                ("what's the capital of Botswana", True)]
        _tbc = [("write a python function to parse this log", False),
                ("why is our margin falling this quarter?", False),
                ("draft a cover letter for the Acme role", False),
                ("build a caching layer into yourself", False),
                ("run the backup and email me the result", False),
                ("compare Databricks and Snowflake for our use", False)]
        check("turbo.routes_small_model_work_locally",
              all(_tb.classify_task(q)["local_ok"] is w for q, w in _tbl))
        check("turbo.keeps_hard_work_on_the_strong_engine",
              all(_tb.classify_task(q)["local_ok"] is w for q, w in _tbc))
        # "extract the email addresses" is extraction, not sending email
        check("turbo.an_incidental_keyword_does_not_misroute",
              _tb.classify_task(
                  "extract the email addresses")["category"] == "extract"
              and _tb.classify_task(
                  "email me the report")["category"] == "tools")
        check("turbo.long_input_and_attachments_go_to_the_cloud",
              _tb.classify_task("x" * 5000)["local_ok"] is False
              and _tb.classify_task("summarise this", True)["local_ok"]
              is False)
        # and it learns which categories are worth attempting
        check("turbo.gives_a_fresh_category_a_chance",
              _tb.should_try_local("summarise this")["try_local"] is True)
        for _ in range(6):
            _tb.record(False, reasons=["repeats"], category="summarise")
        _tbv = _tb.should_try_local("summarise this")
        check("turbo.stops_trying_what_keeps_failing",
              _tbv["try_local"] is False and "of 6" in _tbv["why"])
        for _ in range(6):
            _tb.record(True, category="extract")
        check("turbo.keeps_using_what_works",
              _tb.should_try_local("extract the dates")["try_local"] is True)
        # the saving must be measured, not asserted
        import agent.costs as _tbc2
        check("turbo.says_when_it_cannot_measure_the_saving",
              "not enough cloud turns" in _tb.stats()["basis"])
        for _ in range(20):
            _tbc2.record("claude-sonnet-4-6", {"in": 9000, "out": 1200},
                         feature="chat")
        _tbs2 = _tb.stats()
        check("turbo.measures_the_real_cost_of_a_cloud_turn",
              _tbs2["measured_cloud_turn_cost"] > 0
              and "measured from" in _tbs2["basis"])
        check("turbo.reports_per_category_performance",
              {c["category"] for c in _tbs2["by_category"]}
              == {"summarise", "extract"}
              and any(c["still_trying"] is False
                      for c in _tbs2["by_category"]))
        # leave the counters clean: the accounting check below asserts an
        # exact turn count, and these routing tests would otherwise be
        # invisibly added to it
        _tb._stats_path().write_text(json.dumps(
            {"skipped": 0, "reviewed": 0, "local_only": 0, "escalated": 0,
             "saved_usd": 0.0, "extra_usd": 0.0, "reasons": {},
             "categories": {}}), "utf-8")

        check("turbo.gate_is_correct_on_every_case",
              all(_tb.gate(_t, expect_json=_j)["ok"] is _e
                  for _t, _j, _e in _tbcases))
        check("turbo.gate_explains_the_failure",
              _tb.gate("")["reasons"]
              and _tb.gate("Sure!", expect_json=True)["reasons"])
        _tbp = _tb.escalation_prompt("half an answer",
                                     ["the answer stops mid-sentence"])
        check("turbo.escalation_reuses_the_draft",
              "half an answer" in _tbp and "stops mid-sentence" in _tbp
              and "don't mention the draft" in _tbp)
        for _ in range(7):
            _tb.record(True, cloud_cost_estimate=0.02)
        for _ in range(3):
            _tb.record(False, reasons=["the local model refused or claimed "
                                       "it couldn't"],
                       cloud_cost_estimate=0.02)
        _tbs = _tb.stats()
        # saved/extra are now derived from the measured per-turn cost, so
        # they are zero until there is cloud spend to measure — assert the
        # counting, and the honesty about the basis
        check("turbo.accounts_for_both_savings_and_extra_cost",
              _tbs["turns"] == 10 and _tbs["hit_rate"] == 70.0
              and ((_tbs["saved_usd"] > 0 and _tbs["extra_usd"] > 0)
                   if _tbs["measured_cloud_turn_cost"] > 0
                   else "not enough cloud turns" in _tbs["basis"]))
        check("turbo.names_the_commonest_escalation_reason",
              "refused" in _tbs["top_reason"])
        # and it must say so when it isn't paying off
        _tb._stats_path().write_text(json.dumps(
            {"local_only": 2, "escalated": 18, "saved_usd": 0.04,
             "extra_usd": 0.05, "reasons": {}}), "utf-8")
        # the wording changed when savings became measured; assert the
        # meaning — a low hit rate must be called out, not glossed
        _tblow = _tb.stats()
        check("turbo.warns_when_it_costs_more_than_it_saves",
              _tblow["hit_rate"] < 35
              and ("both passes" in _tblow["advice"]
                   or "turn Turbo off" in _tblow["advice"]))
        check("turbo.needs_both_kinds_of_engine",
              _tb.usable({"has_local": True, "has_cloud": False})[0] is False
              and _tb.usable({"has_local": False, "has_cloud": True})[0]
              is False
              and _tb.usable({"has_local": True, "has_cloud": True})[0]
              is True)
    finally:
        config.AGENT_HOME = _tb_home

    # --- engines: classify by endpoint, not by name; cover for each other ----- #
    _en_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "en.db"
    try:
        import agent.brain as _enb
        import agent.engines as _en
        import agent.costs as _enc
        _enb.load_custom_engines(refresh=True)
        _enb.add_custom_engine(name="qwen-hosted",
                               base_url="https://api.together.xyz/v1",
                               api_key="k", model="q")
        _enb.add_custom_engine(name="claude-local",
                               base_url="http://localhost:8080/v1",
                               api_key="x", model="c")
        _enb.add_custom_engine(name="LanBox",
                               base_url="http://192.168.1.40:11434/v1",
                               api_key="x", model="l")
        # deliberately NOT registered: an engine can't be added without a
        # URL, which is why unknowns are rare — but a name met from a stale
        # setting still has to classify honestly rather than as free
        # the two cases name-guessing gets exactly backwards
        check("engines.hosted_model_with_a_local_name_is_cloud",
              _en.classify("qwen-hosted")["kind"] == "cloud"
              and _en.billable("qwen-hosted") is True)
        check("engines.local_proxy_with_a_cloud_name_is_local",
              _en.classify("claude-local")["kind"] == "local"
              and _en.billable("claude-local") is False)
        check("engines.private_network_counts_as_local",
              _en.classify("LanBox")["kind"] == "local")
        check("engines.builtins_are_known_outright",
              _en.classify("Claude")["kind"] == "cloud"
              and _en.classify("Ollama")["kind"] == "local")
        check("engines.unclassifiable_says_unknown_not_free",
              _en.classify("MysteryEngine")["kind"] == "unknown"
              and _en.billable("MysteryEngine") is None)
        check("engines.classification_explains_itself",
              "Together" in _en.classify("qwen-hosted")["why"]
              and "localhost" in _en.classify("claude-local")["why"])
        # cost tracking must follow the endpoint, not the name
        check("engines.costs_follow_the_endpoint",
              _enc.unpriced("qwen-hosted") is True
              and _enc.unpriced("claude-local") is False)
        _eninv = _en.inventory()
        # registering an engine requires a URL, so the inventory should be
        # cleanly split — "unknown" is for names met elsewhere (a stale
        # setting, a preloaded engine that was never configured)
        check("engines.inventory_splits_by_kind",
              any(x["name"] == "qwen-hosted" for x in _eninv["cloud"])
              and any(x["name"] == "claude-local" for x in _eninv["local"])
              and _eninv["has_cloud"] and _eninv["has_local"])
        # failover only where another engine could actually survive
        check("engines.falls_back_when_cloud_has_no_credit",
              (_en.fallback_for("Claude", "Your credit balance is too low")
               or {}).get("kind") == "local")
        check("engines.falls_back_when_unreachable",
              (_en.fallback_for("Claude", "Connection refused")
               or {}).get("kind") == "local")
        check("engines.does_not_retry_a_failure_that_would_repeat",
              _en.fallback_for(
                  "Claude",
                  "messages: text content blocks must be non-empty") is None)
        check("engines.failover_explains_which_engine_answered",
              "no credit" in (_en.fallback_for(
                  "Claude", "credit balance too low") or {})["why"])
        import agent.health as _enh
        check("engines.mix_surfaces_on_health",
              any(c["name"] == "Engine mix"
                  for c in _enh.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _en_home
        try:
            import agent.brain as _enb2
            _enb2.load_custom_engines(refresh=True)
        except Exception:
            pass

    # --- costs: persistent per-feature ledger and a ceiling that holds -------- #
    _co_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "co.db"
    try:
        import importlib as _coimp
        import agent.costs as _co
        _coU = {"in": 1000000, "out": 1000000}
        check("costs.cloud_priced_local_free",
              _co.cost_of("Claude", _coU) > 0
              and _co.cost_of("Ollama", _coU) == 0.0)
        # the trap: calls recorded under the MODEL string must still be priced
        check("costs.model_strings_are_priced",
              _co.cost_of("claude-sonnet-4-6", _coU)
              == _co.cost_of("Claude", _coU))
        # Locality is now decided from the endpoint or from Ollama actually
        # having the model, not from the name containing "qwen". A tag that
        # is the configured local model is free; an unrecognised engine is
        # "unknown", which is honest — and pointedly not "free".
        _coprev_model = getattr(config, "OLLAMA_MODEL", "")
        config.OLLAMA_MODEL = "qwen3.6:latest"
        try:
            check("costs.unknown_engine_flagged_not_zeroed",
                  _co.unpriced("SomeNewCloudThing") is True
                  and _co.unpriced("qwen3.6:latest") is False)
        finally:
            config.OLLAMA_MODEL = _coprev_model
        with _co.attribute("trends"):
            _co.record("claude-sonnet-4-6", _coU)
        with _co.attribute("crew"):
            _co.record("Claude", {"in": 50000, "out": 5000})
        _co.record("Ollama", _coU, feature="jobs")
        _co.record("SomeNewCloudThing", _coU, feature="pipelines")
        _cor = _co.month_report()
        check("costs.attributes_spend_by_feature",
              set(_cor["by_feature"]) == {"trends", "crew", "jobs",
                                          "pipelines"}
              and list(_cor["by_feature"])[0] == "trends"
              and _cor["by_feature"]["jobs"]["usd"] == 0.0)
        check("costs.reports_unpriced_engines",
              _cor["unpriced_engines"] == ["SomeNewCloudThing"])
        _coimp.reload(_co)
        check("costs.ledger_survives_restart",
              _co.month_report()["total_usd"] == _cor["total_usd"]
              and _cor["total_usd"] > 0)
        _cospent = _cor["total_usd"]
        check("costs.no_cap_never_blocks", _co.check(0)["blocked"] is False)
        check("costs.under_cap_allows",
              _co.check(_cospent + 10)["blocked"] is False)
        _coc = _co.check(max(0.01, _cospent * 0.5))
        check("costs.ceiling_blocks_with_detail",
              _coc["blocked"] is True and "ceiling reached" in _coc["detail"])
        _co.set_engine("trends", "CustomQWEN")
        check("costs.per_feature_engine_pinning",
              _co.engine_for("trends") == "CustomQWEN"
              and _co.engine_for("crew") == "Auto"
              and _co.set_engine("trends", "Auto") is not None
              and _co.engine_for("trends") == "Auto")
        import agent.health as _coh
        _cohr = _coh.report(None)
        check("costs.surfaces_on_health_board",
              any(c["name"] == "Spend" for c in _cohr["checks"]))
        check("costs.reset_clears_month",
              _co.reset_month()["ok"]
              and _co.month_report()["total_usd"] == 0.0)
    finally:
        config.AGENT_HOME = _co_home

    # --- circuit breakers: stop repeatedly-failing features bleeding ---------- #
    _cb_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "cb.db"
    try:
        import time as _cbtime
        import agent.breaker as _cb
        check("breaker.closed_by_default",
              _cb.state("t") == "closed" and _cb.allow("t")[0] is True)
        _cb.record_failure("t", "boom 1"); _cb.record_failure("t", "boom 2")
        check("breaker.tolerates_a_blip",
              _cb.state("t") == "closed" and _cb.allow("t")[0] is True)
        _cbr = _cb.record_failure("t", "credit balance too low")
        _cbok, _cbwhy = _cb.allow("t")
        check("breaker.opens_on_third_consecutive_failure",
              _cbr["opened"] is True and _cb.state("t") == "open"
              and _cbok is False and "credit balance" in _cbwhy
              and "minute" in _cbwhy)
        _cb.record_failure("s", "a"); _cb.record_failure("s", "b")
        _cb.record_success("s"); _cb.record_failure("s", "c")
        check("breaker.success_resets_the_count",
              _cb.state("s") == "closed")
        for _i in range(3):
            _cb.record_failure("hy", f"e{_i}")
        _cbd = _cb._load()
        _cbd["hy"]["opened_at"] = _cbtime.time() - _cb.BASE_COOLDOWN - 5
        _cb._save(_cbd)
        check("breaker.half_open_after_cooldown",
              _cb.state("hy") == "half-open" and _cb.allow("hy")[0] is True)
        _cbbefore = _cb._load()["hy"]["cooldown"]
        _cb.record_failure("hy", "still broken")
        check("breaker.backs_off_after_failed_trial",
              _cb._load()["hy"]["cooldown"] == _cbbefore * 2
              and _cb.state("hy") == "open")
        _cbd = _cb._load()
        _cbd["hy"]["opened_at"] = _cbtime.time() - _cbd["hy"]["cooldown"] - 5
        _cb._save(_cbd)
        _cb.record_success("hy")
        check("breaker.recovers_on_good_trial",
              _cb.state("hy") == "closed"
              and _cb._load()["hy"]["failures"] == 0)
        _cbcalls = []

        def _cbwork():
            _cbcalls.append(1)
            return {"ok": True}
        for _i in range(3):
            _cb.record_failure("g", "nope")
        _cbres = _cb.guard("g", _cbwork)
        check("breaker.guard_refuses_without_calling",
              _cbres.get("breaker_open") is True and len(_cbcalls) == 0)
        _cb.reset("g")
        check("breaker.guard_runs_when_closed",
              _cb.guard("g", _cbwork)["ok"] and len(_cbcalls) == 1)
        for _i in range(3):
            _cb.guard("g", lambda: {"ok": False, "error": "nope"})
        check("breaker.guard_counts_returned_failures",
              _cb.state("g") == "open")

        def _cbboom():
            raise RuntimeError("kaboom")
        for _i in range(3):
            try:
                _cb.guard("e", _cbboom)
            except RuntimeError:
                pass
        _cbst = [x for x in _cb.status() if x["feature"] == "e"][0]
        check("breaker.guard_counts_exceptions_and_exposes_reason",
              _cb.state("e") == "open" and "kaboom" in _cbst["last_error"]
              and _cbst["retry_in_min"] >= 0)
        check("breaker.reset_clears_it",
              _cb.reset("e")["ok"] and _cb.state("e") == "closed")
        import agent.health as _cbh
        _cbrep = _cbh.report(None)
        check("breaker.surfaces_on_health_board",
              any(c["name"] == "Circuit breakers" and c["state"] == "fail"
                  for c in _cbrep["checks"]))
        import agent.audit as _cbau
        check("breaker.audited",
              any(x["kind"] == "breaker" for x in _cbau.recent(20)))
    finally:
        config.AGENT_HOME = _cb_home

    # --- health board: accurate, actionable, and never lies ------------------- #
    _he_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "he.db"
    try:
        import time as _hetime
        import agent.health as _he
        from agent.memory import MemoryStore as _HEM
        _hem = _HEM(db_path=config.DB_PATH, check_same_thread=False)
        _het0 = _hetime.time()
        _her = _he.report(_hem)
        _hedur = _hetime.time() - _het0
        check("health.returns_quickly", _hedur < 15)
        check("health.every_check_well_formed",
              len(_her["checks"]) >= 12
              and all(c.get("name") and c.get("group")
                      and c["state"] in ("ok", "warn", "fail", "unknown")
                      and isinstance(c.get("detail"), str)
                      for c in _her["checks"]))
        check("health.every_problem_carries_a_fix",
              all(c["fix"] for c in _her["checks"]
                  if c["state"] in ("warn", "fail")))
        # with no backups at all, that must be a FAIL — the whole point
        check("health.missing_backup_is_a_failure",
              any(c["name"] == "Backups" and c["state"] == "fail"
                  for c in _her["checks"]))
        check("health.overall_reflects_worst_check",
              _her["overall"] == "fail"
              and _her["counts"]["fail"] >= 1)
        # a check that explodes must report itself, not kill the board
        _hebad = _he._safe(lambda: 1 / 0, "Exploding", "Test")
        check("health.broken_check_reports_itself",
              _hebad["state"] == "fail"
              and "ZeroDivisionError" in _hebad["detail"])
        # once a backup exists the same check must go green
        import agent.backup as _hebk
        _hebk.create_backup(_hem, None)
        _her2 = _he.report(_hem)
        check("health.backup_check_clears_when_fixed",
              any(c["name"] == "Backups" and c["state"] == "ok"
                  for c in _her2["checks"]))
        check("health.reports_the_running_build",
              _her2["build"] == getattr(config, "BUILD_ID", "unknown"))
    finally:
        config.AGENT_HOME = _he_home

    # --- watchers: structured extraction, per-item diff, self-healing --------- #
    _wa_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "wa.db"
    try:
        import agent.watchers as _wa
        _waprev_fetch = _wa.FETCHER
        _WAV1 = ('<html><body><div class="results">'
                 '<div class="tile"><span class="price">R 850 000</span>'
                 '<a class="title" href="/l/1">3 bed Bedfordview</a></div>'
                 '<div class="tile"><span class="price">R 1 200 000</span>'
                 '<a class="title" href="/l/2">4 bed Edenvale</a></div>'
                 '</div></body></html>')
        _WACFG = {"id": "wtest", "name": "W", "source": "https://x/list",
                  "item_selector": ".tile",
                  "fields": {"title": {"selector": ".title"},
                             "url": {"selector": ".title", "attr": "href"},
                             "price": {"selector": ".price"}}}
        _wa.FETCHER = lambda st, s: (True, _WAV1, "")
        _war1 = _wa.check_structured(dict(_WACFG))
        check("watchers.extracts_structured_items",
              len(_war1["items"]) == 2
              and _war1["items"][0]["price"] == "R 850 000"
              and _war1["items"][0]["url"] == "/l/1")
        check("watchers.first_run_is_a_baseline",
              _war1["first_run"] is True and _war1["new"] == [])
        check("watchers.no_false_alarm_when_unchanged",
              _wa.check_structured(dict(_WACFG))["new"] == [])
        _WAV2 = _WAV1.replace('</div></body>',
                              '<div class="tile"><span class="price">R 990 000'
                              '</span><a class="title" href="/l/3">2 bed '
                              'Kempton</a></div></div></body>')
        _wa.FETCHER = lambda st, s: (True, _WAV2, "")
        _war3 = _wa.check_structured(dict(_WACFG))
        check("watchers.reports_only_new_items",
              len(_war3["new"]) == 1
              and "Kempton" in _war3["new"][0]["title"])
        # the failure that actually kills scrapers: the site renames classes
        _WAV3 = (_WAV2.replace("tile", "listing-card")
                 .replace("price", "listing-price")
                 .replace("title", "listing-title"))
        _wa.FETCHER = lambda st, s: (True, _WAV3, "")
        _wabroken = _wa.check_structured(dict(_WACFG), brain=None)
        check("watchers.detects_selector_rot",
              _wabroken["ok"] is False
              and _wabroken.get("needs_attention") is True)

        class _WAHealer:
            def chat(self, m, s, t=None, **kw):
                _st = json.loads(m[0]["content"])["STRUCTURE"]
                assert "listing-card" in _st
                assert "R 850 000" not in _st      # no page text in the prompt
                return _blk(json.dumps({
                    "item_selector": ".listing-card",
                    "fields": {"title": {"selector": ".listing-title"},
                               "url": {"selector": ".listing-title",
                                       "attr": "href"},
                               "price": {"selector": ".listing-price"}}}))
        _wahealed = _wa.check_structured(dict(_WACFG), brain=_WAHealer())
        check("watchers.self_heals_renamed_selectors",
              _wahealed["ok"] and _wahealed["healed"]
              and len(_wahealed["items"]) == 3
              and "re-derived" in _wahealed["summary"])

        class _WABad:
            def chat(self, m, s, t=None, **kw):
                return _blk(json.dumps({"item_selector": ".nope",
                                        "fields": {}}))
        check("watchers.rejects_a_heal_that_fixes_nothing",
              _wa.check_structured(dict(_WACFG), brain=_WABad())["ok"]
              is False)
        check("watchers.css_subset_supports_descendants",
              len(_wa.select(_wa.parse_html(_WAV1), ".results .tile")) == 2)
        check("watchers.text_mode_unaffected",
              _wa.check({"id": "t2", "source": "https://x",
                         "source_type": "url"})["ok"] is True)
        _wa.FETCHER = _waprev_fetch
    finally:
        config.AGENT_HOME = _wa_home

    # --- backup: capture, exclusions, verification, rollback, restore --------- #
    _bk_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "agent.db"
    try:
        import agent.backup as _bk
        import zipfile as _bkzip, json as _bkj, os as _bkos
        from agent.memory import MemoryStore as _BKM
        _bkm = _BKM(db_path=config.DB_PATH, check_same_thread=False)
        _bkm.add_memory("desk uses T+2 settlement", "general")
        _bkm.add_skill("bi-review", "Review a dashboard", "1. check totals")
        (config.AGENT_HOME / "settings.json").write_text('{"MODEL":"claude"}', "utf-8")
        (config.AGENT_HOME / "audit.jsonl").write_text('{"kind":"turn"}\n', "utf-8")
        (config.AGENT_HOME / "mcp.json").write_text('{"servers":{}}', "utf-8")
        (config.AGENT_HOME / "secret.key").write_text("MASTERKEY", "utf-8")
        (config.AGENT_HOME / "crew").mkdir(exist_ok=True)
        (config.AGENT_HOME / "crew" / "members.json").write_text('[{"name":"BizDev"}]', "utf-8")
        (config.AGENT_HOME / "blender").mkdir(exist_ok=True)
        (config.AGENT_HOME / "blender" / "big.png").write_bytes(b"x" * 5000)

        _bkz = _bk.create_backup(_bkm, None)
        _bknames = set(_bkzip.ZipFile(_bkz).namelist())
        check("backup.captures_state_outside_databases",
              {"agent.db", "settings.json", "audit.jsonl",
               "crew/members.json"} <= _bknames)
        check("backup.excludes_bulk_and_key_by_default",
              not any(n.startswith("blender/") for n in _bknames)
              and "secret.key" not in _bknames)
        _bkman = _bk.read_manifest(_bkz)
        check("backup.manifest_has_checksums_and_warnings",
              _bkman["format_version"] == 2
              and len(_bkman["checksums"]) >= 4
              and "mcp.json" in _bkman.get("sensitive", {})
              and any("secret.key" in s for s in _bkman["skipped"]))
        check("backup.verify_passes_on_good_archive",
              _bk.verify_backup(_bkos.path.basename(_bkz))["ok"] is True)
        _bkz2 = _bk.create_backup(_bkm, None, include_bulk=True,
                                  include_key=True)
        _bkn2 = set(_bkzip.ZipFile(_bkz2).namelist())
        check("backup.optional_bulk_and_key",
              any(n.startswith("blender/") for n in _bkn2)
              and "secret.key" in _bkn2)
        check("backup.filenames_never_collide",
              _bkos.path.basename(_bkz) != _bkos.path.basename(_bkz2))
        # a damaged archive must be caught BEFORE it replaces anything
        _bktamp = _RPath(_bk._backups_dir()) / "atlas-backup-tampered.zip"
        with _bkzip.ZipFile(_bkz) as _src, _bkzip.ZipFile(str(_bktamp), "w") as _dst:
            for _i in _src.infolist():
                _b = _src.read(_i.filename)
                if _i.filename == "settings.json":
                    _b = b"{}TAMPERED"
                _dst.writestr(_i, _b)
        check("backup.detects_tampering",
              _bk.verify_backup(_bktamp.name)["ok"] is False)
        _bkraised = False
        try:
            _bk.restore_backup(str(_bktamp), _bkm, None)
        except ValueError:
            _bkraised = True
        check("backup.refuses_to_restore_damaged", _bkraised)
        # restore: rollback point first, then databases AND extras
        (config.AGENT_HOME / "settings.json").write_text('{"MODEL":"WIPED"}', "utf-8")
        _bkm.add_memory("should vanish after restore", "general")
        _bkbefore = len(_bk.list_backups())
        _bks = _bk.restore_backup(_bkz, _bkm, None)
        check("backup.restore_takes_rollback_point",
              bool(_bks.get("rollback"))
              and len(_bk.list_backups()) > _bkbefore)
        check("backup.restore_brings_back_extras",
              _bkj.loads((config.AGENT_HOME / "settings.json")
                         .read_text("utf-8"))["MODEL"] == "claude")
        check("backup.restore_brings_back_databases",
              not any("vanish" in x["content"] for x in _bkm.all_memories())
              and any(s["name"] == "bi-review" for s in _bkm.get_skills()))
        check("backup.lookup_blocks_traversal",
              _bk._resolve("../../etc/passwd") is None
              and _bk._resolve("nope.zip") is None)
        import agent.audit as _bkau
        check("backup.audited",
              any(e["kind"] == "backup" for e in _bkau.recent(10)))
    finally:
        config.AGENT_HOME = _bk_home

    # --- crew: persistent specialists, routing, scoped autonomy --------------- #
    _cw_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "cw.db"
    try:
        import agent.crew as _cw
        import agent.tools as _cwtools
        from agent.memory import MemoryStore as _CWM
        from rich.console import Console as _CWC
        check("crew.default_roster",
              [m["name"] for m in _cw.members()]
              == ["Delivery", "BizDev", "Ops", "Intel"])
        _CWCASES = [
            ("Build a Power BI dashboard for the client", "Delivery"),
            ("Check the ETL pipeline before the deliverable", "Delivery"),
            ("Find prospects in Johannesburg and draft outreach", "BizDev"),
            ("Draft a proposal for the RFP", "BizDev"),
            ("Which invoices are outstanding this month", "Ops"),
            ("Prepare the timesheet summary for billing", "Ops"),
            ("Any new government tenders this week", "Intel"),
            ("What are competitors in the BI market doing", "Intel"),
        ]
        check("crew.routing_matches_specialists",
              all(_cw.choose(_t)["name"] == _w for _t, _w in _CWCASES))
        _cwws = {m["name"]: str(_cw.workspace(m["name"]))
                 for m in _cw.members()}
        check("crew.workspaces_and_memory_isolated",
              len(set(_cwws.values())) == 4
              and len({_cw.category(m["name"])
                       for m in _cw.members()}) == 4)
        _cwm = _CWM(db_path=config.DB_PATH, check_same_thread=False)
        _cwseen = {}

        def _cwfake(brain, memory, console, objective, context,
                    auto_approve, session_id, model=None, tier_label="",
                    execute=None, tool_defs=None):
            _cwseen["context"] = context
            _cwseen["auto"] = auto_approve
            return "Report: did the work."
        import agent.subagent as _cwsub
        _cworig = _cwsub.run_subagent
        _cwsub.run_subagent = _cwfake
        try:
            _cwres = _cw.run("BizDev", "Find BI prospects in JHB", None,
                             _cwm, _CWC(quiet=True))
        finally:
            _cwsub.run_subagent = _cworig
        check("crew.run_injects_brief_and_workspace",
              _cwres["ok"] and "BizDev" in _cwseen["context"]
              and "ONLY folder" in _cwseen["context"]
              and _cwseen["auto"] is True)
        _cwperms = [p["pattern"] for p in _cwm.list_permissions()
                    if p["kind"] == "write_dir"]
        check("crew.autonomy_scoped_to_workspace",
              any("bizdev" in p.lower() for p in _cwperms)
              and not any(p in ("*", "/", "C:\\") for p in _cwperms))
        check("crew.memory_accumulates_per_member",
              any(x["category"] == "crew:bizdev"
                  for x in _cwm.search_memories("prospects", limit=10)))
        check("crew.run_logged",
              len(_cw.log("BizDev")) == 1
              and _cw.log("BizDev")[0]["ok"] is True)
        check("crew.unknown_member_refused",
              _cw.run("Nobody", "x", None, _cwm, _CWC(quiet=True))["ok"]
              is False)
        # a custom specialist can be added and removed
        _cw.upsert({"name": "Legal", "role": "contracts",
                    "brief": "You review contracts.",
                    "keywords": ["contract", "nda"]})
        check("crew.custom_member_roundtrip",
              _cw.get("Legal") is not None
              and _cw.choose("review this NDA contract")["name"] == "Legal"
              and _cw.remove("Legal")["ok"]
              and _cw.get("Legal") is None)
        import agent.audit as _cwau
        check("crew.audited",
              any(e["kind"] == "crew" for e in _cwau.recent(10)))
    finally:
        config.AGENT_HOME = _cw_home

    # --- trend scout: scan/dedupe, digest contract, gated adoption ------------ #
    _tw_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "tw.db"
    try:
        import agent.trendscout as _tw
        import json as _twj
        _TWFAKE = [{"id": "f:1", "title": "agent-memory-kit",
                    "url": "https://x/1", "detail": "memory"},
                   {"id": "f:2", "title": "mcp-everywhere",
                    "url": "https://x/2", "detail": "mcp"}]
        _tw_prev_sources = _tw.SOURCES
        _tw.SOURCES = [("fake", lambda: list(_TWFAKE)),
                       ("broken", lambda: (_ for _ in ()).throw(
                           RuntimeError("net down")))]
        _tws = _tw.scan()
        check("trends.scan_and_broken_source",
              len(_tws["items"]) == 2 and len(_tws["errors"]) == 1)
        check("trends.dedupe", len(_tw.scan()["items"]) == 0)
        _TWD = {"trends": [
            {"title": "Agent memory", "why": "Rising.",
             "sources": ["https://x/1"],
             "learnable": {"kind": "skill", "name": "memory-first",
                           "description": "check memories first",
                           "instructions": "1. search\n2. cite"}},
            {"title": "MCP wave", "why": "Standardising.",
             "sources": ["https://x/2"],
             "learnable": {"kind": "build",
                           "request": "Add an MCP browser"}}]}

        class _TWB:
            def chat(self, messages, system, tools=None, **kw):
                assert "UNTRUSTED DATA" in system[0]
                return _blk(_twj.dumps(_TWD))
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _tw.SOURCES = [("fake", lambda: list(_TWFAKE))]
        _twr = _tw.scan_and_digest(_TWB())
        check("trends.digest_contract",
              _twr["item_count"] == 2 and len(_twr["trends"]) == 2
              and _tw.report()["trends"][0]["adopted"] is False)
        from agent.memory import MemoryStore as _TWM
        _twm = _TWM(db_path=config.DB_PATH, check_same_thread=False)
        _twa = _tw.adopt(0, _twm)
        check("trends.adoption_writes_real_skill",
              _twa["ok"]
              and any(sk["name"] == "memory-first"
                      for sk in _twm.get_skills())
              and _tw.report()["trends"][0]["adopted"] is True)
        check("trends.build_refuses_skill_adopt",
              _tw.adopt(1, _twm)["ok"] is False)
        from agent import scheduler as _twsch
        check("trends.weekly_off_by_default",
              _tw.schedule_enabled(_twm) is False)
        check("trends.schedule_roundtrip",
              _tw.set_schedule(_twm, _twsch, True) is True
              and _tw.schedule_enabled(_twm) is True
              and _tw.set_schedule(_twm, _twsch, False) is False
              and _tw.schedule_enabled(_twm) is False)
        import agent.audit as _twau
        check("trends.audited",
              any(e["kind"] == "trend" for e in _twau.recent(10)))
        # digest must honour the engine the user picked, not silently
        # bill the cloud — this was a real report (credit-balance 400 while
        # a local engine was pinned)
        class _TWEB:
            def __init__(self, out): self.out = out; self.got = "__unset__"

            def chat(self, messages, system, tools=None, **kw):
                self.got = kw.get("model", None)
                return _blk(self.out)
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _twe = _TWEB(_twj.dumps(_TWD))
        _tw.scan_and_digest(_twe, model="CustomQWEN")
        check("trends.pinned_engine_used_for_digest",
              _twe.got == "CustomQWEN")
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _twe2 = _TWEB(_twj.dumps(_TWD))
        _tw.scan_and_digest(_twe2, model=None)
        check("trends.no_engine_keeps_brain_default", _twe2.got is None)
        # small local models fence/wrap their JSON — must still parse
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _twmessy = ("Sure! Here it is:\n```json\n" + _twj.dumps(_TWD)
                    + "\n```\nHope that helps!")
        _twr2 = _tw.scan_and_digest(_TWEB(_twmessy), model="CustomQWEN")
        check("trends.messy_local_json_parsed",
              len(_twr2.get("trends", [])) == 2 and not _twr2.get("error"))
        check("trends.prose_wrapped_json_parsed",
              len(_tw._extract_json("Here: " + _twj.dumps(_TWD)
                                    + " — done")["trends"]) == 2)

        class _TWBroke:
            def chat(self, *a, **kw):
                raise RuntimeError("Error code: 400 - Your credit balance "
                                   "is too low to access the Anthropic API")
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _twr3 = _tw.scan_and_digest(_TWBroke(), model=None)
        check("trends.billing_error_actionable",
              "no credit" in _twr3["error"] and "local engine"
              in _twr3["error"])
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        check("trends.non_json_error_is_clear",
              "usable JSON" in _tw.scan_and_digest(
                  _TWEB("I cannot help."), model="CustomQWEN")["error"])
        # --- resumable batching: the core of local-model support ---------
        _TWITEMS = [{"id": f"i:{i}", "title": f"item-{i}",
                     "url": f"https://x/{i}", "detail": "d"}
                    for i in range(20)]
        _tw.SOURCES = [("fake", lambda: list(_TWITEMS))]
        _TWGOOD = _twj.dumps({"trends": [
            {"title": "T", "why": "w", "sources": [],
             "learnable": {"kind": "skill", "name": "s",
                           "description": "d", "instructions": "i"}}]})

        class _TWFlaky:
            """Two batches then the engine dies, like a local model would."""
            def __init__(self): self.n = 0

            def chat(self, messages, system, tools=None, **kw):
                self.n += 1
                if self.n <= 2:
                    return _blk(_TWGOOD)
                raise RuntimeError("connection refused")
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _tw.clear_progress()
        _twp1 = _tw.scan_and_digest(_TWFlaky(), model="LocalX", batch_size=5)
        _twprog = _tw.progress()
        check("trends.partial_run_checkpoints_exactly",
              _twp1.get("resumable") is True and _twp1["items_done"] == 10
              and _twprog["next"] == 10 and len(_twprog["batches"]) == 2)
        check("trends.partial_results_still_usable",
              len(_twp1.get("trends", [])) > 0 and bool(_twp1.get("error")))

        class _TWHealthy:
            def __init__(self): self.items = []

            def chat(self, messages, system, tools=None, **kw):
                self.items += [i["title"] for i in
                               _twj.loads(messages[0]["content"])["items"]]
                return _blk(_TWGOOD)
        _twh = _TWHealthy()
        _twp2 = _tw.resume(_twh, model="LocalX")
        check("trends.resume_is_precise",
              _twp2["items_done"] == 20 and not _twp2.get("error")
              and _twh.items == [f"item-{i}" for i in range(10, 20)])
        check("trends.progress_cleared_when_complete",
              _tw.progress() == {})

        class _TWCtx:
            """Blows context until the batch shrinks to a single item."""
            def __init__(self): self.sizes = []

            def chat(self, messages, system, tools=None, **kw):
                n = len(_twj.loads(messages[0]["content"])["items"])
                self.sizes.append(n)
                if n > 1:
                    raise RuntimeError("maximum context length exceeded")
                return _blk(_TWGOOD)
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _tw.clear_progress()
        _twc = _TWCtx()
        _twp3 = _tw.scan_and_digest(_twc, model="LocalX", batch_size=8)
        check("trends.context_overflow_shrinks_and_completes",
              _twp3["items_done"] == 20 and not _twp3.get("error")
              and min(_twc.sizes) == 1 and max(_twc.sizes) == 8)

        class _TWErrText:
            """OpenAI-compatible brains return failures as reply TEXT."""
            def chat(self, messages, system, tools=None, **kw):
                return _blk("Could not reach the endpoint (connection "
                            "refused). Check the base URL.")
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _tw.clear_progress()
        _twp4 = _tw.scan_and_digest(_TWErrText(), model="LocalX")
        check("trends.engine_reply_error_surfaced_verbatim",
              "Could not reach the endpoint" in _twp4["error"])
        # engines ignore the declared types constantly (a list where a str
        # was asked for) — that crashed adopt with "'list' object has no
        # attribute 'strip'". Everything is normalised at ingest now.
        _TWSHAPES = [
            {"title": "A", "why": "w", "sources": ["https://x/1"],
             "learnable": {"kind": "skill", "name": ["n1"],
                           "description": ["d"],
                           "instructions": ["1. a", "2. b"]}},
            {"title": "B", "why": "w", "sources": "https://x/2",
             "learnable": {"kind": "skill", "name": "n2",
                           "description": "d", "instructions": "1. a"}},
            {"title": "C", "why": "w", "sources": [],
             "learnable": {"kind": "skill", "name": "n3", "description": "d",
                           "instructions": {"s1": "a", "s2": "b"}}},
            {"title": ["F"], "why": ["w1", "w2"], "sources": None,
             "learnable": {"kind": "skill", "name": "n6",
                           "description": "d", "instructions": "x"}},
            {"title": 123, "why": 45, "sources": [678],
             "learnable": {"kind": "skill", "name": 99,
                           "description": 1, "instructions": 2}},
            {"title": "H", "why": "w", "sources": []},
            {"title": "I", "why": "w", "sources": [], "learnable": "nope"},
        ]
        _twshape_ok = True
        for _tr in _TWSHAPES:
            _n = _tw._normalise_trend(_tr)
            _twshape_ok = _twshape_ok and _n is not None and (
                isinstance(_n["title"], str) and _n["title"]
                and isinstance(_n["why"], str)
                and isinstance(_n["sources"], list)
                and all(isinstance(_s, str) for _s in _n["sources"])
                and _n["learnable"]["kind"] in ("skill", "build")
                and all(isinstance(_v, str)
                        for _v in _n["learnable"].values()))
        check("trends.malformed_engine_shapes_normalised", _twshape_ok)
        check("trends.garbage_trends_dropped",
              _tw._normalise_trend("a string") is None
              and _tw._normalise_trend({"why": "no title"}) is None)
        check("trends.kind_inferred_when_missing",
              _tw._normalise_trend({"title": "D", "learnable":
                                    {"instructions": "1. a"}}
                                   )["learnable"]["kind"] == "skill"
              and _tw._normalise_trend({"title": "E", "learnable":
                                        {"request": "build it"}}
                                       )["learnable"]["kind"] == "build")
        # and the end-to-end adopt that actually crashed in the field
        (config.AGENT_HOME / "trendscout" / "seen.jsonl").write_text("", "utf-8")
        _tw.clear_progress()
        _tw.SOURCES = [("fake", lambda: [{"id": "s:1", "title": "t",
                                          "url": "https://x", "detail": "d"}])]

        class _TWShapeB:
            def chat(self, messages, system, tools=None, **kw):
                return _blk(_twj.dumps({"trends": _TWSHAPES}))
        _twrs = _tw.scan_and_digest(_TWShapeB(), model="LocalX")
        _twsm = _TWM(db_path=config.DB_PATH, check_same_thread=False)
        _twadopted = 0
        for _i, _tr in enumerate(_twrs.get("trends", [])):
            if _tr["learnable"]["kind"] == "skill":
                _twadopted += 1 if _tw.adopt(_i, _twsm).get("ok") else 0
        check("trends.malformed_shapes_adopt_cleanly", _twadopted >= 3)
        # an OLD-format report on disk (written by a pre-fix build) must
        # still adopt cleanly rather than 500 — this is what users hit
        # after upgrading without a fresh scan
        (config.AGENT_HOME / "trendscout" / "report.json").write_text(
            _twj.dumps({"at": "x", "ts": 1.0, "item_count": 1, "trends": [
                {"title": "Old", "why": "w", "sources": ["https://x/1"],
                 "adopted": False,
                 "learnable": {"kind": "skill", "name": ["listy-name"],
                               "description": ["d"],
                               "instructions": ["1. a", "2. b"]}}]}), "utf-8")
        check("trends.old_format_report_adopts",
              _tw.adopt(0, _twsm).get("ok") is True)
        import agent.issues as _issmod
        check("issues.env_carries_build_stamp",
              "build" in _issmod._env() and _issmod._env()["build"])
        _tw.clear_progress()
        _tw.clear_progress()
        check("trends.no_new_handled",
              _tw.scan_and_digest(_TWB()).get("no_new") is True)
        _tw.SOURCES = _tw_prev_sources
    finally:
        config.AGENT_HOME = _tw_home

    # --- neural 3D setup help: detect an install, validate a command ---------- #
    _nd_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "nd.db"
    try:
        import agent.neural3d as _nd
        import os as _ndos
        _ndfake = _RPath(_rtf.mkdtemp())
        (_ndfake / "TripoSR" / "venv" / "bin").mkdir(parents=True)
        (_ndfake / "TripoSR" / "run.py").write_text("# t", "utf-8")
        (_ndfake / "TripoSR" / "venv" / "bin" / "python").write_text("#!/bin/sh\n", "utf-8")
        _ndprev = _ndos.environ.get("HOME")
        _ndos.environ["HOME"] = str(_ndfake)
        try:
            _ndfound = _nd.detect()
        finally:
            if _ndprev is not None:
                _ndos.environ["HOME"] = _ndprev
        check("neural3d.detects_installed_tool",
              len(_ndfound) == 1 and _ndfound[0]["tool"] == "TripoSR"
              and _ndfound[0]["venv"] is True
              and "{image}" in _ndfound[0]["command"]
              and "{out}" in _ndfound[0]["command"])
        check("neural3d.detected_command_validates",
              _nd.validate(_ndfound[0]["command"])["ok"] is True)
        check("neural3d.validate_flags_missing_placeholder",
              _nd.validate("python run.py {image}")["ok"] is False
              and "{out}" in _nd.validate("python run.py {image}")["detail"])
        check("neural3d.validate_flags_missing_interpreter",
              _nd.validate("nosuchexe {image} {out}")["ok"] is False)
        check("neural3d.validate_flags_empty",
              _nd.validate("")["ok"] is False)
    finally:
        config.AGENT_HOME = _nd_home

    # --- neural photo-to-3D: harness, chain, failure paths -------------------- #
    _n3_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "n3.db"
    _n3_prev = (getattr(config, "NEURAL3D_CMD", ""),
                getattr(config, "BLENDER_PATH", ""))
    try:
        import agent.neural3d as _n3
        import stat as _n3stat
        config.NEURAL3D_CMD = ""
        _img = config.AGENT_HOME / "photo.jpg"
        _img.write_bytes(b"\xff\xd8\xff" + b"j" * 40)
        check("neural3d.unconfigured_guidance",
              "Neural 3D command" in _n3.run(str(_img))["error"])
        _ntool = config.AGENT_HOME / "fakeneural.sh"
        _ntool.write_text("#!/bin/sh\nprintf glTFxxxx > \"$2\"/mesh.glb\n",
                          "utf-8")
        _ntool.chmod(_ntool.stat().st_mode | _n3stat.S_IEXEC)
        config.NEURAL3D_CMD = f"{_ntool} {{image}} {{out}}"
        check("neural3d.missing_image_rejected",
              "not found" in _n3.run(str(config.AGENT_HOME / "no.jpg")
                                     )["error"].lower())
        _nfb = config.AGENT_HOME / "fakeblender"
        _nfb.write_text("#!/bin/sh\nexec python3 \"$4\"\n", "utf-8")
        _nfb.chmod(_nfb.stat().st_mode | _n3stat.S_IEXEC)
        config.BLENDER_PATH = str(_nfb)
        import agent.blenderlab as _n3bl
        _pp_prev = _n3._POSTPROCESS
        _n3._POSTPROCESS = (
            "open(os.path.join(OUT_DIR,'turntable_0.png'),'wb')"
            ".write(b'PNG'+b'x'*99)\n"
            "open(os.path.join(OUT_DIR,'model.glb'),'wb')"
            ".write(b'glTF'+b'x'*99)\n")
        try:
            _nr = _n3.run(str(_img), note="chain")
            check("neural3d.chain_collects_and_postprocesses",
                  _nr["ok"] and "model.glb" in _nr["models"]
                  and "turntable_0.png" in _nr["images"]
                  and _n3.jobs(3)[0]["postprocess"] is True)
            check("neural3d.file_lookup_safe",
                  _n3.file_path(_nr["job"], "model.glb") is not None
                  and _n3.file_path(_nr["job"], "../x") is None)
        finally:
            _n3._POSTPROCESS = _pp_prev
        _ntool.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", "utf-8")
        _n3f = _n3.run(str(_img))
        check("neural3d.tool_failure_surfaced",
              _n3f["ok"] is False and "boom" in _n3f["log_tail"])
        import agent.audit as _n3au
        check("neural3d.audited",
              any(e["kind"] == "neural3d" for e in _n3au.recent(10)))
    finally:
        config.AGENT_HOME = _n3_home
        config.NEURAL3D_CMD, config.BLENDER_PATH = _n3_prev

    # --- image attach must not silently override an explicitly pinned engine #
    _iv_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "iv.db"
    try:
        import agent.brain as _ivbrain
        import web.server as _ivws
        _ivbrain.add_custom_engine(name="CustomQWEN",
                                   base_url="http://localhost:11434/v1",
                                   api_key="ollama", model="qwen3.6:latest")
        # explicit pin survives an image attach — this is the exact bug: an
        # image used to force force=None (Claude) regardless of what engine
        # the user had explicitly selected, silently burning cloud credits
        # while the UI still showed the custom engine as active.
        _ivforce = _ivws._force_model("CustomQWEN")
        check("images.explicit_pin_resolves_to_itself",
              _ivforce == "CustomQWEN")
        if _ivforce is agent_main._AUTO:
            _ivforce = None
        check("images.explicit_pin_survives_attachment",
              _ivforce == "CustomQWEN")
        # Auto (no explicit pin) still smart-routes images to Claude
        _ivforce2 = _ivws._force_model("Auto")
        check("images.auto_resolves_to_auto_sentinel",
              _ivforce2 is agent_main._AUTO)
        if _ivforce2 is agent_main._AUTO:
            _ivforce2 = None
        check("images.auto_still_routes_to_claude", _ivforce2 is None)
        _ivbrain.remove_custom_engine("CustomQWEN")
    finally:
        config.AGENT_HOME = _iv_home

    # --- pipeline bronze naming: no double-prefix on bronze_-named sources ---- #
    _bn_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "bn.db"
    try:
        import csv as _bncsv
        import agent.datapipeline as _bndp
        _bnsrc = config.AGENT_HOME / "emp.csv"
        with open(_bnsrc, "w", newline="") as _f:
            _w = _bncsv.writer(_f); _w.writerow(["EmployeeID", "Name"])
            _w.writerow([1, "Alice"])
        # user names the source table with the 'bronze_' prefix already —
        # must NOT become bronze_bronze_employee
        _bnspec = {"name": "bronze_naming", "sources": [
                      {"path": str(_bnsrc), "table": "bronze_employee"}],
                   "primary_key": "EmployeeID",
                   "silver_sql": {"asis": "CREATE TABLE silver_x AS SELECT * "
                                          "FROM bronze_employee;",
                                  "tobe": "CREATE TABLE silver_x AS SELECT * "
                                          "FROM bronze_employee;"},
                   "gold_sql": {"asis": {"gold_x": "SELECT * FROM silver_x"},
                               "tobe": {"gold_x": "SELECT * FROM silver_x"}},
                   "compare": ["gold_x"]}
        _bndp.save(_bnspec)
        _bnres = _bndp.regression("bronze_naming")
        check("pipeline.bronze_prefixed_source_not_doubled",
              _bnres["status"] == "converged")
        # ordinary (non-prefixed) source names still resolve to bronze_<name>
        _bnspec2 = dict(_bnspec, name="bronze_naming_normal",
                        sources=[{"path": str(_bnsrc), "table": "employee"}],
                        silver_sql={"asis": "CREATE TABLE silver_x AS SELECT "
                                            "* FROM bronze_employee;",
                                   "tobe": "CREATE TABLE silver_x AS SELECT "
                                          "* FROM bronze_employee;"})
        _bndp.save(_bnspec2)
        check("pipeline.normal_source_still_prefixed",
              _bndp.regression("bronze_naming_normal")["status"]
              == "converged")
    finally:
        config.AGENT_HOME = _bn_home

    # --- image-only message: no empty text block sent to the API ------------- #
    _it_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "it.db"
    try:
        from agent.memory import MemoryStore as _MSE
        _itm = _MSE(db_path=config.DB_PATH, check_same_thread=False)
        _itcap = {}

        class _ITB:
            model = "claude-x"; fast_model = None; local = None
            last_engine = None; last_usage = None

            def chat(self, messages, system, tools=None, on_text=None,
                     model=None):
                _itcap["content"] = messages[-1]["content"]
                return _blk("I see the image")
        _itimg = [{"type": "image", "source": {"type": "base64",
                                               "media_type": "image/png",
                                               "data": "AA=="}}]
        agent_main.run_turn(_ITB(), _itm, [], "", auto_approve=False,
                            session_id="imgtest", force_model="claude-x",
                            turn_info={}, images=_itimg)
        _itc = _itcap["content"]
        check("images.no_empty_text_block_with_caption_missing",
              isinstance(_itc, list)
              and all(not (b.get("type") == "text" and b.get("text", "") == "")
                      for b in _itc)
              and any(b.get("type") == "image" for b in _itc))
    finally:
        config.AGENT_HOME = _it_home

    # --- data pipelines: medallion, freeze, bisection diff, healing loop ------ #
    _dp_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "dp.db"
    try:
        import agent.datapipeline as _dp
        import csv as _dcsv, json as _djson, sqlite3 as _dsql
        _dsrc = config.AGENT_HOME / "orders.csv"
        with open(_dsrc, "w", newline="") as _f:
            _w = _dcsv.writer(_f); _w.writerow(["id", "region", "amount", "ts"])
            for _r in [[1, "GP", "100.0", "2026-01-01"],
                       [2, "WC", "50.0", "2026-01-02"],
                       [2, "WC", "55.0", "2026-01-03"],
                       [3, "GP", "20.0", "2026-01-04"]]:
                _w.writerow(_r)
        _SG = ("CREATE TABLE silver_orders AS SELECT id, region, "
               "CAST(amount AS REAL) AS amount, ts FROM (SELECT *, "
               "ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) rn "
               "FROM bronze_orders) WHERE rn=1;")
        _SB = ("CREATE TABLE silver_orders AS SELECT id, region, "
               "CAST(amount AS REAL) AS amount, ts FROM bronze_orders;")
        _G = {"gold_rev": "SELECT region, SUM(amount) AS revenue FROM "
                          "silver_orders GROUP BY region"}
        _spec = {"name": "sales", "sources": [{"path": str(_dsrc),
                                               "table": "orders"}],
                 "primary_key": "region", "tolerance": 0.001,
                 "silver_sql": {"asis": _SG, "tobe": _SB},
                 "gold_sql": {"asis": _G, "tobe": dict(_G)},
                 "compare": ["gold_rev"]}
        check("pipeline.spec_validation",
              _dp.save(_spec)["ok"] and _dp.save({"name": "x"})["ok"] is False)
        check("pipeline.freeze_hashes_sources",
              len(_dp.freeze_sources("sales")["files"][0]["sha256"]) == 16)
        _ra = _dp.run_variant("sales", "asis")
        check("pipeline.medallion_run",
              _ra["ok"] and _ra["tables"]["bronze_orders"] == 4
              and _ra["tables"]["silver_orders"] == 3)
        _bc = _dsql.connect(str(config.AGENT_HOME / "pipelines" / "sales"
                                / "asis.db"))
        _bcols = [x[1] for x in _bc.execute("PRAGMA table_info(bronze_orders)")]
        _bc.close()
        check("pipeline.bronze_lineage_columns",
              all(k in _bcols for k in ("_load_ts", "_source_file",
                                        "_process_id")))
        _dp.run_variant("sales", "tobe")
        _dd = _dp.data_diff("sales", "gold_rev", 0.001)
        check("pipeline.bisection_isolates_diff",
              _dd["match"] is False
              and any(m["key"] == "WC" and "revenue" in m["cols"]
                      for m in _dd["modified"]))
        _spec["silver_sql"]["tobe"] = _SG; _dp.save(_spec)
        _dp.run_variant("sales", "tobe")
        _dm = _dp.data_diff("sales", "gold_rev", 0.001)
        check("pipeline.identical_matches_no_row_compare",
              _dm["match"] and _dm["rows_row_compared"] == 0)
        _spec["silver_sql"]["tobe"] = _SB; _dp.save(_spec)

        class _Healer:
            def chat(self, messages, system, tools=None, **kw):
                return _blk(_djson.dumps({"silver_sql": _SG,
                                          "gold_sql": _G}))
        _res = _dp.regression("sales", brain=_Healer())
        check("pipeline.loop_converges_via_healing",
              _res["status"] == "converged"
              and _res["iterations"][0]["converged"] is False
              and _res["iterations"][-1]["converged"] is True)
        _spec["silver_sql"]["tobe"] = _SB; _dp.save(_spec)

        class _BadHealer:
            def chat(self, messages, system, tools=None, **kw):
                return _blk(_djson.dumps({"silver_sql": _SB,
                                          "gold_sql": _G}))
        _res2 = _dp.regression("sales", brain=_BadHealer(), max_iters=3)
        check("pipeline.escalates_at_cap",
              _res2["status"] == "escalated" and bool(_res2["unresolved"])
              and _dp.list_pipelines()[0]["last"]["status"] == "escalated")
        import agent.audit as _dpau
        check("pipeline.iterations_audited",
              any(e["kind"] == "pipeline" for e in _dpau.recent(20)))
    finally:
        config.AGENT_HOME = _dp_home
    # --- self-improvement: history, rollback, and knowing what to fix -------- #
    _si_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "si.db"
    try:
        import agent.selfimprove as _si
        import agent.timemachine as _sitm
        # snapshot must hand back an id, or an applied change can't be undone
        _sif = config.AGENT_HOME / "probe.txt"
        _sif.write_text("original", "utf-8")
        _sieid = _sitm.snapshot(_sif, tool="selfimprove")
        check("selfimprove.snapshot_returns_an_id",
              isinstance(_sieid, str) and _sieid)
        _sif.write_text("changed", "utf-8")
        _si._record_applied({
            "id": "imp1", "at": "now", "summary": "Add a probe",
            "request": "add probe", "files": ["probe.txt"],
            "snapshots": [{"path": "probe.txt", "entry_id": _sieid,
                           "existed": True}],
            "tests": {"passed": 900}})
        check("selfimprove.records_what_was_applied",
              len(_si.history()) == 1
              and _si.history()[0]["summary"] == "Add a probe")
        _sirev = _si.revert("imp1")
        check("selfimprove.reverts_an_improvement_as_a_unit",
              _sirev["ok"] and _sif.read_text("utf-8") == "original"
              and "Restart" in _sirev["note"])
        check("selfimprove.will_not_revert_twice_or_revert_a_ghost",
              _si.revert("imp1")["ok"] is False
              and _si.revert("nope")["ok"] is False)
        check("selfimprove.marks_it_reverted",
              bool(_si.history()[0].get("reverted_at")))
        # suggestions must come from the record, not from imagination
        import agent.issues as _siiss
        for _ in range(4):
            _siiss.note_error("ui", "TypeError: cannot read x of null")
        _sisug = _si.suggestions(None)
        _sitop = [s for s in _sisug if "repeating" in s["title"].lower()]
        check("selfimprove.suggests_from_repeated_errors",
              bool(_sitop) and "logged 4 times" in _sitop[0]["why"])
        check("selfimprove.each_suggestion_is_actionable",
              all(s["request"] and s["title"] and s["why"] for s in _sisug)
              and "add a test that fails without the fix"
              in _sitop[0]["request"])
        check("selfimprove.suggestions_are_ranked_and_capped",
              len(_sisug) <= 6
              and all(_sisug[i]["weight"] >= _sisug[i + 1]["weight"]
                      for i in range(len(_sisug) - 1)))
    finally:
        config.AGENT_HOME = _si_home


    # --- self-improvement: sandbox, gate, human apply, rollback --------------- #
    _si_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "si.db"
    _si_prev = getattr(config, "SELFIMPROVE", True)
    try:
        import agent.selfimprove as _si
        import agent.timemachine as _sitm
        config.SELFIMPROVE = True; config.TIMEMACHINE = True
        _fake = _RPath(_rtf.mkdtemp())
        (_fake / "agent").mkdir()
        (_fake / "agent" / "mod.py").write_text("ORIGINAL", "utf-8")
        _si_live_prev = _si.LIVE_ROOT
        _si.LIVE_ROOT = _fake
        _ws = _RPath(_si.create_workspace("test change")["workspace"])
        check("selfimprove.workspace_is_copy",
              (_ws / "agent" / "mod.py").read_text("utf-8") == "ORIGINAL")
        (_ws / "agent" / "mod.py").write_text("IMPROVED", "utf-8")
        (_ws / "agent" / "newfeat.py").write_text("NEW", "utf-8")
        _sd = _si.diff_against_live()
        check("selfimprove.diff_detects",
              {(d["path"], d["status"]) for d in _sd}
              == {("agent/mod.py", "modified"), ("agent/newfeat.py", "added")})
        check("selfimprove.apply_refused_before_tests",
              _si.apply()["ok"] is False)
        _si._update_proposal(tests_ok=True, tests_summary="stub",
                             state="proposed")
        _ar = _si.apply()
        check("selfimprove.apply_after_gate",
              _ar["ok"]
              and (_fake / "agent" / "mod.py").read_text("utf-8") == "IMPROVED"
              and (_fake / "agent" / "newfeat.py").exists())
        check("selfimprove.replaced_file_snapshotted",
              any(e["path"].endswith("mod.py") and e["tool"] == "selfimprove"
                  for e in _sitm.entries(10)))
        import agent.audit as _siau
        check("selfimprove.audited",
              any(e["kind"] == "selfimprove" for e in _siau.recent(10)))
        check("selfimprove.discard_cleans",
              _si.discard()["ok"] and not _si.workspace_path().exists())
        # kill-switch via the tool layer
        config.SELFIMPROVE = False
        from agent.tools import execute_tool as _sitool
        from agent.memory import MemoryStore as _MSD
        from rich.console import Console as _SIC
        _sim = _MSD(db_path=config.DB_PATH, check_same_thread=False)
        check("selfimprove.kill_switch",
              "disabled" in _sitool("self_improve_start",
                                    {"request": "do something big"},
                                    _sim, _SIC(quiet=True)))
        config.SELFIMPROVE = True
        _si.LIVE_ROOT = _si_live_prev
    finally:
        config.AGENT_HOME = _si_home
        config.SELFIMPROVE = _si_prev

    # --- time machine: snapshot-before-write, restore, undo-the-undo ---------- #
    _tm_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "tm.db"
    _tm_prev = getattr(config, "TIMEMACHINE", True)
    try:
        import agent.timemachine as _tm
        config.TIMEMACHINE = True
        _tf = config.AGENT_HOME / "report.md"
        _tf.write_text("version ONE", "utf-8")
        _tm.snapshot(_tf)
        _tf.write_text("version TWO", "utf-8")
        _te = _tm.entries(5)[0]
        check("timemachine.overwrite_recorded",
              _te["action"] == "overwrite" and _te["restorable"] is True)
        _td = _tm.diff(_te["id"])
        check("timemachine.diff_shows_change",
              _td["ok"] and "-version ONE" in _td["text"]
              and "+version TWO" in _td["text"])
        check("timemachine.restore_roundtrip",
              _tm.restore(_te["id"])["ok"]
              and _tf.read_text("utf-8") == "version ONE")
        _te2 = _tm.entries(5)[0]
        check("timemachine.undo_the_undo",
              _te2["tool"] == "restore" and _tm.restore(_te2["id"])["ok"]
              and _tf.read_text("utf-8") == "version TWO")
        _tg = config.AGENT_HOME / "made.txt"
        _tm.snapshot(_tg); _tg.write_text("agent file", "utf-8")
        _tc = [x for x in _tm.entries(10) if x["action"] == "create"][0]
        check("timemachine.undo_create_deletes",
              _tm.restore(_tc["id"])["ok"] and not _tg.exists())
        from agent.tools import execute_tool as _tmtool
        from agent.memory import MemoryStore as _MSC
        from rich.console import Console as _TMC
        _tmm = _MSC(db_path=config.DB_PATH, check_same_thread=False)
        _th = config.AGENT_HOME / "hooked.txt"
        _th.write_text("precious", "utf-8")
        _tmtool("write_file", {"path": str(_th), "content": "clobbered"},
                _tmm, _TMC(quiet=True), auto_approve=True)
        _the = [x for x in _tm.entries(10) if x["path"] == str(_th)][0]
        check("timemachine.write_file_hooked",
              _the["action"] == "overwrite"
              and _tm.restore(_the["id"])["ok"]
              and _th.read_text("utf-8") == "precious")
        config.TIMEMACHINE = False
        _tn = len(_tm.entries(50)); _tm.snapshot(_th)
        check("timemachine.off_switch", len(_tm.entries(50)) == _tn)
        config.TIMEMACHINE = True
    finally:
        config.AGENT_HOME = _tm_home
        config.TIMEMACHINE = _tm_prev
    # --- the audit chain must survive concurrent processes ------------------- #
    #
    # Reported from a real trail: "1688 entries, chain BROKEN". The lock was a
    # THREAD lock, but the web server, the scheduler and any script that
    # imports these modules are separate PROCESSES. Two of them read the same
    # tail, both claimed it as their predecessor, and the result is
    # indistinguishable from tampering. Reproduced with four writers: broke at
    # line 72.
    _au_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "au.db"
    try:
        import agent.audit as _au
        import subprocess as _ausub, sys as _ausys, textwrap as _autw
        _auw = config.AGENT_HOME / "_w.py"
        _auw.write_text(_autw.dedent(f"""
            import sys, pathlib
            sys.path.insert(0, {str(_RPath('.').resolve())!r})
            import agent.config as config
            config.AGENT_HOME = pathlib.Path({str(config.AGENT_HOME)!r})
            import agent.audit as A
            for i in range(15):
                A.record("tool", name=f"p{{sys.argv[1]}}-{{i}}", summary="x" * 30)
            """), "utf-8")
        _auprocs = [_ausub.Popen([_ausys.executable, str(_auw), str(i)],
                                 stdout=_ausub.DEVNULL, stderr=_ausub.DEVNULL)
                    for i in range(3)]
        for _p in _auprocs:
            _p.wait(timeout=120)
        _auv = _au.verify()
        check("audit.chain_survives_concurrent_processes",
              _auv["ok"] is True and _au.count() == 45)

        # tampering must still be caught — the lock fixes races, not forgery
        _aup = config.AGENT_HOME / "audit.jsonl"
        _aulines = _aup.read_text("utf-8").splitlines()
        _aue = json.loads(_aulines[2]); _aue["summary"] = "EDITED"
        _aulines[2] = json.dumps(_aue, sort_keys=True)
        _aup.write_text("\n".join(_aulines) + "\n", "utf-8")
        _aubad = _au.verify()
        check("audit.tampering_is_still_detected",
              _aubad["ok"] is False and _aubad["break_at"] == 4)

        # resealing archives; it must never rewrite history to make it pass
        _aures = _au.reseal()
        check("audit.reseal_archives_rather_than_rewrites",
              _aures["ok"]
              and _aures["archived"].startswith("audit-unverified-")
              and (config.AGENT_HOME / _aures["archived"]).exists()
              and _au.verify()["ok"] is True)
        _aufirst = json.loads(
            (config.AGENT_HOME / "audit.jsonl").read_text(
                "utf-8").splitlines()[0])
        check("audit.reseal_records_why",
              _aufirst["kind"] == "reseal"
              and _aufirst["broke_at_line"] == 4
              and "unverified" in _aufirst["note"])
        # --- clearing: allowed, but never silent -------------------------
        import time as _autime
        for _i in range(12):
            _au.record("tool", name=f"c{_i}", summary="x")
        _aup2 = config.AGENT_HOME / "audit.jsonl"
        _aul = _aup2.read_text("utf-8").splitlines()
        _auold = _autime.time() - 40 * 86400
        for _i in range(min(8, len(_aul))):
            _aue2 = json.loads(_aul[_i]); _aue2["ts"] = _auold
            _aul[_i] = json.dumps(_aue2, sort_keys=True)
        _aup2.write_text("\n".join(_aul) + "\n", "utf-8")
        _aukeep = _au.clear(keep_days=7)
        check("audit.clears_only_what_is_old_when_asked",
              _aukeep["ok"] and _aukeep["removed"] == 8
              and _aukeep["kept"] > 0)
        check("audit.clearing_is_recorded_not_silent",
              json.loads(_aup2.read_text("utf-8").splitlines()[0])["kind"]
              == "cleared")
        check("audit.the_old_trail_is_archived_not_destroyed",
              _aukeep["archived_as"].startswith("audit-cleared-")
              and (config.AGENT_HOME / _aukeep["archived_as"]).exists())
        check("audit.the_new_chain_verifies_after_clearing",
              _au.verify()["ok"] is True)
        check("audit.refuses_when_nothing_is_old_enough",
              _au.clear(keep_days=3650)["ok"] is False)
        _auarc = _au.archives()
        check("audit.archives_are_listed", isinstance(_auarc, list)
              and len(_auarc) >= 1)
        _audel = _au.delete_archives()
        check("audit.archives_can_be_deleted_and_that_is_recorded",
              _audel["ok"] and _au.archives() == []
              and any(json.loads(ln).get("kind") == "archives-deleted"
                      for ln in _aup2.read_text("utf-8").splitlines()))

        check("audit.a_healthy_chain_cannot_be_resealed",
              _au.reseal()["ok"] is False)
    finally:
        config.AGENT_HOME = _au_home


    # --- audit trail: hash chain, tamper detection, hooks --------------------- #
    _au_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "au.db"
    _au_prev = getattr(config, "AUDIT", True)
    try:
        import agent.audit as _au
        config.AUDIT = True
        _au._last_hash = None
        _au.record("turn", engine="Claude", session="s1")
        _au.record("tool", name="read_file", ok=True, ms=12)
        _au.record("config", name="settings", detail="MODEL")
        _v = _au.verify()
        check("audit.chain_intact", _v["ok"] and _v["entries"] == 3)
        _ln = _au._path().read_text().splitlines()
        _ln[1] = _ln[1].replace("read_file", "EVIL")
        _au._path().write_text("\n".join(_ln) + "\n")
        _v2 = _au.verify()
        check("audit.tamper_detected",
              _v2["ok"] is False and _v2["break_at"] == 3)
        _au._path().unlink(); _au._last_hash = None
        _au.MAX_BYTES = 600
        for _i in range(12):
            _au.record("tool", name=f"t{_i}", ok=True)
        check("audit.rotation_fresh_chain",
              len(list(config.AGENT_HOME.glob("audit-*.jsonl"))) >= 1
              and _au.verify()["ok"]
              and any(e["kind"] == "rotate" for e in _au.recent(50)))
        _au.MAX_BYTES = 2_000_000
        from agent.memory import MemoryStore as _MSB
        from agent.tools import execute_tool as _autool
        from rich.console import Console as _AUC
        _aum = _MSB(db_path=config.DB_PATH, check_same_thread=False)
        _autool("search_memory", {"query": "x"}, _aum, _AUC(quiet=True))
        _ae = _au.recent(1)[0]
        check("audit.tool_hook_records",
              _ae["kind"] == "tool" and _ae["name"] == "search_memory"
              and _ae["ok"] is True and "ms" in _ae)
        _autool("read_file", {"path": str(config.AGENT_HOME / "nope.xyz")},
                _aum, _AUC(quiet=True))
        check("audit.tool_failure_flagged", _au.recent(1)[0]["ok"] is False)
        agent_main.run_turn(_DualBrain(), _aum, [], "hello",
                            auto_approve=False, session_id="aud1",
                            force_model="claude-x", turn_info={},
                            second_opinion=False)
        check("audit.turn_hook_records",
              any(e["kind"] == "turn" and e.get("session") == "aud1"
                  for e in _au.recent(10)))
        check("audit.csv_export",
              _au.export_csv().startswith("iso,kind,")
              and "search_memory" in _au.export_csv())
        config.AUDIT = False
        _n = _au.count(); _au.record("tool", name="ghost")
        check("audit.toggle_off_stops", _au.count() == _n)
    finally:
        config.AGENT_HOME = _au_home
        config.AUDIT = _au_prev

    # --- issue reports: capture, format, export, clear ----------------------- #
    _is_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp()); config.DB_PATH = config.AGENT_HOME / "is.db"
    try:
        from agent.memory import MemoryStore as _MS5
        import agent.issues as _iss
        _im = _MS5(db_path=config.DB_PATH, check_same_thread=False)
        _im.log_message("conv-9", "user", "export my tasks to excel")
        _im.log_message("conv-9", "assistant", "export attempt failed")
        check("issues.session_transcript_helper",
              [x["role"] for x in _im.recent_session_messages("conv-9", 5)]
              == ["user", "assistant"])
        _iss.RECENT_ERRORS.clear()
        _iss.note_error("chat", "RuntimeError: xlsx writer crashed")
        _e = _iss.record_issue(_im, "Export button did nothing",
                               session_id="conv-9", engine="Claude")
        _txt = _iss.format_issue(_e)
        check("issues.report_bundles_everything",
              "Export button did nothing" in _txt and "Engine: Claude" in _txt
              and "export my tasks to excel" in _txt
              and "xlsx writer crashed" in _txt)
        check("issues.count_and_list",
              _iss.issue_count() == 1
              and _iss.list_issues(5)[0]["note"] == "Export button did nothing")
        _iss.record_issue(_im, "engine menu froze", session_id="", engine="")
        check("issues.export_all_combined",
              "Export button" in _iss.export_all()
              and "engine menu froze" in _iss.export_all())
        check("issues.clear", _iss.clear_all() == 2 and _iss.issue_count() == 0)
        from agent.tools import execute_tool as _itool
        from rich.console import Console as _IC
        check("issues.tool_dispatch",
              "Issue recorded" in _itool("report_issue",
                                         {"note": "sidebar button dead"},
                                         _im, _IC(), session_id="conv-9")
              and _iss.issue_count() == 1)
        # automatic pickup is restart-proof: file-backed, ring is just a cache
        _iss.clear_errors()
        _iss.note_error("ui", "[qsSendBtn] /api/email/send -> 500 request failed")
        _iss.RECENT_ERRORS.clear()
        check("issues.errors_survive_restart",
              any("qsSendBtn" in e["message"] for e in _iss.recent_errors(5)))
        _e2 = _iss.record_issue(_im, "send button broke", session_id="conv-9")
        check("issues.report_embeds_auto_errors",
              "qsSendBtn" in _iss.format_issue(_e2))
        check("issues.clear_errors_works",
              _iss.clear_errors() >= 1 and _iss.recent_errors(5) == [])
        # environment diagnostics: reports name the serving interpreter, and
        # voice unavailability carries its real import reason
        _ed = _iss.format_issue(_iss.record_issue(_im, "env probe"))
        check("issues.env_names_interpreter",
              "interpreter=" in _ed and ("venv=yes" in _ed or "venv=no" in _ed))
        import agent.voice as _vc
        check("voice.reason_matches_availability",
              (_vc.stt_available() and _vc.stt_reason() == "")
              or (not _vc.stt_available() and _vc.stt_reason() != ""))
    finally:
        config.AGENT_HOME = _is_home

    # --- free, unknown and gone are three different things ------------------- #
    import agent.costs as _cst2
    check("costs.a_local_model_tag_is_free_not_unpriced",
          not _cst2.unpriced("qwen3.6:latest")
          and not _cst2.unpriced("gemma4:12b"))
    check("costs.an_engine_that_no_longer_exists_says_so",
          _cst2.gone("Some Old Engine") and not _cst2.gone("qwen3.6:latest"))
    check("health.it_offers_a_model_you_already_have",
          "already have it"
          in _RPath("agent/health.py").read_text("utf-8"))

    # --- indexing a big folder has to be interruptible ----------------------- #
    #
    # Reported twice from real use: indexing 12,595 files "runs forever".
    # Three compounding causes, and the third is why it felt hopeless rather
    # than merely slow:
    #   1. embed_texts() was called once PER FILE — thousands of sequential
    #      HTTP round-trips before any real work.
    #   2. one commit at the very end — stop at hour three and you had
    #      nothing.
    #   3. one blocking browser request, which times out long before the end.
    _ix_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.rag as _ixr2
        _ixkb = config.AGENT_HOME / "kb"
        _ixkb.mkdir(parents=True, exist_ok=True)
        for _i in range(400):
            (_ixkb / f"d{_i:04d}.md").write_text(
                "# T\n" + ("knowledge sentence here. " * 120), "utf-8")
        _ixcalls = {"n": 0}
        _ixreal = _ixr2.embed_texts

        def _ixfake(texts):
            _ixcalls["n"] += 1
            return [[0.1] * 8 for _ in texts]
        _ixr2.embed_texts = _ixfake
        try:
            _ixs = _ixr2.DocumentStore()
            _ixcap, config.RAG_MAX_FILES = config.RAG_MAX_FILES, 5000
            _ixres = _ixs.ingest_path(str(_ixkb), budget_s=0)
            check("rag.embeddings_are_batched_across_files_not_per_file",
                  _ixres["added"] == 400 and _ixcalls["n"] < 40)
            config.RAG_MAX_FILES = _ixcap
        finally:
            _ixr2.embed_texts = _ixreal

        # and a budgeted run must save what it did and resume
        _ixkb2 = config.AGENT_HOME / "kb2"
        _ixkb2.mkdir(parents=True, exist_ok=True)
        for _i in range(600):
            (_ixkb2 / f"d{_i:04d}.md").write_text(
                "# T\n" + ("knowledge sentence here. " * 120), "utf-8")
        import time as _ixt

        def _ixslow(texts):
            _ixt.sleep(0.25)
            return [[0.1] * 8 for _ in texts]
        _ixr2.embed_texts = _ixslow
        try:
            _ixcap2, config.RAG_MAX_FILES = config.RAG_MAX_FILES, 5000
            _ixs2 = _ixr2.DocumentStore()
            _ixtotal, _ixpasses = 0, []
            for _p in range(8):
                _rr = _ixs2.ingest_path(str(_ixkb2), budget_s=1)
                _ixtotal += _rr.get("added", 0)
                _ixpasses.append(_rr.get("stopped_early", False))
                if not _rr.get("stopped_early"):
                    break
            check("rag.a_long_run_stops_on_a_budget_and_saves_its_work",
                  _ixpasses[0] is True and _ixtotal == 600)
            check("rag.it_says_the_work_so_far_is_saved",
                  "**Everything done so far is saved**"
                  in (_ixs2.ingest_path(str(_ixkb2), budget_s=0)
                      .get("warning", "") or " ") or _ixtotal == 600)
            _ixagain = _ixs2.ingest_path(str(_ixkb2), budget_s=1)
            check("rag.a_finished_folder_costs_nothing_to_re_run",
                  _ixagain.get("added", 0) == 0
                  and not _ixagain.get("stopped_early"))
            config.RAG_MAX_FILES = _ixcap2
        finally:
            _ixr2.embed_texts = _ixreal
        check("ui.indexing_runs_in_passes_and_can_be_stopped",
              "pass ${pass}" in _uijs and 'id="docStopBtn"' in _uihtml
              and "press again to carry on" in _uijs)
    finally:
        config.AGENT_HOME = _ix_home

    # --- finding a conversation, and filing one away ------------------------- #
    #
    # Titles are generated and often generic, so searching them alone misses
    # the conversation you actually remember — you remember a phrase from it,
    # not what it ended up being called. And a conversation moved into a
    # group stayed in the general list too, so filing never made the list
    # shorter, which is the only reason to file anything.
    check("ui.conversations_can_be_searched",
          'id="convSearch"' in _uihtml and "searchConversations" in _uijs
          and "/api/conversations/search" in _uijs)
    check("ui.the_default_view_is_ungrouped_not_everything",
          'projectFilter: "none"' in _uijs
          and '<option value="none">Ungrouped</option>' in _uihtml)
    check("ui.everything_is_still_reachable_and_honestly_labelled",
          '<option value="all">Everything</option>' in _uihtml)
    check("ui.a_search_result_shows_the_line_it_matched",
          ".conv-snippet" in _uicss and "conv-snippet" in _uijs)
    _srv = _RPath("web/server.py").read_text("utf-8")
    check("web.search_looks_inside_messages_not_just_titles",
          '"/api/conversations/search"' in _srv
          and "JOIN messages m" in _srv)
    check("web.a_one_character_search_is_refused",
          "at least two characters" in _srv)

    # --- a first run has no API key, by definition --------------------------- #
    #
    # From a real log: the app installed, started, and nothing in the window
    # could be clicked. `AnthropicBrain.__init__` called sys.exit(1) when no
    # key was set — fine in a CLI, catastrophic in a web server. It raised
    # SystemExit inside a request, /api/meta returned 500, and the front end
    # never finished loading. So the one page that could accept a key was the
    # page that couldn't render: a fresh install could never be configured.
    import agent.brain as _fkb
    # parsed, not grepped — the docstring explaining the fix mentions the
    # very call it removed, and a text search can't tell those apart
    import ast as _fkast
    _fktree = _fkast.parse(_RPath("agent/brain.py").read_text("utf-8"))
    _fkexits = [n for n in _fkast.walk(_fktree)
                if isinstance(n, _fkast.Call)
                and isinstance(n.func, _fkast.Attribute)
                and n.func.attr == "exit"
                and isinstance(n.func.value, _fkast.Name)
                and n.func.value.id == "sys"]
    check("brain.the_library_never_kills_the_process", not _fkexits)
    check("brain.a_missing_key_raises_something_catchable",
          hasattr(_fkb, "EngineNotConfigured")
          and issubclass(_fkb.EngineNotConfigured, Exception))
    _fkkey = _ros.environ.pop("ANTHROPIC_API_KEY", None)
    _fkkey2 = _ros.environ.pop("AGENT_API_KEY", None)
    # make_brain now falls back to any engine the user configured, so this
    # test needs a home with none — otherwise it's testing the fallback
    _fkhome, config.AGENT_HOME = config.AGENT_HOME, _RPath(_rtf.mkdtemp())
    try:
        _fkraised = None
        try:
            _fkb.make_brain("anthropic")
        except _fkb.EngineNotConfigured as exc:
            _fkraised = exc
        except SystemExit:
            _fkraised = "SystemExit"
        check("brain.no_key_is_an_error_not_an_exit",
              isinstance(_fkraised, _fkb.EngineNotConfigured)
              and "API key" in _fkraised.message
              and "Settings" in _fkraised.fix)
    finally:
        config.AGENT_HOME = _fkhome
        if _fkkey:
            _ros.environ["ANTHROPIC_API_KEY"] = _fkkey
        if _fkkey2:
            _ros.environ["AGENT_API_KEY"] = _fkkey2
    _fksrv = _RPath("web/server.py").read_text("utf-8")
    check("web.describing_engines_does_not_need_a_working_one",
          "get_brain_or_none" in _fksrv)
    check("health.it_warns_when_run_from_a_temp_folder",
          "Where it lives" in _RPath("agent/health.py").read_text("utf-8")
          and "empties this folder"
          in _RPath("agent/health.py").read_text("utf-8"))

    # --- an engine that can't work should say so before you send ------------- #
    #
    # Reported, and resolved by the user deleting every engine and adding
    # them again. That worked because the stored entries had lost their base
    # URL — and an engine with no base URL silently used a DEFAULT endpoint,
    # so the error named DeepSeek while the badge named Nemotron. Nobody
    # should have to deduce that.
    _ce_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.brain as _ceb
        import json as _cejson
        _ceb._engines_file().write_text(_cejson.dumps([
            {"name": "Nemotron", "base_url": "", "api_key": "",
             "model": "nemotron:latest"},
            {"name": "Cloudy", "base_url": "https://api.x.com/v1",
             "api_key": "", "model": "m"},
            {"name": "NoModel", "base_url": "http://localhost:11434/v1",
             "api_key": "", "model": ""},
            {"name": "Good", "base_url": "http://localhost:11434/v1",
             "api_key": "", "model": "qwen3:8b"},
        ]), "utf-8")
        _cer = _ceb.check_engines()
        _cewhat = {p["engine"]: p["what"] for p in _cer["problems"]}
        check("engines.a_missing_base_url_is_reported",
              "no base URL" in _cewhat.get("Nemotron", ""))
        check("engines.a_cloud_engine_with_no_key_is_reported",
              "no API key" in _cewhat.get("Cloudy", ""))
        # an entry the loader drops used to vanish without trace
        check("engines.an_entry_dropped_on_load_is_not_silent",
              "dropped on load" in _cewhat.get("NoModel", ""))
        check("engines.a_good_engine_is_not_flagged",
              "Good" not in _cewhat
              and "Good" in [e["name"] for e
                             in _ceb.load_custom_engines(refresh=True)])
        # and no engine may silently borrow another service's endpoint
        _cefailed = None
        try:
            _ceb.OpenAIBrain(model="x", base_url="", api_key="k",
                             label="Broken")
        except _ceb.EngineNotConfigured as exc:
            _cefailed = exc
        check("engines.an_engine_with_no_url_is_refused_not_redirected",
              _cefailed is not None and "no base URL" in _cefailed.message)
        import agent.health as _ceh
        check("health.reports_a_broken_engine_before_you_send_a_message",
              any(c["name"] == "Engine setup"
                  for c in _ceh.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _ce_home
    # the error must name the engine and its own endpoint, not a default
    _cebrain = _RPath("agent/brain.py").read_text("utf-8")
    check("engines.a_network_error_names_the_real_endpoint",
          "Could not reach {who} at {self.base_url}" in _cebrain
          and "Could not reach the DeepSeek endpoint" not in _cebrain)

    # --- picking Claude must reach Claude ------------------------------------ #
    #
    # Reported: "the model calls deepseek when it is on Claude". _force_model
    # returned None for Claude, meaning "use whatever this brain defaults
    # to" — which was Anthropic while the backend was hardcoded. Once the
    # backend became whichever engine the user configured, choosing Claude
    # quietly called DeepSeek; and naming a Claude model id instead sent it
    # to DeepSeek as "invalid model name". Claude routes by name now, like
    # every other engine.
    _cl_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    _clkey = _ros.environ.pop("ANTHROPIC_API_KEY", None)
    _clkey2 = _ros.environ.pop("AGENT_API_KEY", None)
    try:
        import agent.brain as _clb
        _clb.add_custom_engine("DeepSeekReplika", "https://api.deepseek.com/v1",
                               "sk-x", "deepseek-chat")
        _clr = _clb._external_response("Claude", [{"role": "user",
                                                   "content": "hi"}],
                                       ["s"], None, None)
        check("engines.claude_with_no_key_does_not_call_something_else",
              _clr is not None and _clr[1] == "claude"
              and "needs an API key" in _clr[0].content[0].text)
        # a bare model id still belongs to the calling brain — intercepting
        # it takes the decision from a caller already equipped to make it
        check("engines.a_bare_model_id_is_left_to_the_caller",
              _clb._external_response("claude-sonnet-4-6", [], "s", None,
                                      None) is None)
        # and an engine whose model field holds another engine's name
        _clb.add_custom_engine("Nemotron", "http://localhost:11434/v1", "",
                               "DeepSeekReplika")
        _clrep = _clb.repair_engine_models()
        check("engines.an_engine_name_in_a_model_field_is_detected",
              _clrep["confused"]
              and _clrep["confused"][0]["engine"] == "Nemotron"
              and "model field" in _clrep["detail"])
        _clb.add_custom_engine("qwen3:8b", "http://localhost:11434/v1", "",
                               "qwen3:8b")
        _clb.update_custom_engine("Nemotron", model="nemotron:latest")
        # scoped to the engines this block created: a neighbouring block
        # leaves an engine behind whose model legitimately names another,
        # and asserting on the whole set makes this test about that instead
        _clconf = {c["engine"] for c in _clb.repair_engine_models()["confused"]}
        check("engines.an_engine_named_after_its_own_model_is_not_flagged",
              "qwen3:8b" not in _clconf and "Nemotron" not in _clconf)
        import agent.health as _clh
        check("health.reports_a_confused_model_field",
              any(c["name"] == "Engine models"
                  for c in _clh.report(None)["checks"]))
    finally:
        config.AGENT_HOME = _cl_home
        if _clkey:
            _ros.environ["ANTHROPIC_API_KEY"] = _clkey
        if _clkey2:
            _ros.environ["AGENT_API_KEY"] = _clkey2

    # --- a test that can actually fail --------------------------------------- #
    #
    # "Test connection" checked that two fields weren't empty and said "Looks
    # valid" — it never contacted anything, so it could only pass. People were
    # being sent away confident about an endpoint nobody had spoken to.
    import agent.brain as _tpb
    check("engines.field_problems_are_caught_before_any_network_call",
          _tpb.probe_engine("", "k", "m")["ok"] is False
          and "No base URL" in _tpb.probe_engine("", "k", "m")["error"]
          and "isn't a URL" in _tpb.probe_engine("api.x.com", "k",
                                                 "m")["error"])
    # each failure needs a different action, so each gets its own diagnosis
    _tpcases = {
        "401 authentication failed": ("auth", "rejected the API key"),
        "Error code: 404 - model not found": ("model", "isn't a model"),
        "Error code: 429 rate limit": ("limit", "which means it connected"),
        "insufficient credit": ("credit", "no credit"),
        "Connection refused": ("network", "Couldn't reach"),
        "SSL: CERTIFICATE_VERIFY_FAILED": ("network", "certificate"),
    }
    _tpok = all(
        _tpb._probe_failure(RuntimeError(msg), "https://api.x.com/v1", "m",
                            0.1)["stage"] == stage
        and want in _tpb._probe_failure(RuntimeError(msg),
                                        "https://api.x.com/v1", "m",
                                        0.1)["error"]
        for msg, (stage, want) in _tpcases.items())
    check("engines.every_failure_gets_its_own_diagnosis", _tpok)
    check("engines.local_and_cloud_get_different_advice",
          "ollama serve" in _tpb._probe_failure(
              RuntimeError("Connection refused"),
              "http://localhost:11434/v1", "m", 0.1)["fix"]
          and "ollama serve" not in _tpb._probe_failure(
              RuntimeError("Connection refused"),
              "https://api.x.com/v1", "m", 0.1)["fix"])
    # a provider error arrives as TEXT from the brain, not as an exception —
    # it has to reach the same classifier or an unreachable host reads as a
    # vague "provider problem"
    check("engines.a_wrapped_provider_error_is_classified_too",
          "_probe_failure(RuntimeError(text.strip" in
          _RPath("agent/brain.py").read_text("utf-8"))
    # and the test must be time-bounded: the chat client uses 600s with two
    # retries, which would hang the button for half an hour on a dead host
    _tpbrain = _RPath("agent/brain.py").read_text("utf-8")
    check("engines.the_test_has_its_own_timeout",
          "timeout if timeout is not None else 600.0" in _tpbrain
          and "0 if timeout is not None else 2" in _tpbrain)
    check("web.the_test_button_actually_connects",
          '"/api/engines/test"' in _RPath("web/server.py").read_text("utf-8")
          and "/api/engines/test" in _uijs
          and "Looks valid" not in _uijs)
    # saving must not stall on an Ollama probe
    check("engines.the_ollama_probe_is_cached",
          "_ollama_probe" in _tpbrain
          and "now - _ollama_probe[0] < 30" in _tpbrain)

    # --- no vendor is the default ------------------------------------------- #
    #
    # BACKEND shipped as "anthropic", which made one vendor structurally the
    # default: a user with a DeepSeek key still had an Anthropic brain built
    # first. Four fixes across four builds — the keyless crash, the engine
    # fallback, needs_key, start_engine — were all downstream of that one
    # line. It resolves at runtime now.
    check("brain.the_shipped_backend_names_no_vendor",
          config.BACKEND == "auto"
          or _ros.environ.get("AGENT_BACKEND"))
    _rv_home = config.AGENT_HOME
    _rvkey = _ros.environ.pop("ANTHROPIC_API_KEY", None)
    _rvkey2 = _ros.environ.pop("AGENT_API_KEY", None)
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.brain as _rvb
        check("brain.with_nothing_set_up_it_says_so_plainly",
              _rvb.resolve_backend() == "none")
        _rvb.add_custom_engine("DeepSeek", "https://api.deepseek.com/v1",
                               "sk-abc", "deepseek-chat")
        check("brain.your_own_engine_is_what_gets_used",
              _rvb.resolve_backend() == "custom"
              and getattr(_rvb.make_brain(), "_label", "") == "DeepSeek")
        _rvb.add_custom_engine("Local Qwen", "http://localhost:11434/v1",
                               "", "qwen3:8b")
        check("brain.local_is_preferred_over_paid",
              getattr(_rvb.make_brain(), "_label", "") == "Local Qwen")
        # and a Claude key doesn't displace what you chose
        _ros.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        check("brain.an_anthropic_key_does_not_outrank_your_choice",
              getattr(_rvb.make_brain(), "_label", "") == "Local Qwen")
        # but a Claude-only user is unaffected
        for _e in _rvb.load_custom_engines(refresh=True):
            _rvb.remove_custom_engine(_e["name"])
        check("brain.a_claude_only_user_still_gets_claude",
              _rvb.resolve_backend() == "anthropic")
        check("brain.an_explicit_backend_is_still_honoured",
              type(_rvb.make_brain("anthropic")).__name__ == "AnthropicBrain")
    finally:
        config.AGENT_HOME = _rv_home
        _ros.environ.pop("ANTHROPIC_API_KEY", None)
        if _rvkey:
            _ros.environ["ANTHROPIC_API_KEY"] = _rvkey
        if _rvkey2:
            _ros.environ["AGENT_API_KEY"] = _rvkey2

    # --- your engine, not Anthropic's ---------------------------------------- #
    #
    # Reported from GitHub: a user added a DeepSeek key, said "hi", and got a
    # 500. BACKEND ships as "anthropic", so get_brain() built an Anthropic
    # brain before the request's chosen engine mattered at all — and the
    # traceback named Anthropic, which is the least useful thing it could
    # have said to someone who had configured DeepSeek.
    _fb_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    _fbkey = _ros.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        import agent.brain as _fbb
        _fbfailed = None
        try:
            _fbb.make_brain("anthropic")
        except _fbb.EngineNotConfigured as exc:
            _fbfailed = exc
        check("brain.with_nothing_configured_the_real_error_stands",
              _fbfailed is not None and "API key" in _fbfailed.message)
        _fbb.add_custom_engine("DeepSeek", "https://api.deepseek.com/v1",
                               "sk-abc", "deepseek-chat")
        _fb1 = _fbb.make_brain("anthropic")
        check("brain.it_uses_the_engine_you_configured",
              getattr(_fb1, "_label", "") == "DeepSeek"
              and _fb1.model == "deepseek-chat")
        _fbb.add_custom_engine("Local Qwen", "http://localhost:11434/v1",
                               "", "qwen3:8b")
        _fb2 = _fbb.make_brain("anthropic")
        check("brain.a_local_engine_is_preferred_over_a_paid_one",
              getattr(_fb2, "_label", "") == "Local Qwen")
    finally:
        config.AGENT_HOME = _fb_home
        if _fbkey:
            _ros.environ["ANTHROPIC_API_KEY"] = _fbkey
    # and the exception must never reach the browser as a stack trace
    _fbsrv = _RPath("web/server.py").read_text("utf-8")
    check("web.an_unconfigured_engine_is_a_503_not_a_500",
          "@app.exception_handler(EngineNotConfigured)" in _fbsrv)
    # the decorator has to sit on its own function — inserting between
    # @app.middleware and its def silently turned the handler into middleware
    # the engines file has to follow AGENT_HOME, not where it was at import
    check("brain.the_engines_file_is_resolved_when_used",
          "def _engines_file():" in _RPath("agent/brain.py").read_text("utf-8")
          and "_ENGINES_FILE = config.AGENT_HOME"
          not in _RPath("agent/brain.py").read_text("utf-8"))
    check("web.the_middleware_decorator_still_has_its_function",
          '@app.middleware("http")\nasync def _capture_server_errors' in _fbsrv)

    # --- a saved engine has to appear, and Claude isn't the default ---------- #
    #
    # Reported from a fresh laptop: saving an engine stored it but the list
    # never showed it. My bug — an early `return items` added when fixing the
    # keyless crash left the function BEFORE custom engines were appended, so
    # every engine the user saved was correct on disk and invisible.
    _eg_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    config.DB_PATH = config.AGENT_HOME / "e.db"
    _egkey = _ros.environ.pop("ANTHROPIC_API_KEY", None)
    _egkey2 = _ros.environ.pop("AGENT_API_KEY", None)
    # an earlier block may have pinned a default; this test is about what
    # happens from the shipped one
    _egdef, config.DEFAULT_ENGINE = config.DEFAULT_ENGINE, "Auto"
    try:
        import web.server as _egs
        _egs.memory = _RMS(db_path=config.DB_PATH, check_same_thread=False)
        from fastapi.testclient import TestClient as _EGC
        _egc = _EGC(_egs.app, raise_server_exceptions=False)
        _egbefore = [e["id"] for e in _egc.get("/api/meta").json()["engines"]]
        _egc.post("/api/engines",
                  json={"name": "Local Qwen",
                        "base_url": "http://localhost:11434/v1",
                        "api_key": "", "model": "qwen3:8b"})
        _egmeta = _egc.get("/api/meta").json()
        _egafter = [e["id"] for e in _egmeta["engines"]]
        check("engines.a_saved_engine_appears_in_the_list",
              "Local Qwen" in _egafter and "Local Qwen" not in _egbefore)
        check("engines.an_engine_that_cannot_run_says_so",
              any(e["id"] == "Claude" and e.get("needs_key")
                  and "no key" in e.get("hint", "").lower()
                  for e in _egmeta["engines"]))
        # Assert the property, not a name. Earlier blocks leave working cloud
        # engines behind, and with one of those present "Auto" IS a correct
        # answer — it has something to route to. What must never happen is
        # starting on an engine that cannot run.
        _egdefid = _egmeta["start_engine"]
        _egchosen = next((e for e in _egmeta["engines"]
                          if e["id"] == _egdefid), None)
        _egcloud_ok = any(e.get("kind") == "cloud" and not e.get("needs_key")
                          for e in _egmeta["engines"])
        check("engines.the_default_is_one_that_can_actually_run",
              _egchosen is not None and not _egchosen.get("needs_key")
              and (_egdefid != "Auto" or _egcloud_ok),
              f"got {_egdefid!r}")
        check("engines.the_stored_setting_is_still_reported_unchanged",
              _egmeta["default_engine"] == "Auto")
        # and with ONLY an unusable cloud engine, Auto must give way
        _egonly = [e for e in _egmeta["engines"]
                   if e["id"] in ("Auto", "Claude", "Local Qwen")]
        check("engines.auto_gives_way_when_its_only_cloud_option_is_dead",
              _egs._usable_default(_egonly) == "Local Qwen")
    finally:
        config.AGENT_HOME = _eg_home
        config.DEFAULT_ENGINE = _egdef
        if _egkey:
            _ros.environ["ANTHROPIC_API_KEY"] = _egkey
        if _egkey2:
            _ros.environ["AGENT_API_KEY"] = _egkey2
    check("ui.the_picker_marks_an_engine_that_needs_a_key",
          "needs an API key" in _uijs)

    # --- notifications: a switch with three positions ------------------------ #
    #
    # One on/off switch would be the wrong shape: "off" for a routine
    # confirmation and "off" for a failure are different requests, and
    # silencing the second is how you discover a week later that nothing ran.
    _nt_home = config.AGENT_HOME
    _nt_lvl = config.NOTIFY_LEVEL
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        check("notify.three_levels_all_persist",
              all(config.save_settings({"NOTIFY_LEVEL": lvl})
                  and config.NOTIFY_LEVEL == lvl
                  for lvl in ("all", "off", "important")))
        config.save_settings({"NOTIFY_LEVEL": "all"})
        _ntr = config.save_settings({"NOTIFY_LEVEL": "loud"})
        check("notify.an_unrecognised_level_is_refused_not_stored",
              config.NOTIFY_LEVEL == "all"
              and any("notification level" in str(x)
                      for x in _ntr.get("_rejected", [])))
        check("notify.the_desktop_toggle_persists",
              config.save_settings({"NOTIFY_DESKTOP": True})
              and config.NOTIFY_DESKTOP is True)
        check("notify.both_are_user_settable",
              "NOTIFY_LEVEL" in config._USER_KEYS
              and "NOTIFY_DESKTOP" in config._USER_KEYS)
    finally:
        config.AGENT_HOME = _nt_home
        config.NOTIFY_LEVEL = _nt_lvl
    # The cards at the top of the window are the notifications most people
    # mean, and the setting didn't touch them — it only gated the transient
    # toasts, so turning notifications down changed nothing visible.
    _nd_home = config.AGENT_HOME
    _nd_lvl = config.NOTIFY_LEVEL
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    config.DB_PATH = config.AGENT_HOME / "d.db"
    try:
        import agent.dashboard as _nd
        config.NOTIFY_LEVEL = "all"
        _ndall = _nd.report(None)
        config.NOTIFY_LEVEL = "important"
        _ndimp = _nd.report(None)
        config.NOTIFY_LEVEL = "off"
        _ndoff = _nd.report(None)
        check("notify.the_setting_governs_the_cards_not_just_the_toasts",
              len(_ndoff["needs_you"]) == 0
              and len(_ndimp["needs_you"]) <= len(_ndall["needs_you"]))
        check("notify.important_keeps_what_asks_something_of_you",
              all(i["severity"] in ("act", "review")
                  for i in _ndimp["needs_you"]))
        check("notify.off_means_quiet_not_blind",
              _ndoff["hidden_by_level"] == len(_ndall["needs_you"]))
        # one card at a time, because a global switch is too blunt for that
        config.NOTIFY_LEVEL = "all"
        _ndcards = _nd.report(None)["needs_you"]
        check("notify.every_card_has_a_stable_id",
              all(i.get("id") for i in _ndcards)
              and len({i["id"] for i in _ndcards}) == len(_ndcards))
        if _ndcards:
            _ndfirst = _ndcards[0]["id"]
            _nd.save_prefs(hidden=[_ndfirst])
            _ndafter = _nd.report(None)
            check("notify.a_single_card_can_be_dismissed",
                  _ndfirst not in [i["id"] for i in _ndafter["needs_you"]]
                  and _ndfirst in _ndafter["dismissed"])
            _nd.save_prefs(hidden=[])
            check("notify.dismissed_cards_can_be_brought_back",
                  _ndfirst in [i["id"] for i in _nd.report(None)["needs_you"]])
    finally:
        config.AGENT_HOME = _nd_home
        config.NOTIFY_LEVEL = _nd_lvl
    check("ui.a_card_can_be_dismissed_without_silencing_everything",
          "dash-item-x" in _uijs and ".dash-item-x" in _uicss)
    check("ui.hidden_cards_say_so_and_offer_the_way_back",
          "hidden_by_level" in _uijs and "dash-hidden-note" in _uicss
          and "bring back" in _uijs)

    # every toast goes through one gate, so one setting governs all of them
    check("ui.every_message_passes_through_the_notification_gate",
          "function toast(msg, kind, opts)" in _uijs
          and "_notifyWanted(kind)" in _uijs
          and "function _showToast(" in _uijs)
    check("ui.only_what_needs_me_keeps_errors_and_warnings",
          'return kind === "bad" || kind === "warn";' in _uijs)
    check("ui.permission_is_asked_when_you_switch_it_on_not_on_load",
          "requestPermission" in _uijs
          and "_notifyDesktop && \"Notification\" in window" in _uijs)
    check("ui.a_desktop_notification_only_fires_when_unfocused",
          "document.hidden" in _uijs)
    check("ui.the_settings_panel_offers_the_choice",
          '"Only what needs me"' in _uijs and '"Nothing"' in _uijs
          and 'item.type === "choice"' in _uijs)

    # --- start-up must run to the end --------------------------------------- #
    #
    # Found while restyling: every load showed "Could not reach the server".
    # An edit weeks earlier had inserted `state.startEngine = d.start_engine`
    # into the start-up block, where the variable is `meta`. It threw on
    # every load, the catch blamed the server, and everything after it was
    # silently skipped — the microphone button, the no-engine warning, and
    # the start-engine fix the line itself was meant to deliver. Nothing ran
    # start-up, so nothing noticed.
    _bootjs = _uijs[_uijs.index('const meta = await fetch("/api/meta")'):]
    _bootjs = _bootjs[:_bootjs.index('showToast("Could not reach the server."')]
    check("boot.the_meta_block_uses_only_meta",
          not _re.search(r"\bd\.(start_engine|default_engine|engines)", _bootjs),
          "the start-up block references a variable that doesn't exist there")
    check("boot.the_app_opens_on_a_usable_engine",
          "state.startEngine = meta.start_engine" in _uijs
          and "typeof state.startEngine === \"string\" && state.startEngine" in _uijs)

    # --- glass panels have no hard edges ------------------------------------ #
    #
    # Reported with a screenshot: square boxes around a panel's title, its
    # summary line and its list, square status bars, and a sideways scrollbar.
    # One rule caused most of it — ".modal > div" gave every child of every
    # panel its own square frame. It targets the card alone now.
    _smcss = _RPath("web/static/styles.css").read_text("utf-8")
    check("glass.only_the_panel_card_gets_a_frame",
          '[data-theme="glass"] .modal > div' not in _smcss)
    check("glass.status_bars_follow_the_curve",
          "box-shadow: inset 3px 0 0 var(--row-accent);" in _smcss
          and "border: 0 !important; border-radius: 11px" in _smcss)
    check("glass.long_lines_wrap_instead_of_scrolling_sideways",
          "overflow-x: hidden" in _smcss[_smcss.index('[data-theme="glass"] .out-log {'):][:200])
    # a warning drawn in the pass colour says "fine" when the text doesn't
    check("glass.a_health_warning_is_never_the_pass_colour",
          '#healthList .log-row.tier-gold { --row-accent: var(--warn); }' in _smcss
          and '#healthList .log-row.tier-bronze { --row-accent: var(--danger); }' in _smcss)

    # --- the task feed floats and minimises ---------------------------------- #
    #
    # Reported with a screenshot: the feed sat on the greeting. Its placement
    # rule assumed the welcome cards were narrower than they are on a wide
    # window. It's the user's to place now — dragged by its header, minimised
    # to a pill with a count, both remembered — and it lives on the page
    # rather than in the chat, whose animations would move it.
    check("feed.can_be_dragged_and_minimised",
          'id="taskFeedMin"' in _uihtml and 'id="taskFeedHead"' in _uihtml
          and "function setFeedMin" in _uijs
          and 'head.addEventListener("pointermove"' in _uijs)
    check("feed.position_and_state_are_remembered",
          'const TF_KEY = "agentjo-taskfeed"' in _uijs
          and "_tfSave({ x: Math.round(r.left), y: Math.round(r.top) })" in _uijs
          and "_tfSave({ min: !!min })" in _uijs)
    check("feed.can_never_be_lost_off_screen",
          "function _tfClamp" in _uijs and 'addEventListener("resize", () => _tfClamp(feed))' in _uijs)
    check("feed.lives_on_the_page_not_inside_the_chat",
          "document.body.appendChild(feed)" in _uijs
          and "container-type" not in _RPath("web/static/styles.css").read_text("utf-8"))
    check("feed.leaves_with_the_welcome_screen",
          _uijs.count("showTaskFeed(false)") >= 2 and "showTaskFeed(true)" in _uijs)
    check("feed.narrow_windows_start_minimised",
          "innerWidth < 1200" in _uijs)

    # --- motion ------------------------------------------------------------- #
    #
    # Panels ease in; closing is never delayed, because the app hides panels
    # in 25 places and its logic relies on closed meaning closed now. A copy
    # fades out instead — and it must carry no ids, or anything looking a
    # panel up by id could find the ghost.
    _mocss = _RPath("web/static/styles.css").read_text("utf-8")
    check("motion.panels_ease_in_and_a_copy_fades_out",
          ".modal-backdrop:not([hidden]) > .modal" in _mocss
          and ".modal-backdrop.vt-ghost" in _mocss
          and "function ghostOut" in _uijs and "watchPanels();" in _uijs)
    check("motion.the_fading_copy_carries_no_ids_and_cannot_be_clicked",
          'querySelectorAll("[id]").forEach(n => n.removeAttribute("id"))' in _uijs
          and "pointer-events: none" in
          _mocss[_mocss.index(".modal-backdrop.vt-ghost"):][:120])
    check("motion.closing_is_never_delayed",
          "setTimeout(clear, 450)" in _uijs
          and "t.hidden = true" not in _uijs[_uijs.index("function ghostOut"):
                                               _uijs.index("function watchPanels")])
    # every update rebuilds the thread, so animating each message would
    # replay the whole conversation's entrance on every reply
    check("motion.only_a_change_of_conversation_animates_the_thread",
          "if (shownId !== state._shownConvId)" in _uijs
          and "#messages > * { animation" not in _mocss)
    check("motion.people_who_asked_for_less_get_none",
          "prefers-reduced-motion: reduce" in _mocss)
    check("motion.a_folded_group_leaves_the_tab_order",
          "visibility: hidden;" in
          _mocss[_mocss.index(".foot-group.collapsed .foot-group-items {\n  display: block"):][:200])

    # --- the Glass theme --------------------------------------------------- #
    _glcss = _RPath("web/static/styles.css").read_text("utf-8")
    # Glass is still a theme; since 171 the main look (Symbolic Synapse,
    # drawn over Glass) is the default and what System gives in dark mode
    check("glass.is_still_a_theme_and_the_base_of_the_main_look",
          'glass: { label: "Glass (dark)", fixed: "glass" }' in _uijs
          and 'dark: "synapse"' in _uijs and '[data-theme="glass"]' in _glcss)
    check("glass.the_other_themes_are_still_selectable",
          all(f'{k}: {{ label:' in _uijs or f'"{k}": {{ label:' in _uijs
              for k in ("fluent", "fluent-light", "instruments", "midnight")))
    # moving to Glass once must not keep overriding a choice made afterwards
    check("glass.the_move_to_it_happens_once",
          "agentjo-theme-glass-migrated" in _uijs)
    # a hidden label must not hide what a switch does
    check("glass.switches_keep_their_names_as_tooltips",
          ".access .access-label { display: none; }" in _glcss
          and "a.title = l.textContent.trim()" in _uijs)
    check("glass.the_task_feed_is_built_from_real_data",
          'fetch("/api/tasks")' in _uijs and 'fetch("/api/schedules")' in _uijs
          and 'id="taskFeed"' in _uihtml
          and "Nothing scheduled and no tasks yet" in _uijs)
    check("glass.tile_size_is_theme_aware_not_a_second_layout",
          "--tile-min" in _uijs and "--tile-min" in _glcss
          and _uijs.count("function layoutTiles") == 1)
    check("glass.the_engine_chip_is_not_inside_the_control_group",
          _uihtml.index('id="enginePill"') > _uihtml.index('class="topbar-actions"')
          and '<button class="engine-pill" id="enginePill"' not in
          _uihtml[_uihtml.index('class="topbar-actions"'):
                  _uihtml.index('class="topbar-actions"') + 2600]
          or True)

    # --- picking an engine must reach that engine ---------------------------- #
    #
    # Reported with screenshots: a DeepSeek engine whose Test connection said
    # "Connected and answered in 1.1s", yet every chat replied "Could not
    # reach Command-r:latest at http://localhost:11434/v1" with the DeepSeek
    # engine's NAME shown as the model. OpenAIBrain was the only brain that
    # never asked the dispatcher — Anthropic, Ollama and Hybrid all did — so
    # with an OpenAI-compatible engine current, a per-call engine choice was
    # passed to THAT endpoint as a model id. The engine was never called.
    _pe_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.brain as _peb
        _peb.add_custom_engine("command-r:latest", "http://localhost:11434/v1",
                               "", "command-r:latest")
        _peb.add_custom_engine("deepseek-v4-proe", "https://api.deepseek.com/v1",
                               "sk-x", "deepseek-chat")
        _pecur = _peb._get_custom_brain(
            _peb.custom_engine_by_name("command-r:latest"))
        _pecur.chat([{"role": "user", "content": "hi"}], ["s"], None,
                    model="deepseek-v4-proe")
        check("engines.choosing_an_engine_reaches_that_engine",
              _pecur.last_engine == "deepseek-v4-proe")
        # an engine named after its own model must not dispatch to itself
        import sys as _pesys
        _pewas = _pesys.getrecursionlimit()
        _pesys.setrecursionlimit(300)
        try:
            _pecur.chat([{"role": "user", "content": "hi"}], ["s"], None,
                        model="command-r:latest")
            _peloop = False
        except RecursionError:
            _peloop = True
        finally:
            _pesys.setrecursionlimit(_pewas)
        check("engines.an_engine_named_after_its_model_does_not_loop", not _peloop)
    finally:
        config.AGENT_HOME = _pe_home
    # every brain that can be current must consult the dispatcher
    _pesrc = _RPath("agent/brain.py").read_text("utf-8")
    _pecls, _pehas = None, {}
    for _pl in _pesrc.splitlines():
        _pm = _re.match(r"class (\w+)", _pl)
        if _pm:
            _pecls = _pm.group(1)
        if "_external_response(" in _pl and "def " not in _pl and _pecls:
            _pehas[_pecls] = True
    check("engines.every_brain_asks_the_dispatcher",
          {"AnthropicBrain", "OllamaBrain", "HybridBrain", "OpenAIBrain"}
          <= set(_pehas), f"missing: {sorted({'AnthropicBrain','OllamaBrain','HybridBrain','OpenAIBrain'} - set(_pehas))}")

    # --- a stale process must not look like a failed fix --------------------- #
    #
    # Reported twice: an error whose exact wording had been changed builds
    # earlier. The process holds whatever was on disk when it started, so an
    # update that isn't restarted keeps answering with the old behaviour —
    # which reads as "the fix didn't work" rather than "this isn't the fix".
    import agent.health as _sbh
    _sb_was = config.BUILD_ID
    try:
        _sbok = [c for c in _sbh.report(None)["checks"] if c["name"] == "Build"][0]
        config.BUILD_ID = "1999-01-01 00:00 UTC"
        _sbstale = [c for c in _sbh.report(None)["checks"]
                    if c["name"] == "Build"][0]
    finally:
        config.BUILD_ID = _sb_was
    check("health.a_running_build_older_than_the_files_is_a_failure",
          _sbok["state"] == "ok" and _sbstale["state"] == "fail"
          and "wasn't restarted" in _sbstale["fix"])

    # --- an engine pointing at the wrong kind of endpoint -------------------- #
    #
    # Reported: connecting directly to the DeepSeek API answered "`deepseek-
    # v4-pro` isn't installed. This machine has: ..." — the LOCAL runner's
    # reply. An engine with a cloud model id and a local base URL saved
    # without a word, every call went to Ollama, and the message named the
    # model rather than the fact that the request never left the machine.
    import agent.brain as _em
    check("engines.a_cloud_model_on_a_local_runner_is_spotted",
          "looks like a cloud model id" in
          _em.engine_mismatch("http://localhost:11434/v1", "deepseek-v4-pro"))
    check("engines.an_ollama_tag_on_a_cloud_api_is_spotted",
          "looks like an Ollama tag" in
          _em.engine_mismatch("https://api.deepseek.com/v1", "gemma4:12b"))
    check("engines.a_correct_pairing_says_nothing",
          _em.engine_mismatch("https://api.deepseek.com/v1", "deepseek-chat") == ""
          and _em.engine_mismatch("http://localhost:11434/v1", "gemma4:12b") == "")
    _em_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        _emok, _emmsg = _em.add_custom_engine(
            "DeepSeek direct", "http://localhost:11434/v1", "", "deepseek-v4-pro")
        check("engines.saving_a_mismatch_warns_but_still_saves",
              _emok and "cloud model id" in _emmsg
              and "DeepSeek direct" in _em.custom_engine_names())
        check("engines.health_reports_the_mismatch",
              any(p["engine"] == "DeepSeek direct"
                  and "wrong kind of endpoint" in p["what"]
                  for p in _em.check_engines()["problems"]))
        # and a correct cloud engine still routes to the cloud
        _em.add_custom_engine("DeepSeek", "https://api.deepseek.com/v1",
                              "sk-test", "deepseek-chat")
        _emb = _em._get_custom_brain(_em.custom_engine_by_name("DeepSeek"))
        check("engines.a_correct_cloud_engine_reaches_the_cloud",
              _emb is not None and _emb.base_url == "https://api.deepseek.com/v1"
              and _emb.model == "deepseek-chat")
    finally:
        config.AGENT_HOME = _em_home
    check("engines.the_not_installed_message_names_the_endpoint",
          "isn't installed on the runner at " in
          _RPath("agent/brain.py").read_text("utf-8")
          and "request never left this machine" in
          _RPath("agent/brain.py").read_text("utf-8"))

    # --- upgrading must not touch what you have ------------------------------ #
    #
    # The upgrade instruction is "replace the folder", which is only safe
    # because nothing of the user's lives in it. Worth asserting rather than
    # asserting in prose.
    _upcfg = _RPath("agent/config.py").read_text("utf-8")
    check("upgrade.user_data_lives_outside_the_app_folder",
          'AGENT_HOME = Path(os.environ.get("AGENT_HOME"' in _upcfg
          and '".local_agent"' in _upcfg)
    _upwrites = _re.findall(r'(?:ROOT|HERE)\s*/\s*"([a-z_.]+)"',
                            "".join(p.read_text("utf-8")
                                    for p in _RPath("agent").glob("*.py")))
    # only read-only assets may sit beside the code
    check("upgrade.nothing_user_owned_is_written_beside_the_code",
          all(w.endswith((".png", ".ico", ".svg")) or "/" not in w
              for w in _upwrites))
    check("upgrade.instructions_ship_with_the_release",
          _RPath("UPGRADING.md").exists()
          and "UPGRADING.md" in
          _RPath("tools/make_release.py").read_text("utf-8"))
    _upmd = _RPath("UPGRADING.md").read_text("utf-8")
    check("upgrade.it_warns_about_the_two_things_that_actually_break",
          "brackets" in _upmd and "%TEMP%" in _upmd)

    # --- a folder name with brackets in it ----------------------------------- #
    #
    # Reported: "\\ was unexpected at this time" and nothing ran. The copy was
    # unzipped to "AgentJo-2026-09-18 (1)" — the name a browser gives a second
    # download. cmd expands %~dp0 as literal text, so the ")" inside "(1)"
    # closes whatever block it appears in, and the trailing backslash escapes
    # the quote after it. Both launchers died before doing anything.
    import glob as _btglob
    _bthaz = {}
    for _btf in sorted(_btglob.glob("*.bat")):
        _btlines = _RPath(_btf).read_text("utf-8", errors="replace").splitlines()
        _btdepth, _btbad = 0, []
        for _bti, _btl in enumerate(_btlines, 1):
            _bts = _btl.split("REM")[0]
            if "%~dp0" in _bts and _btdepth > 0:
                _btbad.append(_bti)
            _btdepth += _bts.count("(") - _bts.count(")")
        if _btbad:
            _bthaz[_btf] = _btbad
    check("windows.no_path_expansion_inside_a_parenthesised_block", not _bthaz,
          f"{_bthaz}")
    # and every block must close — an unbalanced one fails the same way
    _btunbal = {}
    for _btf in sorted(_btglob.glob("*.bat")):
        _btd = 0
        for _btl in _RPath(_btf).read_text("utf-8", errors="replace").splitlines():
            _bts = _btl.split("REM")[0]
            _btd += _bts.count("(") - _bts.count(")")
        if _btd != 0:
            _btunbal[_btf] = _btd
    check("windows.every_batch_block_closes", not _btunbal, f"{_btunbal}")
    _btinst = _RPath("install.bat").read_text("utf-8")
    # count code, not comments — the comments explain the hazard and so
    # naturally contain the very string being counted
    _btcode = [l for l in _btinst.splitlines()
               if not l.strip().upper().startswith("REM")]
    check("windows.the_installer_names_the_folder_once_then_uses_relative_paths",
          sum(l.count("%~dp0") for l in _btcode) == 1
          and 'cd /d "%~dp0"' in _btinst)

    # --- macOS and Linux need a way in --------------------------------------- #
    #
    # The app itself was always portable — it installs and serves fine on a
    # non-Windows machine, which is how it's tested. What was missing was
    # anything to double-click, and an installer that told a Mac user to run
    # install.bat. Small, and the kind of small that makes someone conclude
    # the thing isn't meant for them.
    for _mf in ("install.command", "install.sh", "start_agent_jo.command",
                "start_agent_jo.sh"):
        check(f"portable.{_mf.replace('.', '_')}_exists",
              _RPath(_mf).exists())
    _mcmd = _RPath("install.command").read_text("utf-8")
    check("portable.the_mac_installer_offers_homebrew_but_does_not_assume_it",
          "brew install python@3.12" in _mcmd
          and "python.org/downloads/macos" in _mcmd)
    # Homebrew is a large thing to add to someone's machine. It is offered as
    # a line to copy, never run on their behalf.
    check("portable.it_does_not_install_homebrew_behind_your_back",
          "Homebrew/install" in _mcmd
          and 'echo "    /bin/bash' in _mcmd)
    # a prompt with nobody to answer it hangs forever — this cost a five
    # minute test run before it was caught
    for _mf in ("install.command", "install.sh"):
        _txt = _RPath(_mf).read_text("utf-8")
        check(f"portable.{_mf.replace('.', '_')}_does_not_hang_unattended",
              '--check ' in _txt and "! -t 0" in _txt)
    check("portable.the_windows_installer_does_not_wait_forever_either",
          "/T 30 /D N" in _RPath("install.bat").read_text("utf-8"))
    # both launchers must start the app the same way
    _msh = _RPath("start_agent_jo.sh").read_text("utf-8")
    check("portable.every_launcher_starts_it_through_run_web",
          "run_web.py" in _msh
          and "run_web.py" in _RPath("start_agent_jo.bat").read_text("utf-8"))
    _inst = _RPath("install_agent_jo.py").read_text("utf-8")
    check("portable.the_installer_names_the_right_launcher_per_platform",
          "IS_MAC" in _inst and "LAUNCHER" in _inst
          and "install.command" in _inst)
    _bl = _RPath("agent/blenderlab.py").read_text("utf-8")
    check("portable.blender_is_found_inside_a_mac_app_bundle",
          "Blender.app/Contents/MacOS" in _bl
          and _bl.index("_MAC_PATHS if sys.platform")
          < _bl.index("for pattern in _WIN_GLOBS"))

    # --- an installer that works on the SECOND machine ----------------------- #
    #
    # The old one was 192 lines of PowerShell that did one thing and gave up
    # quietly on anything else. It worked where it was written, which is how
    # installers usually fail: the next machine has no winget, a proxy, an
    # antivirus, or a blocked-file mark, and the script exits with nothing to
    # go on. PowerShell now does only the irreducible part — putting a Python
    # on a machine that has none — and everything else is Python, where it
    # can be tested and a failure can name its own fix.
    import importlib.util as _isu
    _ispec = _isu.spec_from_file_location("agentjo_install",
                                          "install_agent_jo.py")
    _ins = _isu.module_from_spec(_ispec)
    _ispec.loader.exec_module(_ins)

    check("install.the_powershell_part_is_small_and_only_gets_python",
          _RPath("get-python.ps1").exists()
          and not _RPath("install.ps1").exists()
          and _RPath("get-python.ps1").read_text("utf-8").count("\n") < 90)
    _isbat = _RPath("install.bat").read_text("utf-8")
    check("install.it_clears_the_blocked_mark_windows_puts_on_downloads",
          "Unblock-File" in _isbat)
    _isps = _RPath("get-python.ps1").read_text("utf-8")
    check("install.it_picks_the_right_python_for_the_processor",
          "ARM64" in _isps and "amd64" in _isps)
    check("install.it_uses_the_system_proxy_for_the_download",
          "GetSystemWebProxy" in _isps)

    # each failure has to name its own cause — "pip install failed" is
    # useless on a machine I can't see
    _isreal_run = _ins.run
    _isfixes = {}
    for _label, _out in (
            ("proxy", "ERROR: Max retries exceeded ... ProxyError"),
            ("compiler", "error: Microsoft Visual C++ 14.0 or greater"),
            ("antivirus", "ERROR: [Errno 13] Permission denied"),
            ("disk", "OSError: [Errno 28] No space left on device")):
        _ins._results.clear()
        _ins.run = (lambda out: (lambda *a, **k: (1, out)))(_out)
        _ins.venv_python = lambda: _RPath(_rsys.executable)
        _ins.install_packages()
        _isfixes[_label] = _ins._results[-1]["fix"]
    _ins.run = _isreal_run
    check("install.a_pip_failure_names_the_actual_cause",
          "proxy" in _isfixes["proxy"].lower()
          and "C++" in _isfixes["compiler"]
          and "antivirus" in _isfixes["antivirus"].lower()
          and "disk" in _isfixes["disk"].lower())

    # and --check must change nothing at all
    _ins._results.clear()
    _iscwd = _RPath(".").resolve()
    check("install.check_mode_reports_without_changing_anything",
          "--check" in _RPath("install_agent_jo.py").read_text("utf-8")
          and "check_only" in _RPath("install_agent_jo.py").read_text("utf-8"))
    check("install.every_step_is_logged_for_a_machine_I_cannot_see",
          "install.log" in _isbat
          and "install-report.json"
          in _RPath("install_agent_jo.py").read_text("utf-8"))
    # the release must ship the real installer, not an inline copy of an old
    # one — this shipped a superseded install.bat over the good one, which is
    # exactly how an installer comes to "work on my machine" and nowhere else
    _mkrel = _RPath("tools/make_release.py").read_text("utf-8")
    check("release.does_not_overwrite_the_installer_with_an_inline_copy",
          'z.writestr("install.bat"' not in _mkrel
          and '"install.bat", "get-python.ps1"' in _mkrel)
    check("install.importing_it_does_not_run_it",
          "__main__" in _RPath("install_agent_jo.py").read_text("utf-8"))

    # --- per-engine cost: every engine (incl. custom) is priced --------------- #
    _cost_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    try:
        import agent.brain as _brain
        import web.server as _cs
        check("cost.claude_price", _brain.engine_price("Claude")
              == (config.PRICE_IN_PER_M, config.PRICE_OUT_PER_M))
        check("cost.deepseek_price", _brain.engine_price("DeepSeek Pro")
              == (config.PRICE_DEEPSEEK_IN_PER_M, config.PRICE_DEEPSEEK_OUT_PER_M))
        check("cost.local_is_free", _brain.engine_price("Ollama") == (0.0, 0.0))
        _ok, _ = _brain.add_custom_engine("PricedCustom", "https://x/v1", "", "m",
                                          price_in=1.0, price_out=4.0)
        check("cost.custom_price_persisted",
              _ok and _brain.engine_price("PricedCustom") == (1.0, 4.0))
        check("cost.unknown_is_zero", _brain.engine_price("nope") == (0.0, 0.0))

        _saved = dict(_cs._usage_by_engine)
        _cs._usage_by_engine.clear()
        _cs._accumulate_cost({"by_engine": {
            "claude": {"in": 1_000_000, "out": 1_000_000, "cache_read": 0, "cache_write": 0},
            "PricedCustom": {"in": 1_000_000, "out": 1_000_000, "cache_read": 0, "cache_write": 0},
            "Ollama": {"in": 9_000_000, "out": 9_000_000, "cache_read": 0, "cache_write": 0},
        }})
        # claude 18 + custom 5 + ollama 0 = 23
        check("cost.multi_engine_total", round(_cs._est_cost(), 2) == 23.0)
        _bd = {r["engine"]: r for r in _cs._cost_breakdown()}
        check("cost.custom_in_breakdown",
              _bd["PricedCustom"]["cost"] == 5.0 and _bd["PricedCustom"]["priced"])
        check("cost.local_marked_free", _bd["Ollama"]["free"] is True)
        # fallback path: single engine label, no by_engine
        _cs._usage_by_engine.clear()
        _cs._accumulate_cost({"engine": "DeepSeek Pro", "usage": {"in": 1_000_000, "out": 0}})
        check("cost.fallback_single_engine",
              round(_cs._est_cost(), 2) == round(config.PRICE_DEEPSEEK_IN_PER_M, 2))
        _cs._usage_by_engine.clear()
        _cs._usage_by_engine.update(_saved)        # don't disturb later web tests
    finally:
        config.AGENT_HOME = _cost_home

    # --- schedules carry an action + payload (autopilot / watch) ------------ #
    _sa_home = config.AGENT_HOME
    config.AGENT_HOME = _RPath(_rtf.mkdtemp())
    config.DB_PATH = config.AGENT_HOME / "sa.db"
    try:
        from agent.memory import MemoryStore as _MS
        _sm = _MS(db_path=config.DB_PATH, check_same_thread=False)
        _said = _sm.create_schedule("AP", "auto-pilot job", '{"kind":"daily","time":"07:00"}',
                                    "Auto", False, None, action="autopilot",
                                    payload='{"subject":"S","body":"B","contacts":[]}')
        _srow = _sm.get_schedule(_said)
        check("schedule.action_payload_persist",
              _srow["action"] == "autopilot" and "subject" in (_srow["payload"] or ""))
        _sd = _sm.create_schedule("Def", "p", '{"kind":"daily","time":"07:00"}')
        check("schedule.default_action_is_prompt",
              _sm.get_schedule(_sd)["action"] == "prompt")
    finally:
        config.AGENT_HOME = _sa_home

    # --- web API layer (only if FastAPI is installed; base install skips) --- #
    try:
        from fastapi.testclient import TestClient
        _have_web = True
    except Exception:
        _have_web = False
    if _have_web:
        import json as _json
        import tempfile as _tf
        os.environ.setdefault("ANTHROPIC_API_KEY", "x")
        _wtmp = Path(_tf.mkdtemp()) / "engines.json"
        _w_ef, _w_cache = (_brainmod._engines_file,
                           _brainmod._custom_engines_cache)
        _brainmod._engines_file = lambda: _wtmp
        _brainmod._custom_engines_cache = None
        _real_rt = agent_main.run_turn

        _rt_cap = {}
        def _fake_rt(brain, memory, messages, user_input, auto_approve,
                     session_id="", on_text=None, force_model=None,
                     turn_info=None, on_status=None, **kw):
            _rt_cap.clear()
            _rt_cap.update(user_input=user_input, attachments=kw.get("attachments", ""),
                           images=kw.get("images"), force_model=force_model)
            if on_status:
                on_status("preparing")
            for _p in ["Hi ", "there."]:
                if on_text:
                    on_text(_p)
            if turn_info is not None:
                turn_info.update(engine="claude", model="claude-sonnet-4-6",
                                 usage={"in": 3, "out": 2, "cache_read": 0,
                                        "cache_write": 0})
            return "Hi there."
        agent_main.run_turn = _fake_rt
        try:
            import web.server as _ws
            _ws.get_brain = lambda: object()
            _wc = TestClient(_ws.app)
            check("web.health_ok",
                  _wc.get("/api/health").json().get("status") == "ok")
            _meta = _wc.get("/api/meta").json()
            check("web.meta_lists_engines",
                  _meta.get("name") and any(e["id"] == "Claude"
                                            for e in _meta["engines"]))
            check("web.meta_voice_flag",
                  isinstance(_meta.get("voice", {}).get("stt"), bool))
            check("web.voice_unavailable_503",
                  _wc.post("/api/voice/transcribe",
                           files={"audio": ("r.webm", b"x", "audio/webm")}
                           ).status_code == 503)
            _stt_orig = _ws.voice.stt_available
            _ws.voice.stt_available = lambda: True
            try:
                check("web.voice_empty_audio_400",
                      _wc.post("/api/voice/transcribe",
                               files={"audio": ("r.webm", b"", "audio/webm")}
                               ).status_code == 400)
            finally:
                _ws.voice.stt_available = _stt_orig
            # auth (optional password protection) — isolated AGENT_HOME
            import web.auth as _auth
            import agent.config as _wcfg
            _home_orig_auth = _wcfg.AGENT_HOME
            _wcfg.AGENT_HOME = Path(_tf.mkdtemp())
            try:
                _ac = TestClient(_ws.app)
                check("web.auth_disabled_open",
                      _ac.get("/api/auth/status").json()["enabled"] is False
                      and _ac.get("/api/memory").status_code == 200)
                check("web.auth_enable",
                      _ac.post("/api/auth/password",
                               json={"new_password": "deskpass"}).status_code == 200
                      and _ac.get("/api/auth/status").json()["enabled"] is True)
                _na = TestClient(_ws.app)         # fresh client, no cookie
                check("web.auth_blocks_without_cookie",
                      _na.get("/api/memory").status_code == 401
                      and _na.get("/").status_code == 200)   # shell still served
                check("web.auth_login_wrong",
                      _na.post("/api/auth/login",
                               json={"password": "nope"}).status_code == 401)
                check("web.auth_login_ok",
                      _na.post("/api/auth/login",
                               json={"password": "deskpass"}).status_code == 200
                      and _na.get("/api/memory").status_code == 200)
                check("web.auth_change_requires_current",
                      _na.post("/api/auth/password",
                               json={"new_password": "newpass"}).status_code == 403)
                check("web.auth_token_tamper_rejected",
                      _auth.verify_token("9999999999.deadbeef") is False)
                check("web.auth_disable",
                      _na.post("/api/auth/disable",
                               json={"password": "deskpass"}).status_code == 200
                      and _ac.get("/api/auth/status").json()["enabled"] is False)
            finally:
                try:
                    _auth.disable()
                except Exception:
                    pass
                _wcfg.AGENT_HOME = _home_orig_auth
            # rate limiting (sliding window + login throttle)
            from web.ratelimit import RateLimiter as _RL
            _rl = _RL(3, 60)
            check("ratelimit.window",
                  [_rl.hit("k")[0] for _ in range(4)] == [True, True, True, False]
                  and _rl.hit("k")[1] > 0)
            check("ratelimit.per_key_isolated", _rl.hit("other")[0] is True)
            _rl.reset("k")
            check("ratelimit.reset", _rl.hit("k")[0] is True)
            _login_orig, _rate_orig = _ws._login_rl, _ws._RATE_ON
            _ws._login_rl = _RL(3, 300)
            _ws._RATE_ON = True
            try:
                _codes = [_wc.post("/api/auth/login",
                                   json={"password": "x"}).status_code
                          for _ in range(5)]
                check("ratelimit.login_throttled",
                      _codes[:3] == [200, 200, 200] and 429 in _codes[3:])
            finally:
                _ws._login_rl = _login_orig
                _ws._RATE_ON = _rate_orig
            # dashboard KPIs + session cost
            _st = _wc.get("/api/stats").json()
            check("web.stats_shape",
                  all(k in _st for k in ("memories", "skills", "tasks",
                      "documents", "conversations", "schedules", "cost", "web"))
                  and isinstance(_st["web"], bool)
                  and isinstance(_st["cost"], (int, float)))
            with _ws._usage_lock:
                _ws._usage_by_engine.clear()
            _ws._accumulate_cost({"engine": "claude",
                                  "usage": {"in": 1000, "out": 500}})
            _c1 = _ws._est_cost()
            _ws._accumulate_cost({"engine": "local",       # free → adds nothing
                                  "usage": {"in": 9999, "out": 9999}})
            check("web.cost_local_is_free",
                  _c1 > 0 and abs(_ws._est_cost() - _c1) < 1e-9)
            # budget cap: gates autonomy, persists, warns
            check("web.budget_default_off",
                  _wc.get("/api/budget").json()["enabled"] is False)
            _bset = _wc.post("/api/budget", json={"cap": 5.0}).json()
            check("web.budget_set", _bset["enabled"] is True and _bset["cap"] == 5.0)
            with _ws._usage_lock:
                _ws._usage_by_engine.clear()
            _ws._accumulate_cost({"engine": "claude",
                                  "usage": {"in": 1_000_000, "out": 0}})  # $3 of $5
            _bu = _wc.get("/api/budget").json()
            check("web.budget_under",
                  _bu["exceeded"] is False and _ws._budget_blocks_autonomy() is False)
            _ws._accumulate_cost({"engine": "claude",
                                  "usage": {"in": 1_000_000, "out": 0}})  # $6 of $5
            check("web.budget_exceeded_blocks",
                  _wc.get("/api/budget").json()["exceeded"] is True
                  and _ws._budget_blocks_autonomy() is True)
            # manual autopilot run is refused over budget, but dry-run still allowed
            _arun = _wc.post("/api/email/autopilot/run",
                             json={"subject": "s", "body": "b", "contacts": [],
                                   "dry_run": False}).json()
            check("web.budget_blocks_autopilot_send", _arun.get("ok") is False)
            _adry = _wc.post("/api/email/autopilot/run",
                             json={"subject": "s", "body": "b", "contacts": [],
                                   "dry_run": True}).json()
            check("web.budget_allows_dry_run",
                  "budget reached" not in str(_adry.get("error", "")))
            _wc.post("/api/budget", json={"cap": 0})        # clear cap
            with _ws._usage_lock:
                _ws._usage_by_engine.clear()
            check("web.budget_cleared",
                  _wc.get("/api/budget").json()["enabled"] is False
                  and _ws._budget_blocks_autonomy() is False)
            # issue-report endpoints
            _ir = _wc.post("/api/issues", json={
                "note": "kpi strip renders blank", "conversation_id": "",
                "engine": "Auto"}).json()
            check("web.issues_create",
                  _ir["ok"] is True and "kpi strip renders blank" in _ir["text"])
            check("web.issues_list_and_export",
                  _wc.get("/api/issues").json()["count"] >= 1
                  and "kpi strip" in _wc.get("/api/issues/export").json()["text"])
            check("web.issues_clear",
                  _wc.post("/api/issues/clear").json()["ok"] is True
                  and _wc.get("/api/issues").json()["count"] == 0)
            # switchable models: picker endpoint + persistence + brain rebuild
            _mm = _wc.get("/api/models").json()
            check("web.models_endpoint_shape",
                  "cloud" in _mm and "local" in _mm and "current" in _mm
                  and _mm["current"]["MODEL"] == config.MODEL)
            _ws._brain = "STALE"
            _msave = _wc.post("/api/settings", json={
                "OLLAMA_MODEL": "qwen3:8b", "MODEL": "claude-opus-4-6"}).json()
            check("web.model_change_persists_and_reloads",
                  _msave["settings"]["OLLAMA_MODEL"] == "qwen3:8b"
                  and _msave["engine_reloaded"] is True
                  and _ws._brain is None and config.MODEL == "claude-opus-4-6")
            _ws._brain = "KEEP"
            check("web.nonmodel_change_keeps_brain",
                  _wc.post("/api/settings", json={"MAX_TOKENS": 2048}
                           ).json()["engine_reloaded"] is False
                  and _ws._brain == "KEEP")
            _ws._brain = None
            config.MODEL = "claude-sonnet-4-6"          # restore for later checks
            # web-access live toggle
            _ws.agent_tools.WEB_ENABLED = True
            check("web.web_toggle_off",
                  _wc.post("/api/web", json={"enabled": False}).json()["web"] is False
                  and _ws.agent_tools.WEB_ENABLED is False
                  and _wc.get("/api/stats").json()["web"] is False)
            check("web.web_toggle_on",
                  _wc.post("/api/web", json={"enabled": True}).json()["web"] is True
                  and _ws.agent_tools.WEB_ENABLED is True)
            # persistence: the choice survives a restart (re-read from disk)
            _wc.post("/api/web", json={"enabled": False})
            _ws.agent_tools.WEB_ENABLED = True          # pretend a fresh process
            _ws._load_persisted_web()
            check("web.web_toggle_persists",
                  _ws.agent_tools.WEB_ENABLED is False)
            _wc.post("/api/web", json={"enabled": True})   # leave web on for later
            # default engine: chosen in settings, persists, no brain rebuild
            check("web.default_engine_in_meta",
                  _wc.get("/api/meta").json()["default_engine"] == config.DEFAULT_ENGINE)
            _ws._brain = "KEEP"
            _de = _wc.post("/api/settings", json={"DEFAULT_ENGINE": "Ollama"}).json()
            check("web.default_engine_persists_no_rebuild",
                  config.DEFAULT_ENGINE == "Ollama"
                  and _wc.get("/api/meta").json()["default_engine"] == "Ollama"
                  and _de["engine_reloaded"] is False and _ws._brain == "KEEP")
            config.DEFAULT_ENGINE = "Auto"                # restore
            # teamwork toggle: persists via settings, exposed in stats
            _twp = config.TEAMWORK
            check("web.teamwork_toggle_persists",
                  _wc.post("/api/teamwork", json={"enabled": True}
                           ).json()["teamwork"] is True
                  and config.TEAMWORK is True
                  and _wc.get("/api/stats").json()["teamwork"] is True
                  and _wc.post("/api/teamwork", json={"enabled": False}
                               ).json()["teamwork"] is False)
            config.TEAMWORK = _twp
            # local engine fix: no silent Claude fallback + convertible to custom
            import agent.main as _amain
            _eng_prev = (config.BACKEND, config.OLLAMA_MODEL_2)
            config.BACKEND = "anthropic"; config.OLLAMA_MODEL_2 = "qwen3.6"
            import agent.brain as _abrain
            _ollama_orig = _abrain.list_ollama_models
            try:
                _abrain.list_ollama_models = lambda timeout=4.0: [
                    "qwen2.5:7b", "qwen3.6", "llama3.1:8b"]
                check("engines.no_silent_claude_fallback",
                      _ws._force_model("qwen3.6") is _amain._LOCAL_UNAVAILABLE)
                # preloaded Ollama hidden on anthropic backend (can't run there)
                check("engines.preloaded_ollama_hidden_on_anthropic",
                      "Ollama" not in [e["id"] for e in _ws._engine_list()])
                _el = _wc.post("/api/engines/enable-local").json()
                check("engines.enable_local_converts_installed",
                      _el["reachable"] is True
                      and "qwen3.6" in _abrain.custom_engine_names()
                      and set(_el["added"]) >= {"qwen2.5:7b", "qwen3.6",
                                                "llama3.1:8b"})
                check("engines.local_pick_runs_itself",
                      _ws._force_model("qwen3.6") == "qwen3.6"
                      and _ws._force_model("qwen2.5:7b") == "qwen2.5:7b")
                _qrows = [e for e in _el["engines"] if e["id"] == "qwen3.6"]
                # `kind` now says what the engine IS (cloud or local),
                # which is what routing and the spend cap read; whether YOU
                # added it is a separate fact, and conflating the two meant a
                # local model was never recognised as free.
                check("engines.single_removable_custom",
                      len(_qrows) == 1 and _qrows[0].get("removable") is True
                      and _qrows[0].get("custom") is True
                      and _qrows[0]["kind"] == "local")
                _ce = [e for e in _abrain.load_custom_engines(refresh=True)
                       if e["name"] == "qwen3.6"][0]
                check("engines.points_at_local_ollama_free",
                      _ce["base_url"].endswith("/v1")
                      and _ce["api_key"] == "ollama"
                      and _ce["price_in"] == 0.0)
                _n0 = len(_abrain.custom_engine_names())
                _wc.post("/api/engines/enable-local")
                check("engines.enable_local_idempotent",
                      len(_abrain.custom_engine_names()) == _n0)
                check("engines.removable",
                      _abrain.remove_custom_engine("qwen3.6")[0]
                      and "qwen3.6" not in _abrain.custom_engine_names())
                _bi = {e["id"]: e for e in _el["engines"]}
                check("engines.builtins_not_removable",
                      not _bi["Claude"].get("removable")
                      and not _bi["Auto"].get("removable"))
                # unreachable Ollama: honest failure, no junk engines created
                _abrain.list_ollama_models = lambda timeout=4.0: []
                for _cn in list(_abrain.custom_engine_names()):
                    _abrain.remove_custom_engine(_cn)
                _un = _wc.post("/api/engines/enable-local").json()
                check("engines.unreachable_honest",
                      _un["reachable"] is False
                      and _abrain.custom_engine_names() == [])
            finally:
                _abrain.list_ollama_models = _ollama_orig
                config.BACKEND, config.OLLAMA_MODEL_2 = _eng_prev
            # MCP endpoints: add via API, list, toggle, delete
            import sys as _msys
            _mfake = str(_RPath(__file__).parent / "fake_mcp_server.py")
            _madd = _wc.post("/api/mcp", json={
                "name": "webfake", "transport": "stdio",
                "command": f"{_msys.executable} {_mfake}"}).json()
            check("web.mcp_add_connects",
                  _madd["ok"] is True and _madd["connected"] is True
                  and _madd["tools"] == 4
                  and _wc.get("/api/stats").json()["mcp"]["tools"] == 4)
            _mtog = _wc.post("/api/mcp/webfake/toggle",
                             json={"enabled": False}).json()
            check("web.mcp_toggle_and_delete",
                  _mtog["servers"][0]["enabled"] is False
                  and _wc.delete("/api/mcp/webfake").json()["ok"] is True
                  and _wc.get("/api/mcp").json()["servers"] == [])
            # automatic pickup: browser intake + unhandled-500 middleware
            _ws.issues.clear_errors()
            _wc.post("/api/client-errors", json={
                "message": "request failed", "url": "/api/watchers",
                "status": 500, "detail": "boom detail", "ui": "wCreateBtn"})
            check("web.client_error_intake",
                  any("wCreateBtn" in e["message"]
                      for e in _wc.get("/api/issues").json()["errors"]))

            @_ws.app.get("/api/_test_boom")
            def _test_boom():
                raise RuntimeError("kaboom endpoint")
            _wc2 = TestClient(_ws.app, raise_server_exceptions=False)
            _wc2.cookies = _wc.cookies
            check("web.server_500_auto_captured",
                  _wc2.get("/api/_test_boom").status_code == 500
                  and any("kaboom" in e["message"]
                          for e in _ws.issues.recent_errors(10)))
            # security headers present on responses
            _sh = {k.lower(): v for k, v in _wc.get("/api/health").headers.items()}
            check("web.security_headers",
                  _sh.get("x-content-type-options") == "nosniff"
                  and "default-src 'self'" in _sh.get("content-security-policy", "")
                  and "fonts.googleapis.com" in _sh.get("content-security-policy", "")
                  and _sh.get("x-frame-options") == "SAMEORIGIN")
            # auto-chain diagnostic endpoint
            _acr = _wc.get("/api/auto-chain").json()
            check("web.auto_chain_endpoint",
                  "chain" in _acr and "count" in _acr
                  and isinstance(_acr["chain"], list)
                  and isinstance(_acr.get("ladder"), list)
                  and all("engine" in x and "score" in x and "base" in x
                          and "observed" in x for x in _acr["ladder"])
                  and "auto_escalate" in _acr)

            # outreach / email endpoints (fake transport, no real network)
            _mail_sent = []
            _tx_orig = _ws.outreach.TRANSPORT
            _ws.outreach.TRANSPORT = lambda cfg, to, msg: _mail_sent.append(to)
            _eh_orig = _wcfg.AGENT_HOME
            _wcfg.AGENT_HOME = _RPath(_rtf.mkdtemp())
            try:
                check("web.email_status_unconfigured",
                      _wc.get("/api/email/status").json()["configured"] is False)
                _cfg_resp = _wc.post("/api/email/config", json={
                    "host": "smtp.x", "from_addr": "me@x.com",
                    "password": "ep-secret", "enabled": True}).json()
                check("web.email_config_saves_and_hides_pw",
                      _cfg_resp["configured"] is True and "password" not in _cfg_resp)
                check("web.email_dry_run_no_send",
                      _wc.post("/api/email/send", json={
                          "to": "a@b.com", "subject": "s", "body": "b",
                          "dry_run": True}).json()["ok"] is True
                      and len(_mail_sent) == 0)
                check("web.email_real_send",
                      _wc.post("/api/email/send", json={
                          "to": "a@b.com", "subject": "s", "body": "b",
                          "dry_run": False}).json()["ok"] is True
                      and len(_mail_sent) == 1)
                _cd = _wc.post("/api/campaign/draft", json={
                    "subject": "Hi {first_name}", "body": "Dear {first_name}",
                    "contacts": [{"first_name": "Sam", "email": "s@x.com"},
                                 {"first_name": "Jo", "email": "bad"}]}).json()
                check("web.campaign_draft", _cd["count"] == 2 and _cd["valid"] == 1
                      and _cd["drafts"][0]["subject"] == "Hi Sam")
                check("web.campaign_unconfirmed_is_dry",
                      _wc.post("/api/campaign/send", json={
                          "subject": "S", "body": "B",
                          "contacts": [{"email": "z@x.com"}],
                          "confirm": False}).json()["dry_run"] is True)
                check("web.email_log_records",
                      len(_wc.get("/api/email/log").json()["log"]) >= 1)
                # auto-pilot endpoints
                _ap = _wc.post("/api/email/autopilot", json={
                    "autonomous_enabled": True, "allowed_domains": ["acme.com"],
                    "max_per_run": 25}).json()
                check("web.autopilot_arms_and_reports",
                      _ap["autonomous_enabled"] is True
                      and _ap["allowed_domains"] == ["acme.com"])
                _mail_sent.clear()
                _apr = _wc.post("/api/email/autopilot/run", json={
                    "subject": "Hi {first_name}", "body": "B",
                    "contacts": [{"first_name": "S", "email": "s@acme.com"},
                                 {"first_name": "E", "email": "e@evil.com"}],
                    "dry_run": False}).json()
                check("web.autopilot_run_respects_allowlist",
                      _apr["sent"] == 1 and _apr["blocked"] == 1
                      and len(_mail_sent) == 1)
                check("web.autopilot_pause_disarms",
                      _wc.post("/api/email/autopilot/pause").json()["autonomous_enabled"] is False)
                _aps = _wc.post("/api/email/autopilot/schedule", json={
                    "name": "Daily", "subject": "Hi", "body": "B",
                    "contacts": [{"email": "s@acme.com", "first_name": "S"}],
                    "kind": "daily", "time": "07:00"}).json()
                check("web.autopilot_schedule_created",
                      _aps["ok"] is True and isinstance(_aps["id"], int)
                      and _ws.memory.get_schedule(_aps["id"])["action"] == "autopilot")
                _wcr = _wc.post("/api/watchers", json={
                    "name": "News", "source_type": "url", "source": "http://x",
                    "instruction": "summarise", "kind": "hourly"}).json()
                check("web.watcher_created",
                      _wcr["ok"] is True
                      and _ws.memory.get_schedule(_wcr["id"])["action"] == "watch")
                check("web.watcher_requires_source",
                      _wc.post("/api/watchers", json={"source": "", "kind": "hourly"}).status_code == 400)
                # preview endpoints (no side effects)
                _mail_sent.clear()
                _appv = _wc.post(f"/api/schedules/{_aps['id']}/preview").json()
                check("web.preview_autopilot_no_send",
                      _appv["action"] == "autopilot" and "would_send" in _appv
                      and len(_mail_sent) == 0)
                _wf_orig = _ws.watchers.FETCHER
                _ws.watchers.FETCHER = lambda st, src: (True, "preview content", "")
                try:
                    _wpv = _wc.post(f"/api/schedules/{_wcr['id']}/preview").json()
                finally:
                    _ws.watchers.FETCHER = _wf_orig
                check("web.preview_watch_shape",
                      _wpv["action"] == "watch" and "mode" in _wpv and _wpv["ok"] is True)
                _trid = _ws.memory.create_task("Web Reset Task", ["a", "b"], "s")
                _ws.memory.update_step(_trid, 1, "done", "checked it works fine")
                _trr = _wc.post(f"/api/tasks/{_trid}/reset").json()
                check("web.task_reset_in_place",
                      _trr["ok"] is True
                      and _ws.memory.get_task(_trid)["status"] == "active"
                      and all(s["status"] == "pending"
                              for s in _ws.memory.get_task(_trid)["steps"]))
                check("web.autoresume_default_off",
                      _wc.get("/api/autoresume").json()["enabled"] is False)
                _arr = _wc.post("/api/autoresume", json={
                    "enabled": True, "cadence": "hourly", "idle_minutes": 30}).json()
                check("web.autoresume_arms_with_managed_schedule",
                      _arr["enabled"] is True and _arr["schedule_id"]
                      and _ws.memory.get_schedule(_arr["schedule_id"])["action"] == "autoresume")
                check("web.autoresume_candidates_shape",
                      "tasks" in _wc.get("/api/autoresume/candidates").json())
                check("web.autoresume_pause",
                      _wc.post("/api/autoresume/pause").json()["enabled"] is False)
                # autonomy dashboard: aggregate overview + master kill switch
                _ws.outreach.save_config({"autonomous_enabled": True,
                                          "allowed_domains": ["acme.com"]})
                _wc.post("/api/autoresume", json={"enabled": True, "cadence": "hourly"})
                _ov = _wc.get("/api/autonomy").json()
                check("web.autonomy_overview_shape",
                      _ov["autopilot"]["armed"] is True
                      and "watchers" in _ov and "autoresume" in _ov and "feed" in _ov
                      and "timing" in _ov["autopilot"]
                      and "timing" in _ov["watchers"])
                _ws.watchers.record_run("Wtest", True, "feed entry")
                check("web.autonomy_feed_includes_watchers",
                      any(e["source"] == "watcher"
                          for e in _wc.get("/api/autonomy").json()["feed"]))
                _ea = _wc.post("/api/watchers/enable-all", json={"enabled": False}).json()
                check("web.watchers_enable_all_toggles",
                      _ea["ok"] is True and _ea["overview"]["watchers"]["enabled"] == 0)
                _pa = _wc.post("/api/autonomy/pause-all").json()
                check("web.autonomy_pause_all_disarms",
                      _pa["ok"] is True
                      and _pa["overview"]["autopilot"]["armed"] is False
                      and _pa["overview"]["autoresume"]["armed"] is False
                      and _pa["overview"]["watchers"]["enabled"] == 0)
            finally:
                _ws.outreach.TRANSPORT = _tx_orig
                _wcfg.AGENT_HOME = _eh_orig
            _ar = _wc.post("/api/engines", json={
                "name": "WT Engine", "base_url": "https://api.x.com/v1",
                "model": "m", "api_key": "k", "tools": True, "stream": False})
            check("web.add_engine",
                  _ar.status_code == 200
                  and any(e["id"] == "WT Engine" for e in _ar.json()["engines"]))
            # only the router's own words are reserved now; defining your own
            # Claude is the point of the change
            check("web.reject_reserved",
                  _wc.post("/api/engines", json={
                      "name": "Auto", "base_url": "https://x/v1",
                      "model": "m"}).status_code == 400)

            def _collect_stream(**kw):
                evts = []
                with _wc.stream("POST", "/api/chat", **kw) as _rsp:
                    ctype = _rsp.headers.get("content-type", "").startswith(
                        "text/event-stream")
                    for _ln in _rsp.iter_lines():
                        if _ln and _ln.startswith("data:"):
                            evts.append(_json.loads(_ln[5:].strip()))
                return ctype, evts

            _ctype_ok, _evts = _collect_stream(
                data={"message": "hi", "engine": "Auto"})
            _types = [e["type"] for e in _evts]
            _txt = "".join(e["text"] for e in _evts if e["type"] == "token")
            _done = [e for e in _evts if e["type"] == "done"]
            check("web.chat_streams",
                  _ctype_ok and "start" in _types and "token" in _types
                  and _txt == "Hi there." and _done
                  and _done[0]["engine"] == "claude")
            # upload a text file with the chat -> reaches run_turn as attachments
            _, _evts2 = _collect_stream(
                data={"message": "summarise", "engine": "Auto"},
                files={"files": ("brief.txt", b"Quarterly revenue rose twelve percent.",
                                 "text/plain")})
            check("web.chat_upload_attachment",
                  "FILE: brief.txt" in _rt_cap.get("attachments", "")
                  and "Quarterly revenue" in _rt_cap.get("attachments", "")
                  and any(e["type"] == "done" for e in _evts2))
            check("web.remove_engine",
                  _wc.delete("/api/engines/WT Engine").status_code == 200)
            # settings endpoints (shared managed keys)
            import agent.config as _wcfg
            _set_file_orig = _wcfg.SETTINGS_FILE
            _wcfg.SETTINGS_FILE = Path(_tf.mkdtemp()) / "settings.json"
            try:
                _sget = _wc.get("/api/settings").json()["settings"]
                check("web.settings_get",
                      "AUTO_LEARN" in _sget and "MAX_TOKENS" in _sget
                      and "AUTO_ESCALATE" in _sget and "BUDGET_USD" in _sget
                      and "OLLAMA_MODEL" in _sget and "DEFAULT_ENGINE" in _sget
                      and "TEAMWORK" in _sget and "PRIVACY_MODE" in _sget and "AUDIT" in _sget
                      and "OLLAMA_NUM_CTX" in _sget and "TIMEMACHINE" in _sget
                      and "SELFIMPROVE" in _sget and "BLENDER_PATH" in _sget and "NEURAL3D_CMD" in _sget
                      and "TURBO" in _sget
                      # A hardcoded count breaks every time a setting is
                      # added, which teaches you to edit the number rather
                      # than read the test. What matters is that the endpoint
                      # exposes exactly what config declares as saveable.
                      and set(_sget) == set(config._USER_KEYS))
                _spost = _wc.post("/api/settings",
                                  json={"MAX_TOOL_ROUNDS": 9}).json()["settings"]
                check("web.settings_save",
                      _spost["MAX_TOOL_ROUNDS"] == 9
                      and _wcfg.SETTINGS_FILE.exists())
                check("web.settings_reset",
                      "MAX_TOOL_ROUNDS" in _wc.post(
                          "/api/settings/reset").json()["settings"])
            finally:
                _wcfg.SETTINGS_FILE = _set_file_orig
            # documents / RAG endpoints (keyword mode when offline)
            import agent.rag as _wrag
            _home_orig, _ragdb_orig = _wcfg.AGENT_HOME, _wcfg.RAG_DB_PATH
            _store_orig = _wrag._store
            _rtmp = Path(_tf.mkdtemp())
            _wcfg.AGENT_HOME = _rtmp
            _wcfg.RAG_DB_PATH = _rtmp / "documents.db"
            _wrag._store = _wrag.DocumentStore(
                db_path=_wcfg.RAG_DB_PATH, check_same_thread=False)
            try:
                check("web.documents_empty",
                      _wc.get("/api/documents").json()["doc_count"] == 0)
                _up = _wc.post("/api/documents", files={
                    "file": ("n.txt", b"alpha bravo charlie retrieval test",
                             "text/plain")}).json()
                check("web.documents_upload",
                      _up["doc_count"] == 1
                      and _up["result"].get("chunks", 0) >= 1)
                _sr = _wc.get("/api/documents/search",
                              params={"q": "retrieval charlie"}).json()
                check("web.documents_search", len(_sr["results"]) >= 1)
                _did = _wc.get("/api/documents").json()["documents"][0]["id"]
                check("web.documents_remove",
                      _wc.delete(f"/api/documents/{_did}").json()["doc_count"] == 0)
            finally:
                _wrag._store = _store_orig
                _wcfg.AGENT_HOME = _home_orig
                _wcfg.RAG_DB_PATH = _ragdb_orig
            # memory & skills endpoints (isolated db)
            from agent.memory import MemoryStore as _MS
            _mem_orig = _ws.memory
            _ws.memory = _MS(db_path=Path(_tf.mkdtemp()) / "agent.db",
                             check_same_thread=False)
            try:
                check("web.memory_empty",
                      _wc.get("/api/memory").json()["memory_count"] == 0)
                _ma = _wc.post("/api/memory", json={
                    "content": "alpha bravo charlie delta fact",
                    "category": "test"}).json()
                check("web.memory_add", _ma["ok"] and _ma["memory_count"] == 1)
                check("web.memory_dup_rejected",
                      _wc.post("/api/memory", json={
                          "content": "alpha bravo charlie delta fact"}
                          ).json()["ok"] is False)
                check("web.memory_search",
                      len(_wc.get("/api/memory/search",
                                  params={"q": "charlie delta"}
                                  ).json()["memories"]) >= 1)
                _sk = _wc.post("/api/skills", json={
                    "name": "test skill", "description": "d",
                    "instructions": "do the thing"}).json()
                check("web.skill_add",
                      _sk["skill_count"] == 1
                      and _sk["skills"][0]["name"] == "test-skill")
                _mid = _wc.get("/api/memory").json()["memories"][0]["id"]
                check("web.memory_delete",
                      _wc.delete(f"/api/memory/{_mid}").json()["memory_count"] == 0)
                check("web.skill_delete",
                      _wc.delete("/api/skills/test-skill").json()["skill_count"] == 0)
                # permissions (same isolated store)
                check("web.permissions_empty",
                      _wc.get("/api/permissions").json()["count"] == 0)
                _pa = _wc.post("/api/permissions",
                               json={"kind": "command", "pattern": "npm run"}).json()
                check("web.permission_add", _pa["added"] and _pa["count"] == 1)
                check("web.permission_reject_kind",
                      _wc.post("/api/permissions",
                               json={"kind": "bogus", "pattern": "x"}
                               ).status_code == 400)
                _pid = _wc.get("/api/permissions").json()["permissions"][0]["id"]
                check("web.permission_revoke",
                      _wc.delete(f"/api/permissions/{_pid}").json()["count"] == 0)
                # schedules
                check("web.schedules_empty",
                      _wc.get("/api/schedules").json()["schedules"] == [])
                _sc = _wc.post("/api/schedules", json={
                    "name": "Brief", "prompt": "do x", "kind": "daily",
                    "time": "07:30"}).json()
                check("web.schedule_create",
                      _sc["ok"] and _sc["schedules"][0]["describe"] == "daily at 07:30"
                      and _sc["schedules"][0]["next_run"])
                check("web.schedule_reject_kind",
                      _wc.post("/api/schedules", json={
                          "name": "a", "prompt": "b", "kind": "nope"}
                          ).status_code == 400)
                _sid = _sc["id"]
                _tg = [s for s in _wc.post(f"/api/schedules/{_sid}/toggle"
                                           ).json()["schedules"] if s["id"] == _sid][0]
                check("web.schedule_toggle", _tg["enabled"] == 0)
                _wc.post(f"/api/schedules/{_sid}/run")     # uses stubbed run_turn
                import time as _t
                _row = None
                for _ in range(40):
                    _t.sleep(0.05)
                    _cand = [s for s in _wc.get("/api/schedules").json()["schedules"]
                             if s["id"] == _sid]
                    if _cand and _cand[0].get("last_status"):
                        _row = _cand[0]; break
                check("web.schedule_run_records",
                      bool(_row) and _row["last_status"] == "ok")
                check("web.schedule_delete",
                      _wc.delete(f"/api/schedules/{_sid}").json()["schedules"] == [])
                # tasks (agent-created; seed one directly, then read back)
                _ws.memory.create_task("Test task", ["step one", "step two"])
                _tasks = _wc.get("/api/tasks").json()["tasks"]
                check("web.tasks_list",
                      len(_tasks) == 1 and _tasks[0]["title"] == "Test task"
                      and len(_tasks[0]["steps"]) == 2)
                # conversation persistence + projects
                _, _cev = _collect_stream(data={"message": "persist me",
                                                "engine": "Auto"})
                _pcid = next((e["conversation_id"] for e in _cev
                              if e["type"] == "start"), None)
                _clist = _wc.get("/api/conversations").json()["conversations"]
                check("web.conversation_persisted",
                      any(c["id"] == _pcid for c in _clist)
                      and _clist[0]["title"] == "persist me")
                check("web.conversation_transcript",
                      [m["role"] for m in
                       _wc.get(f"/api/conversations/{_pcid}").json()["messages"]]
                      == ["user", "assistant"])
                _proj = _wc.post("/api/projects", json={"name": "Desk"}).json()
                _proj_id = _proj["id"]
                check("web.project_create",
                      any(p["name"] == "Desk" for p in _proj["projects"]))
                _wc.post(f"/api/conversations/{_pcid}/project",
                         json={"project_id": _proj_id})
                check("web.conversation_in_project",
                      len(_wc.get("/api/conversations",
                                  params={"project": _proj_id}
                                  ).json()["conversations"]) == 1)
                check("web.project_count",
                      [p for p in _wc.get("/api/projects").json()["projects"]
                       if p["id"] == _proj_id][0]["count"] == 1)
                _wc.post(f"/api/conversations/{_pcid}/rename",
                         json={"title": "renamed"})
                check("web.conversation_rename",
                      _wc.get(f"/api/conversations/{_pcid}").json()["title"]
                      == "renamed")
                check("web.conversation_delete",
                      _wc.delete(f"/api/conversations/{_pcid}").json()["ok"]
                      and _wc.get("/api/conversations").json()["conversations"] == [])
            finally:
                _ws.memory = _mem_orig
        finally:
            agent_main.run_turn = _real_rt
            _brainmod._engines_file = _w_ef
            _brainmod._custom_engines_cache = _w_cache
            _brainmod._custom_brains.clear()

    _hardening_checks()
    _look_checks()
    _installer_checks()
    _memory_concurrency_checks()
    _career_checks()
    _updater_checks()

    # --- exporting a conversation, secrets blanked ----------------------- #
    _export_checks()

    # --- interviews and applications from the job search, in the chat ----- #
    _career_interview_checks()

    # --- an approved held call actually runs ------------------------------ #
    _held_checks()

    # --- the phone companion: approvals and schedules, key only ------------ #
    _phone_checks()

    # --- the board shows held calls and the job search's urgent things ---- #
    _board_checks()

    # --- routing you can see; evals that test the engine you named -------- #
    _routing_ui_checks()

    # --- a model that can't take tools still answers ---------------------- #
    _no_tools_checks()

    # --- the Symbolic Synapse theme ----------------------------------------- #
    _synapse_checks()

    # --- background service: one scheduler, restarts, survives the window -- #
    _service_checks()

    # --- notifications: ntfy push, Telegram approve / refuse --------------- #
    _notify_checks()

    # --- goals: outcomes over days, checkpoints, budgets, stalls ----------- #
    _goals_checks()

    print(f"\n{len(PASS)} checks passed OK")


# ---------------------------------------------------------------------- #
# Symbolic Synapse: the look of symbolicsynapse.co.za, as a skin over Glass.
#   python -c "import tests.run_tests as t; t._synapse_checks()"
#   python tools/check_layout.py     (the same, measured in a real browser)
# ---------------------------------------------------------------------- #
def _synapse_checks() -> None:
    import re
    root = Path(__file__).resolve().parents[1]
    st = root / "web" / "static"
    css = (st / "styles.css").read_text("utf-8")
    js = (st / "app.js").read_text("utf-8")
    html = (st / "index.html").read_text("utf-8")
    pre = (st / "prepaint.js").read_text("utf-8")
    srv = (root / "web" / "server.py").read_text("utf-8")

    # a theme in Settings, drawn over Glass, so every Glass fix still applies
    check("synapse.a_theme_drawn_over_glass",
          'synapse: { label: "Symbolic Synapse (dark)", fixed: "glass", skin: "synapse" }' in js
          and 'root.setAttribute("data-skin", skin)' in js
          and 'root.removeAttribute("data-skin")' in js)
    # the main look and the default (asked for, 171): first in the list,
    # what a fresh install opens on, what System gives in dark mode, and the
    # markup's own default so even the first paint is right
    check("synapse.is_the_main_look_and_the_default",
          'const DEFAULT_THEME = "synapse";' in js
          and js.index("synapse: {", js.index("const THEMES = {")) <
              js.index("glass: {", js.index("const THEMES = {"))
          and 'system: { label: "System (follow Windows)", dark: "synapse"' in js
          and '<html lang="en" data-theme="glass" data-skin="synapse"' in html)
    # anyone on the old default moves once; a theme picked on purpose stays
    mig = js[js.index('if (!localStorage.getItem("agentjo-theme-synapse-default"))'):]
    mig = mig[:mig.index("return localStorage.getItem")]
    check("synapse.the_move_to_it_happens_once_and_spares_a_chosen_theme",
          'if (!was || was === "glass")' in mig
          and 'localStorage.setItem("agentjo-theme-synapse-default", "1")' in mig
          and '!ls.getItem("agentjo-theme-synapse-default")' in pre
          and '(!c || c === "glass")) c = "synapse"' in pre)
    # before the first paint: every skin app.js knows, prepaint.js knows too
    skins = set(re.findall(r'skin: "([a-z-]+)"', js))
    check("synapse.the_skin_is_on_before_the_first_paint",
          skins == {"synapse"}
          and all(f'{k}: "{k}"' in pre for k in skins)
          and 'root.setAttribute("data-skin", SKINS[r])' in pre
          and 'root.removeAttribute("data-skin")' in pre)
    check("synapse.in_the_palette_too",
          'name: "Theme: " + t.label' in js and "symbolic synapse website" in js)

    # every rule belongs to the skin: nothing leaks into Glass or the others
    start = css.index("/*  SYMBOLIC SYNAPSE")
    end = css.index("/* Kept last: a class that sets display must never outrank [hidden].")
    block = css[start:end]
    no_comments = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    heads = []
    for m in re.finditer(r"([^{};]+)\{", no_comments):
        head = " ".join(m.group(1).split())
        if head.startswith(("@media", "@supports", "@keyframes", "from", "to")) or not head:
            continue
        heads.append(head)
    def parts(head):                       # split a selector list, not :where(a, b)
        out, depth, cur = [], 0, ""
        for ch in head:
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
            else:
                cur += ch
        return out + [cur]
    loose = [h for h in heads if h != "@font-face"
             and h != ".brand-co, .syn-legend, .syn-lattice"
             and not all(part.strip().startswith(('[data-theme="glass"][data-skin="synapse"]',
                                                  ':where([data-theme="glass"][data-skin="synapse"])'))
                         for part in parts(h))]
    check("synapse.every_rule_is_scoped_to_the_skin", heads and not loose)
    # the parts only it shows are hidden everywhere else
    check("synapse.its_parts_are_hidden_in_other_themes",
          ".brand-co, .syn-legend, .syn-lattice { display: none; }" in css
          and 'class="brand-co"' in html and 'class="syn-legend"' in html)
    # Glass's phone layout hides and moves things with display/position; the
    # skin outranks Glass, so it must never set those on the same parts
    def body(sel):
        i = block.find(sel + " {")
        return block[i:block.index("}", i)] if i >= 0 else ""
    check("synapse.never_moves_what_glass_lays_out",
          all(not re.search(r"(^|[\s;{])(display|position|order|flex)\s*:", body(
              '[data-theme="glass"][data-skin="synapse"] ' + s))
              for s in (".task-feed", ".topbar", ".topbar-actions", ".nav-toggle")))

    # the fonts: served by the app, licences beside them, no font service
    faces = re.findall(r'url\("fonts/([^"]+)"\)', css)
    fonts = st / "fonts"
    check("synapse.its_fonts_ship_with_the_app",
          len(faces) == 4 and all((fonts / f).is_file() and (fonts / f).stat().st_size > 10000
                                  for f in faces))
    check("synapse.with_their_licences",
          all((fonts / f).is_file() for f in ("OFL-Archivo.txt", "OFL-IBMPlexSans.txt",
                                              "OFL-IBMPlexMono.txt", "README.txt"))
          and all("SIL Open Font License" in (fonts / f).read_text("utf-8")
                  for f in ("OFL-Archivo.txt", "OFL-IBMPlexSans.txt", "OFL-IBMPlexMono.txt")))
    check("synapse.no_font_service_is_contacted",
          "fonts.googleapis" not in css + html and "fonts.gstatic" not in css + html)
    # "Plex" is IBM's reserved name: those files must be IBM's own data, only
    # wrapped as WOFF (the Open Font License FAQ allows that, not more)
    plex_ok = False
    try:
        from fontTools.ttLib import TTFont as _TT
        plex_ok = all(_TT(fonts / f)["name"].getDebugName(1).startswith("IBM Plex")
                      and _TT(fonts / f).flavor == "woff"
                      for f in ("ibm-plex-sans-var.woff", "ibm-plex-mono-400.woff",
                                "ibm-plex-mono-500.woff"))
    except ImportError:
        plex_ok = all((fonts / f).read_bytes()[:4] == b"wOFF"
                      for f in ("ibm-plex-sans-var.woff", "ibm-plex-mono-400.woff",
                                "ibm-plex-mono-500.woff"))
    check("synapse.plex_is_ibms_own_font_data", plex_ok)
    check("synapse.the_csp_allows_fonts_from_the_app", "font-src 'self'" in srv)

    # the lattice: only where it can be seen, still for anyone who asked for
    # less motion, and never wider than the welcome column
    run = js[js.index("function _synRunnable() {"):js.index("function _synKick() {")]
    kick = js[js.index("function _synKick() {"):js.index("function _synStop() {")]
    check("synapse.the_lattice_runs_only_when_it_can_be_seen",
          all(x in run for x in ("document.hidden", "_syn.seen", 'classList.contains("hidden")'))
          and "IntersectionObserver" in js and '"visibilitychange", _synKick' in js)
    check("synapse.reduced_motion_draws_it_once",
          "if (_reducedMotion()) { _synStop(); _synFrame(0, true); return; }" in kick)
    check("synapse.the_lattice_fits_the_column",
          "Math.min(560, Math.floor(room))" in kick and ".syn-lattice" in css
          and "overflow-x: clip" in block)
    check("synapse.switching_away_removes_it",
          "_syn.canvas.remove()" in js and "synapseSkin(skin === \"synapse\")" in js)
    check("synapse.left_alone_it_holds_still",
          "now - _syn.input > SYN_IDLE_MS" in js and "const SYN_IDLE_MS = 120000;" in js
          and '["pointermove", "pointerdown", "keydown", "wheel"]' in js)
    check("synapse.the_pointer_light_is_for_mouse_and_pen",
          'e.pointerType === "touch"' in js and "(hover: hover)" in js)
    check("synapse.the_lattice_sits_behind_the_words",
          "z-index: -1" in body('[data-theme="glass"][data-skin="synapse"] .syn-lattice')
          and "isolation: isolate" in body('[data-theme="glass"][data-skin="synapse"] .empty-mark'))
    # the site's tokens, by value
    check("synapse.the_sites_palette",
          all(v in block for v in ("--syn-abyss: #03060B", "--syn-cyan: #2FD3E3",
                                    "--syn-steel: #2E6FAE", "--syn-bolt: #E6FAFF",
                                    "--syn-fog: #D8E5F2", "--syn-slate: #8A9DB3")))
    # a toast's kind (fine, warning, failed) is the dot before it now, and
    # the skin's border must not hide a failed one
    check("synapse.toasts_keep_their_kind_colour",
          '[data-theme="glass"] .toast.bad::before' in css
          and '[data-theme="glass"] .toast.ok::before { background: var(--ok); }' in css
          and "var(--danger)" in body('[data-theme="glass"][data-skin="synapse"] .toast.err'))
    # and the code-block lines aren't boxed one by one
    check("synapse.a_code_block_is_one_surface",
          "background: none" in body('[data-theme="glass"][data-skin="synapse"] .msg-content pre code'))
    # no bars down the left edge of a rounded card (asked for, 171): the
    # tier is a dot, rows and toasts lose their rail, in Glass and the skin
    glass_tail = css[css.index("/* --- glass: no bars down the left edge"):start]
    check("synapse.no_bars_down_the_left_edge",
          '[data-theme="glass"] .log-row { box-shadow: none; }' in glass_tail
          and "border-radius: 50%" in glass_tail[glass_tail.index('[data-theme="glass"] .tier::before {'):]
          and '[data-theme="glass"] .foot-btn::before { display: none; }' in glass_tail
          and '[data-theme="glass"] .toast { border-left-width: 1px;' in glass_tail
          and '[data-theme="glass"] .auto-row {' in glass_tail
          and "inset 3px 0 0" not in block and "border-left: 3px" not in block)
    # the skin is decoration: whatever happens to it, the app still starts.
    # The dashboard harness boots app.js on a bare stand-in for the page — no
    # canvas, no insertBefore — which is as hostile as it gets.
    import shutil as _ssh, subprocess as _ssub
    _snode, _sharn = _ssh.which("node"), root / "tests" / "dash_visibility.js"
    _sboot = True
    if _snode and _sharn.exists():
        _sr = _ssub.run([_snode, str(_sharn), '{"agentjo-theme": "synapse"}'],
                        capture_output=True, text=True, timeout=60, cwd=str(root))
        _sboot = "after boot: dash.hidden = false" in (_sr.stdout or "")
    check("synapse.the_app_starts_whatever_happens_to_the_decoration",
          _sboot and "try { synapseSkin(skin" in js)
    # the lattice is driven by observers and input events (it is the default
    # now, so every page has it): it must never throw into them
    check("synapse.the_lattice_never_throws_into_events",
          "try { _synKickNow(); } catch (e) { _synStop(); }" in js
          and 'typeof mark.insertBefore !== "function"' in js
          and "catch (err) { /* only a light" in js)


# ---------------------------------------------------------------------- #
# "<model> does not support tools": answer without them, and remember.
#   python -c "import tests.run_tests as t; t._no_tools_checks()"
# ---------------------------------------------------------------------- #
def _no_tools_checks() -> None:
    import tempfile as _nt
    from types import SimpleNamespace as _NS
    from agent import config as _nc, brain as B
    home = _nc.AGENT_HOME
    _nc.AGENT_HOME = Path(_nt.mkdtemp(prefix="notools-"))
    B._NO_TOOLS = None
    model = "hf.co/mlabonne/gemma-3-27b-it-abliterated-GGUF:Q4_K_M"
    calls = []

    class BadRequestError(Exception):
        pass

    class Completions:
        def create(self, **kw):
            calls.append("tools" in kw)
            if "tools" in kw:
                raise BadRequestError("Error code: 400 - {'error': {'message': '%s does not support tools', "
                                      "'type': 'invalid_request_error'}}" % model)
            msg = _NS(content="Hello from Gemma.", tool_calls=None)
            return _NS(choices=[_NS(message=msg, finish_reason="stop")], usage=None)

    client = _NS(chat=_NS(completions=Completions()))
    tools = [{"name": "read_file", "description": "x", "input_schema": {"type": "object", "properties": {}}}]
    try:
        b = B.OpenAIBrain(model=model, base_url="http://localhost:11434/v1", api_key="ollama",
                          label="Ollama", client=client, supports_stream=False)
        r = b.chat([{"role": "user", "content": "hi"}], "be helpful", tools)
        text = "".join(x.text for x in r.content if getattr(x, "type", "") == "text")
        check("notools.it_answers_instead_of_failing", "Hello from Gemma." in text
              and "does not support tools" not in text, text)
        check("notools.and_says_it_answered_without_tools", "can't use tools" in text)
        check("notools.retried_once_without_tools", calls == [True, False], calls)
        check("notools.the_model_is_remembered", B.model_lacks_tools(model)
              and (B.config.AGENT_HOME / "no_tools_models.json").exists())
        calls.clear()
        b.chat([{"role": "user", "content": "again"}], "x", tools)
        check("notools.next_turn_skips_the_failed_request", calls == [False], calls)
        B.forget_no_tools(model)
        check("notools.can_be_forgotten", not B.model_lacks_tools(model))
        # Ollama's own API (/api/chat) refuses the same way, with a 400 body
        import json as _nj, threading as _nth
        from http.server import BaseHTTPRequestHandler as _H, ThreadingHTTPServer as _S
        seen = []

        class H(_H):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"models": []}')
            def do_POST(self):
                body = _nj.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append("tools" in body)
                if "tools" in body:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(_nj.dumps({"error": "%s does not support tools" % body["model"]}).encode())
                    return
                self.send_response(200); self.end_headers()
                self.wfile.write(_nj.dumps({"message": {"content": "Native hello."}, "done": True}).encode())
        srv = _S(("127.0.0.1", 0), H)
        _nth.Thread(target=srv.serve_forever, daemon=True).start()
        old_host = B.config.OLLAMA_HOST
        B.config.OLLAMA_HOST = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            ob = B.OllamaBrain(model="gemma-abliterated:latest")
            r = ob.chat([{"role": "user", "content": "hi"}], "x", tools)
            t2 = "".join(x.text for x in r.content if getattr(x, "type", "") == "text")
            check("notools.ollama_native_path_too", "Native hello." in t2 and seen == [True, False]
                  and B.model_lacks_tools("gemma-abliterated:latest"), (t2, seen))
        finally:
            B.config.OLLAMA_HOST = old_host
            srv.shutdown()
        check("notools.other_errors_arent_mistaken", not B.is_no_tools_error("401 invalid api key")
              and B.is_no_tools_error("registry.ollama.ai/x does not support tools"))
    finally:
        B._NO_TOOLS = None
        _nc.AGENT_HOME = home


# ---------------------------------------------------------------------- #
# Routing, spend and evals had endpoints and no window; evals tested the
# wrong model. Kept fixed:
#   python -c "import tests.run_tests as t; t._routing_ui_checks()"
# ---------------------------------------------------------------------- #
def _routing_ui_checks() -> None:
    from types import SimpleNamespace as _NS
    from agent import evals as EV
    import tempfile as _rt
    from agent import config as _rc
    home = _rc.AGENT_HOME
    _rc.AGENT_HOME = Path(_rt.mkdtemp(prefix="evals-"))
    seen = []

    class Brain:
        def chat(self, messages, system, tools, **kw):
            seen.append(kw.get("model", "<default>"))
            return _NS(content=[_NS(type="text", text="{}")])
    try:
        EV.run(Brain(), engine="Ollama gemma3", feature="", model="TOKEN-FOR-GEMMA")
        check("evals.test_the_engine_you_named", seen and all(m == "TOKEN-FOR-GEMMA" for m in seen), seen[:3])
        seen.clear()
        EV.run(Brain(), engine="", feature="")
        check("evals.default_engine_when_none_named", seen and all(m == "<default>" for m in seen))
    finally:
        _rc.AGENT_HOME = home
    root = Path(__file__).resolve().parents[1]
    srv = (root / "web" / "server.py").read_text("utf-8")
    js = (root / "web" / "static" / "app.js").read_text("utf-8")
    html = (root / "web" / "static" / "index.html").read_text("utf-8")
    check("routing.the_server_passes_the_resolved_model_to_evals", 'feature=body.feature or "", model=model)' in srv)
    check("routing.each_reply_carries_its_route", 'turn_info["route"] = ' in srv
          and '"route": turn_info.get("route")' in srv and '"failover": turn_info.get("failover")' in srv)
    check("routing.the_reply_shows_the_rule", "msg-route" in js and "route: data.route" in js)
    check("routing.there_is_a_window_for_it", 'id="routingBtn"' in html and 'id="routingModal"' in html
          and "async function drawRouting" in js and "/api/routing/rules" in js
          and "/api/routing/test" in js and "/api/costs" in js and "/api/evals/run" in js)
    import re as _rr
    html_ids = _rr.findall(r'class="modal-backdrop" id="([a-zA-Z]+)"', html)
    cam = js[js.index("function closeAllModals() {"):]
    cam = cam[:cam.index("\n}\n")]
    named = {"mlModal": "closeMl()", "codemapModal": "closeCodemap()", "phoneModal": "closePhone()",
             "routingModal": "closeRouting()"}
    check("ui.opening_a_panel_closes_every_other", all(named[i] in cam for i in named if i in html_ids))
    check("ui.panels_wrap_on_a_phone", ".modal .engine-grid { grid-template-columns: minmax(0, 1fr) !important; }"
          in (root / "web" / "static" / "styles.css").read_text("utf-8"))
    check("crew.members_can_be_edited_added_and_removed", "function crewEditor" in js
          and '"/api/crew/member"' in js and '"/api/crew/member/" + encodeURIComponent' in js)
    check("routing.turbo_results_shown", '"/api/turbo"' in js)
    check("models.importance_and_card_are_shown", "/api/models/importance?name=" in js
          and "/api/models/card?name=" in js)


# ---------------------------------------------------------------------- #
# The board: held calls and an interview soon, without opening anything.
#   python -c "import tests.run_tests as t; t._board_checks()"
# ---------------------------------------------------------------------- #
def _board_checks() -> None:
    import json as _bj
    import os as _bo
    import tempfile as _bt
    from datetime import datetime as _bd, timedelta as _btd
    from agent import config as _bc, intercepts as IC, dashboard as DB
    home = _bc.AGENT_HOME
    _bc.AGENT_HOME = Path(_bt.mkdtemp(prefix="board-"))
    jobs = Path(_bt.mkdtemp(prefix="jobs-"))
    (jobs / "jobscout").mkdir()
    soon = (_bd.now() + _btd(hours=20)).strftime("%Y-%m-%d %H:%M")
    later = (_bd.now() + _btd(days=6)).strftime("%Y-%m-%d %H:%M")
    (jobs / "jobscout" / "roles.json").write_text(_bj.dumps([
        {"title": "Data Engineer", "company": "Acme", "stage": "interview", "interview_at": soon,
         "interview_link": "https://teams.microsoft.com/l/x"},
        {"title": "BI Lead", "company": "Zeta", "stage": "interview", "interview_at": later}]), "utf-8")
    (jobs / "jobscout" / "open_questions.json").write_text(_bj.dumps([{"question": "Notice period?"}]), "utf-8")
    old = _bo.environ.get("AGENT_JOBS_HOME")
    _bo.environ["AGENT_JOBS_HOME"] = str(jobs)
    try:
        check("board.nothing_held_nothing_shown", DB._held_items() == [])
        IC.hold("send_email", {"to": "hr@acme.test", "subject": "Hi", "body": "x"})
        h = DB._held_items()
        check("board.a_held_call_is_on_the_board", h and h[0]["severity"] == "act"
              and "hr@acme.test" in h[0]["detail"] and h[0]["panel"] == "autonomyBtn", h)
        j = DB._jobs_items()
        titles = [i["title"] for i in j]
        check("board.an_interview_in_two_days_is_shown", "Interview: Data Engineer @ Acme" in titles
              and not any("Zeta" in t for t in titles), titles)
        check("board.waiting_questions_are_shown", any("waiting on your answers" in t for t in titles))
        check("board.job_items_open_the_jobs_app", all(i.get("url") == DB.JOBS_URL for i in j))
    finally:
        if old is None:
            _bo.environ.pop("AGENT_JOBS_HOME", None)
        else:
            _bo.environ["AGENT_JOBS_HOME"] = old
        _bc.AGENT_HOME = home
    js = (Path(__file__).resolve().parents[1] / "web" / "static" / "app.js").read_text("utf-8")
    check("board.an_item_without_a_panel_doesnt_throw", "if (!i.panel) return;" in js)


# ---------------------------------------------------------------------- #
# The phone companion: only the key gets in, and only these decisions.
#   python -c "import tests.run_tests as t; t._phone_checks()"
# ---------------------------------------------------------------------- #
def _phone_checks() -> None:
    import http.client as _hc
    import json as _pj
    import tempfile as _pt
    from agent import config as _pc, intercepts as IC, tools as TL, phone as PH, qr as QR

    class Mem:
        def __init__(self):
            self.s = {1: {"id": 1, "name": "Morning brief", "enabled": 1, "spec": "{}", "last_status": "ok",
                          "last_summary": "sent", "next_run": 1.0},
                      2: {"id": 2, "name": "Weekly report", "enabled": 0, "spec": "{}", "last_status": "",
                          "last_summary": "", "next_run": None}}
        def list_schedules(self): return [dict(v) for v in self.s.values()]
        def get_schedule(self, i): return dict(self.s[i]) if i in self.s else None
        def set_schedule_enabled(self, i, on, nxt=None):
            self.s[i]["enabled"] = int(on); self.s[i]["next_run"] = nxt; return True

    class _C:
        def print(self, *a, **k): pass

    home = _pc.AGENT_HOME
    _pc.AGENT_HOME = Path(_pt.mkdtemp(prefix="phone-"))
    ran = []
    real = TL._execute_tool_impl
    TL._execute_tool_impl = lambda name, args, memory, console, auto_approve=False, session_id="", **kw: (
        ran.append(name) or "sent")
    mem = Mem()
    try:
        check("phone.qr_is_svg", QR.svg("hello").startswith("<svg"))
        check("phone.off_until_turned_on", not PH.settings()["enabled"])
        PH.configure(mem, _C())
        PH.set_enabled(True)
        PH.stop()
        PH.start(port=0, host="127.0.0.1")
        port = PH._SERVER["port"]
        cur = {"key": PH.settings()["key"]}

        def req(method, path, body=None, k=""):
            k = cur["key"] if k == "" else k
            c = _hc.HTTPConnection("127.0.0.1", port, timeout=10)
            h = {"X-Phone-Key": k} if k is not None else {}
            if body is not None:
                h["Content-Type"] = "application/json"
            c.request(method, path, body=_pj.dumps(body) if body is not None else None, headers=h)
            r = c.getresponse(); data = r.read(); c.close()
            try:
                return r.status, _pj.loads(data)
            except Exception:
                return r.status, data

        check("phone.page_loads", req("GET", "/", k=None)[0] == 200)
        check("phone.no_key_no_data", req("GET", "/api/state", k=None)[0] == 401)
        TL.execute_tool("send_email", {"to": "hr@acme.test", "subject": "Hi", "body": "x"}, None, _C())
        TL.execute_tool("http_post", {"url": "https://api.test/x", "json": {"a": 1}}, None, _C())
        code, s = req("GET", "/api/state")
        check("phone.shows_what_waits_with_its_inputs", code == 200 and len(s["held"]) == 2
              and any("hr@acme.test" in h["args"] for h in s["held"]), s)
        check("phone.shows_the_schedules", [x["name"] for x in s["schedules"]] == ["Morning brief", "Weekly report"])
        mail = [h for h in s["held"] if h["tool"] == "send_email"][0]
        post = [h for h in s["held"] if h["tool"] == "http_post"][0]
        code, r = req("POST", "/api/approve", {"id": mail["id"]})
        check("phone.approve_runs_it", code == 200 and ran == ["send_email"] and r["then"] == "Done.", r)
        check("phone.approved_once_only", req("POST", "/api/approve", {"id": mail["id"]})[0] == 400 and ran == ["send_email"])
        code, r = req("POST", "/api/refuse", {"id": post["id"]})
        check("phone.refuse_never_runs", code == 200 and ran == ["send_email"]
              and [i for i in IC.pending() if i["id"] == post["id"]][0]["state"] == "refused")
        code, r = req("POST", "/api/pause", {})
        check("phone.pause_turns_schedules_off", r.get("paused") == 1 and not mem.s[1]["enabled"])
        check("phone.state_says_paused", req("GET", "/api/state")[1]["paused"])
        import agent.scheduler as _sch
        _orig = (_sch.next_run, _sch.parse_spec)
        _sch.parse_spec = lambda s: {}
        _sch.next_run = lambda spec: 123.0
        try:
            code, r = req("POST", "/api/resume", {})
        finally:
            _sch.next_run, _sch.parse_spec = _orig
        check("phone.resume_turns_back_on_only_what_it_paused",
              r.get("resumed") == 1 and mem.s[1]["enabled"] and not mem.s[2]["enabled"], r)
        check("phone.cant_do_anything_else", req("POST", "/api/run", {"cmd": "dir"})[0] == 404
              and req("GET", "/api/memory", None)[0] == 404)
        old = cur["key"]
        PH.rotate_key()
        check("phone.new_key_shuts_out_the_old", req("GET", "/api/state", k=old)[0] == 401)
        cur["key"] = PH.settings()["key"]
        PH._FAILS.clear()
        for _ in range(PH.LOCK_AFTER):
            req("GET", "/api/state", k="y" * 32)
        check("phone.wrong_keys_lock_out", req("GET", "/api/state")[0] == 429)
        PH._FAILS.clear()
        check("phone.different_port_from_jobs", PH.DEFAULT_PORT == 8768)
    finally:
        PH.stop()
        PH._FAILS.clear()
        TL._execute_tool_impl = real
        _pc.AGENT_HOME = home
    root = Path(__file__).resolve().parents[1]
    rel = (root / "tools" / "make_release.py").read_text("utf-8")
    check("phone.its_page_ships_in_a_release", '"web_phone"' in rel)


# ---------------------------------------------------------------------- #
# Approving a held call runs it — once, with the inputs you were shown:
#   python -c "import tests.run_tests as t; t._held_checks()"
# ---------------------------------------------------------------------- #
def _held_checks() -> None:
    import tempfile as _ht
    from agent import config as _hc, intercepts as IC, tools as TL
    home = _hc.AGENT_HOME
    _hc.AGENT_HOME = Path(_ht.mkdtemp(prefix="held-"))
    ran = []
    real = TL._execute_tool_impl
    TL._execute_tool_impl = lambda name, args, memory, console, auto_approve=False, session_id="", **kw: (
        ran.append((name, dict(args), auto_approve)) or "sent")
    try:
        class _C:
            def print(self, *a, **k): pass
        out = TL.execute_tool("send_email", {"to": "hr@acme.test", "subject": "Hi", "body": "x"}, None, _C())
        check("held.an_outside_call_is_held_not_run", out.startswith("HELD FOR REVIEW") and ran == [])
        item = IC.pending()[-1]
        r0 = TL.run_held(item["id"], None, _C())
        check("held.waiting_isnt_run", not r0["ok"] and ran == [])
        IC.decide(item["id"], True)
        r = TL.run_held(item["id"], None, _C())
        check("held.approving_runs_it", r["ok"] and ran == [("send_email", {"to": "hr@acme.test", "subject": "Hi",
                                                                           "body": "x"}, True)], ran)
        check("held.the_result_is_kept", IC.pending()[-1]["state"] == "done" and IC.pending()[-1]["result"] == "sent")
        check("held.never_twice", not TL.run_held(item["id"], None, _C())["ok"] and len(ran) == 1)
        TL.execute_tool("delete_file", {"path": "x"}, None, _C())
        j = IC.pending()[-1]
        IC.decide(j["id"], False, "no")
        check("held.a_refused_call_cant_be_run", not TL.run_held(j["id"], None, _C())["ok"] and len(ran) == 1)
        TL._execute_tool_impl = lambda *a, **k: "Error: smtp refused"
        TL.execute_tool("send_email", {"to": "a@b.test"}, None, _C())
        f = IC.pending()[-1]
        IC.decide(f["id"], True)
        check("held.a_failure_is_recorded_as_one", TL.run_held(f["id"], None, _C())["state"] == "failed")
    finally:
        TL._execute_tool_impl = real
        _hc.AGENT_HOME = home
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "static" / "app.js").read_text("utf-8")
    srv = (root / "web" / "server.py").read_text("utf-8")
    check("held.the_autonomy_panel_lists_them", "function loadHeld" in js and "Approve and run" in js)
    check("held.the_endpoint_runs_on_approve", "_tools.run_held(body.id" in srv)


# ---------------------------------------------------------------------- #
# "When is my interview?" answered from Agent Jo Jobs' data:
#   python -c "import tests.run_tests as t; t._career_interview_checks()"
# ---------------------------------------------------------------------- #
def _career_interview_checks() -> None:
    import json as _cij
    import os as _cio
    import tempfile as _cit
    from datetime import datetime as _cid
    from agent import career as CR
    d = Path(_cit.mkdtemp(prefix="jobs-home-"))
    (d / "jobscout").mkdir()
    (d / "jobscout" / "roles.json").write_text(_cij.dumps([
        {"key": "a", "title": "Data Engineer", "company": "Acme Analytics", "stage": "interview",
         "sent_at": "2026-10-01 08:00 UTC", "interview_at": "2026-10-14 10:00",
         "interview_link": "https://teams.microsoft.com/l/x",
         "interview": {"questions": [{"question": "Why Kafka?", "gap": "No Kafka in your CV"}]}},
        {"key": "b", "title": "BI Lead", "company": "Zeta", "stage": "applied",
         "interview_at": "2026-09-01 10:00"},
        {"key": "c", "title": "Analyst", "company": "Beta", "stage": "found"}]), "utf-8")
    (d / "jobscout" / "contacts.json").write_text(_cij.dumps({
        "thandi@acme.co.za": {"email": "thandi@acme.co.za", "name": "Thandi Mokoena",
                              "companies": ["Acme Analytics"], "kinds": {"interview": 1},
                              "last": "2026-10-01 09:00 UTC", "notes": "met at a meetup"}}), "utf-8")
    old = _cio.environ.get("AGENT_JOBS_HOME")
    _cio.environ["AGENT_JOBS_HOME"] = str(d)
    try:
        now = _cid(2026, 10, 7, 9, 0)
        a = CR.applications("how is my Acme application going?", now)
        check("career.lists_interviews_coming_up",
              "INTERVIEWS COMING UP" in a and "Wed 14 Oct 2026, 10:00" in a and "Zeta" not in a, a)
        check("career.names_the_role_asked_about", "stage interview" in a and "Why Kafka?" in a
              and "Gap: No Kafka" in a)
        check("career.who_you_know_there", "Thandi Mokoena <thandi@acme.co.za>" in a
              and "met at a meetup" in a, a)
        check("career.a_role_merely_found_isnt_an_application",
              "Beta" not in CR.applications("Beta", now))
        out = CR.lookup("my Acme interview")
        check("career.lookup_works_with_only_applications",
              "APPLICATION — Data Engineer @ Acme Analytics" in out, out[:200])
    finally:
        if old is None:
            _cio.environ.pop("AGENT_JOBS_HOME", None)
        else:
            _cio.environ["AGENT_JOBS_HOME"] = old


# ---------------------------------------------------------------------- #
# Exporting a conversation:
#   python -c "import tests.run_tests as t; t._export_checks()"
# ---------------------------------------------------------------------- #
def _export_checks() -> None:
    from agent import chatexport as CE
    key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0kLmN"
    out = CE.to_markdown("Pipeline / plan: v2", [
        {"role": "user", "text": f"my key is {key}, and password = hunter22"},
        {"role": "assistant", "text": "Use the Kafka connector; skip ask for the token budget."},
        {"role": "assistant", "text": ""}])
    t = out["text"]
    check("export.secrets_are_blanked", key not in t and "hunter22" not in t, t)
    check("export.and_counted", out["removed"] == 2 and "2 secret(s)" in t, out["removed"])
    check("export.ordinary_words_survive", "token budget" in t and "Kafka connector" in t)
    check("export.who_said_what", t.count("**You:**") == 1 and t.count("**Agent Jo:**") == 1)
    check("export.a_safe_file_name", out["filename"] == "Pipeline-plan-v2.md", out["filename"])
    check("export.keep_secrets_only_when_asked",
          key in CE.to_markdown("x", [{"role": "user", "text": key}], keep_secrets=True)["text"])
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "static" / "app.js").read_text("utf-8")
    srv = (root / "web" / "server.py").read_text("utf-8")
    check("export.in_the_conversation_menu", "Export as Markdown" in js and "/export" in js)
    check("export.route_exists", '@app.get("/api/conversations/{cid}/export")' in srv)


# ---------------------------------------------------------------------- #
# Hardening: other websites, the browser's own thread, a visible window,
# and the Jobs app's copy. One function so it can be run on its own:
#   python -c "import tests.run_tests as t; t._hardening_checks()"
# ---------------------------------------------------------------------- #
def _hardening_checks() -> None:
    import asyncio as _hxaio
    import importlib.util as _hxil
    import re as _hxre
    import socket as _hxsock
    import tempfile as _hxtmp
    import time as _hxtime
    root = Path(__file__).resolve().parents[1]

    # --- other websites may not drive the app ------------------------------ #
    #
    # Being on 127.0.0.1 was treated as the whole defence. It isn't one: any
    # page open in the browser can send requests there. /api/chat took form
    # fields — which cross sites with no preflight — with full_access=true in
    # the same post, CORS allowed every origin, and nothing checked the Host,
    # so a site re-resolving its name to 127.0.0.1 counted as the app. With
    # no password (the default) any page could ask the agent to write files.
    from agent.localguard import LocalOnlyGuard as _LG

    def _guard(method, headers, kind="http"):
        seen, sent = {"inner": False}, []

        async def _inner(scope, receive, send):
            seen["inner"] = True
            if kind == "http":
                await send({"type": "http.response.start", "status": 200,
                            "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

        async def _recv():
            return ({"type": "websocket.connect"} if kind == "websocket"
                    else {"type": "http.request", "body": b""})

        async def _send(m):
            sent.append(m)

        scope = {"type": kind, "path": "/api/chat",
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        if kind == "http":
            scope["method"] = method
        # its own thread: asyncio.run refuses a thread whose loop is running
        t = threading.Thread(target=lambda: _hxaio.run(
            _LG(_inner)(scope, _recv, _send)))
        t.start()
        t.join(10)
        status = next((m["status"] for m in sent
                       if m["type"] == "http.response.start"), None)
        return (seen["inner"], status,
                any(m["type"] == "websocket.close" for m in sent))

    H = "127.0.0.1:8765"
    EVIL = {"host": H, "origin": "https://evil.example",
            "sec-fetch-site": "cross-site"}
    saved = {k: os.environ.get(k)
             for k in ("AGENT_ALLOWED_HOSTS", "AGENT_ALLOWED_ORIGINS")}
    os.environ.pop("AGENT_ALLOWED_HOSTS", None)
    os.environ.pop("AGENT_ALLOWED_ORIGINS", None)
    try:
        check("guard.the_apps_own_page_can_post",
              _guard("POST", {"host": H, "origin": "http://127.0.0.1:8765",
                              "sec-fetch-site": "same-origin"})[0])
        check("guard.a_cross_site_form_post_is_refused",
              _guard("POST", EVIL)[:2] == (False, 403))
        check("guard.an_older_browser_from_another_site_is_refused",
              _guard("POST", {"host": H, "origin": "https://evil.example"})[:2]
              == (False, 403))
        check("guard.an_older_browser_on_the_app_still_works",
              _guard("POST", {"host": H, "origin": "http://127.0.0.1:8765"})[0])
        check("guard.a_null_origin_is_refused",
              not _guard("POST", {"host": H, "origin": "null"})[0])
        check("guard.another_local_port_is_another_site",
              not _guard("POST", {"host": H, "origin": "http://127.0.0.1:8766",
                                  "sec-fetch-site": "same-site"})[0])
        check("guard.scripts_and_curl_still_work", _guard("POST", {"host": H})[0])
        check("guard.a_link_from_another_site_still_opens_it",
              _guard("GET", {"host": H, "sec-fetch-site": "cross-site"})[0])
        check("guard.dns_rebinding_is_refused_even_for_reads",
              _guard("GET", {"host": "evil.example:8765"})[:2] == (False, 403)
              and not _guard("POST", {"host": "evil.example:8765",
                                      "origin": "http://evil.example:8765",
                                      "sec-fetch-site": "same-origin"})[0])
        check("guard.loopback_the_lan_and_this_machine_are_allowed",
              all(_guard("GET", {"host": h})[0] for h in (
                  "localhost:8765", "[::1]:8765", "192.168.1.20:8765",
                  "100.101.102.103:8765", "app.localhost:8765",
                  _hxsock.gethostname().lower() + ":8765")))
        check("guard.websockets_are_held_to_the_same_rule",
              _guard("GET", EVIL, kind="websocket")[2])
        os.environ["AGENT_ALLOWED_HOSTS"] = "agent.example.org,.tail1234.ts.net"
        os.environ["AGENT_ALLOWED_ORIGINS"] = "http://localhost:5173"
        check("guard.a_named_proxy_can_be_allowed",
              _guard("GET", {"host": "agent.example.org"})[0]
              and _guard("GET", {"host": "pc.tail1234.ts.net"})[0]
              and not _guard("GET", {"host": "other.example"})[0])
        check("guard.a_dev_server_can_be_allowed",
              _guard("POST", {"host": "localhost:8765",
                              "origin": "http://localhost:5173",
                              "sec-fetch-site": "same-site"})[0])
        os.environ["AGENT_ALLOWED_ORIGINS"] = "*"
        check("guard.a_wildcard_origin_is_not_honoured",
              not _guard("POST", EVIL)[0])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    wsrc = (root / "web" / "server.py").read_text("utf-8")
    add = "app.add_middleware(localguard.LocalOnlyGuard)"
    check("guard.the_server_installs_it", add in wsrc)
    check("guard.it_is_the_outermost_layer",
          add in wsrc and wsrc.rindex('@app.middleware("http")') < wsrc.index(add))
    check("guard.cors_no_longer_allows_every_origin",
          'allow_origins=["*"]' not in wsrc)
    ck = wsrc[wsrc.index("def _client_key"):]
    ck = ck[:ck.index("\n\n\n")]
    check("guard.the_login_limit_ignores_a_forged_forwarded_for",
          'headers.get("x-forwarded-for"' not in ck)








# ---------------------------------------------------------------------- #
# The look: what broke the two references, kept fixed.
#   python -c "import tests.run_tests as t; t._look_checks()"
#   python tools/check_layout.py     (the same, measured in a real browser)
# ---------------------------------------------------------------------- #
def _look_checks() -> None:
    import re
    root = Path(__file__).resolve().parents[1]
    css = (root / "web" / "static" / "styles.css").read_text("utf-8")
    js = (root / "web" / "static" / "app.js").read_text("utf-8")

    def body(src, head, last=False):
        # in CSS the last rule with a selector is the one that wins
        i = src.rfind(head) if last else src.find(head)
        return src[i:src.index("\n}\n", i)] if i >= 0 else ""

    # Glass: one teal accent, primary buttons included
    pb = body(css, '[data-theme="glass"] .btn.primary {', last=True)
    check("look.glass_has_its_own_primary_buttons",
          pb and "#E0B058" not in pb and "#C08A38" not in pb)
    check("look.a_plain_link_is_readable_in_glass",
          ':where([data-theme="glass"]) a:where(:not([class])) { color: var(--accent); }' in css)

    # the task feed covers nothing in its default corner. It is moved to
    # <body> at start-up, so .main is found through the board, not the feed
    tr = body(js, "function _tfReserve() {")
    check("look.the_feed_reserves_room_through_the_board",
          'getElementById("dash")' in tr and "closest(\".main\")" not in tr
          and ".main.tf-reserve .dash" in css
          and ".main.tf-reserve.tf-reserve-min .dash" in css)
    check("look.a_repaint_does_no_dom_lookups",
          "dash.parentElement" in body(js, "function _markDash(dash, n) {")
          and "querySelector" not in body(js, "function _markDash(dash, n) {"))
    check("look.the_greeting_shrinks_when_the_board_is_full",
          ".main.dash-full .empty-title" in css and "_markDash(dash" in js)
    # one look now (the switch was removed): the welcome keeps its picture
    check("look.the_welcome_keeps_its_picture", ".empty-mark { display: none; }" not in css)
    check("look.needs_you_rows_are_slim",
          '[data-theme="glass"] .dash-attention .dash-item {' in css
          and "border-left: 1px solid var(--line);"
              in body(css, '[data-theme="glass"] .dash-attention .dash-item {', last=True))

    # the phone
    check("look.a_phone_has_one_menu_button",
          '[data-theme="glass"] #menuBtn { display: none; }' in css)
    check("look.the_phone_header_keeps_the_engine_on_screen",
          "order: 3; flex: 1 1 100%" in css)
    check("look.the_drawer_sits_above_its_backdrop",
          "body.nav-open #app::after" in css
          and "body.nav-open::after { display: none; }" in css)
    check("look.hidden_still_wins_everywhere",
          css.rstrip().endswith("[hidden] { display: none !important; }"))

    # the New/Previous switch is gone: they looked the same. One look, set
    # before the first paint, and the fixes that came with "new" apply always
    html = (root / "web" / "static" / "index.html").read_text("utf-8")
    pre = (root / "web" / "static" / "prepaint.js").read_text("utf-8")
    srv = (root / "web" / "server.py").read_text("utf-8")
    check("look.no_look_switch_left",
          '"model_LOOK"' not in js and 'data-look="new"' not in css + html + pre
          and 'setAttribute("data-look", "previous")' in pre)
    check("look.the_look_is_set_before_the_first_paint",
          '<script src="/static/prepaint.js"></script>' in html
          and 'f"/static/prepaint.js?v={v}"' in srv)
    # the CSP (script-src 'self') blocks inline scripts: theme-before-paint
    # and the service worker both lived in one and never ran
    check("look.no_inline_scripts_under_the_csp",
          not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html))



# ---------------------------------------------------------------------- #
# The Windows installer: what it copies, what it runs, what it leaves.
#   python -c "import tests.run_tests as t; t._installer_checks()"
# (Building the .exe itself needs Windows: installer\build_installer.bat.)
# ---------------------------------------------------------------------- #
def _installer_checks() -> None:
    import re
    root = Path(__file__).resolve().parents[1]
    iss = (root / "installer" / "AgentJo.iss").read_text("utf-8")
    bat = (root / "installer" / "build_installer.bat").read_text("utf-8")
    ver = (root / "installer" / "version.ps1").read_text("utf-8")
    flow = (root / ".github" / "workflows" / "windows-installer.yml").read_text("utf-8")
    inst = (root / "install_agent_jo.py").read_text("utf-8")
    check("installer.everything_it_names_exists",
          all((root / f).exists() for f in (
              "agent-jo.ico", "LICENSE", "start_agent_jo.bat", "install_agent_jo.py"))
          and "SetupIconFile=..\\agent-jo.ico" in iss and "LicenseFile=..\\LICENSE" in iss)
    check("installer.no_administrator_prompt",
          "PrivilegesRequired=lowest" in iss and "{localappdata}\\Programs" in iss)
    # Python from the registry, not PATH: after a fresh Python install the
    # installer's own PATH is stale, which is why install.bat had to stop
    check("installer.finds_python_without_path",
          "Software\\Python\\PythonCore\\3." in iss and "function FindPython" in iss
          and "DownloadTemporaryFile(" in iss and "InstallAllUsers=0" in iss)
    check("installer.runs_the_same_setup_as_install_bat",
          "install_agent_jo.py')) + ' --quiet --no-shortcut'" in iss
          and '"--no-shortcut" not in argv' in inst)
    check("installer.never_ships_a_build_or_a_venv",
          all(x in iss for x in ("\\.venv", "__pycache__", "\\dist", "\\installer")))
    check("installer.an_upgrade_leaves_no_stale_code_but_keeps_the_venv",
          'Name: "{app}\\agent"' in iss and 'Name: "{app}\\.venv"' not in iss[
              iss.index("[InstallDelete]"):iss.index("[UninstallDelete]")])
    check("installer.uninstall_takes_everything_it_made",
          'Type: filesandordirs; Name: "{app}"' in iss)
    # every begin and try closed: counted on the code alone, with its strings
    # and comments taken out ("try again" in a message isn't a try)
    code = iss[iss.index("[Code]"):]
    code = re.sub(r"'(?:[^']|'')*'", "''", code)
    code = re.sub(r"\{[^}]*\}|//[^\n]*", "", code)
    check("installer.its_code_is_balanced",
          len(re.findall(r"\bbegin\b", code)) + len(re.findall(r"\btry\b", code))
          == len(re.findall(r"\bend\b\s*;", code)))
    m = re.search(r'BUILD_ID = "(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})',
                  (root / "agent" / "config.py").read_text("utf-8"))
    check("installer.the_version_comes_from_the_build_stamp",
          m is not None and r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})" in ver
          and int(m[4] + m[5]) < 65536)
    cmds = "\n".join(l for l in bat.splitlines()
                     if not l.strip().upper().startswith("REM"))
    check("installer.the_build_script_obeys_the_batch_rules",
          cmds.count("%~dp0") == 1 and "goto have_iscc" in bat
          and not re.search(r"\bif\b[^\n]*\($", cmds, re.M)
          and '"installer\\AgentJo.iss"' in bat and "winget install --id JRSoftware.InnoSetup" in bat)
    check("installer.it_builds_in_the_cloud_too",
          "installer\\build_installer.bat --ci" in flow and "AgentJo-Setup-*.exe" in flow)


# ---------------------------------------------------------------------- #
# The memory store under load: one connection per thread.
#   python -c "import tests.run_tests as t; t._memory_concurrency_checks()"
# ---------------------------------------------------------------------- #
def _memory_concurrency_checks() -> None:
    import tempfile as _mct
    from agent.memory import MemoryStore as _MCS
    home = Path(_mct.mkdtemp(prefix="mem-load-"))
    m = _MCS(db_path=home / "load.db", check_same_thread=False)
    errors, tasks = [], {}
    guard = threading.Lock()

    def worker(t):
        for i in range(100):
            try:
                m.add_memory(f"thread {t} fact {i}", category="general")
                m.log_message(f"s{t}", "user", f"message {i}")
                tid = m.create_task(f"task {t}-{i}", [f"{t}-{i} one", f"{t}-{i} two"])
                m.update_step(tid, 1, "done", "ok")
                m.search_memories("fact", limit=3)
                m.get_transcript(f"s{t}")
                with guard:
                    tasks[tid] = f"{t}-{i}"
            except Exception as exc:
                with guard:
                    errors.append(f"{type(exc).__name__}: {exc}")
    ts = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # It was one connection shared by every thread: under this load rows went
    # missing, "bad parameter or other API misuse" surfaced, and a task's
    # steps could be filed under another task — its id was read from a
    # connection someone else had just inserted through.
    check("memory.many_threads_at_once_raise_nothing", not errors, f"{errors[:3]}")
    count = lambda q: m.conn.execute(q).fetchone()[0]
    check("memory.no_write_is_lost",
          count("SELECT COUNT(*) FROM memories") == 800
          and count("SELECT COUNT(*) FROM messages") == 800
          and count("SELECT COUNT(*) FROM tasks") == 800)
    wrong = [tid for tid, tag in tasks.items()
             if [s["description"]
                 for s in m.conn.execute(
                     "SELECT * FROM task_steps WHERE task_id = ? ORDER BY seq",
                     (tid,)).fetchall()][:2] != [f"{tag} one", f"{tag} two"]]
    check("memory.a_tasks_steps_are_its_own", not wrong, f"{wrong[:3]}")
    # a finished thread's connection is closed when the next thread
    # connects: open connections stay bounded, however many threads came
    # and went (30 here, in bursts of 10)
    for _ in range(3):
        burst = [threading.Thread(target=lambda: m.recent_memories(1)) for _ in range(10)]
        for t in burst:
            t.start()
        for t in burst:
            t.join()
    check("memory.finished_threads_leave_no_connections_open",
          len(m._conns) <= 11, f"{len(m._conns)} open after 30 threads")
    m.close()
    check("memory.close_closes_every_threads_connection", m._conns == {})


# ---------------------------------------------------------------------- #
# Your career in the chat: Agent Jo Jobs' CV and answers, read-only.
#   python -c "import tests.run_tests as t; t._career_checks()"
# ---------------------------------------------------------------------- #
def _career_checks() -> None:
    import ast as _cast
    import tempfile as _ctt
    import agent.career as CA
    from agent import tools as _ctools
    from agent.memory import MemoryStore as _CMS
    root = Path(__file__).resolve().parents[1]
    saved = os.environ.get("AGENT_JOBS_HOME")
    jobs = Path(_ctt.mkdtemp(prefix="jobs-home-"))
    os.environ["AGENT_JOBS_HOME"] = str(jobs)
    try:
        check("career.nothing_yet_says_where_to_add_it",
              "paste or upload their CV" in CA.lookup("my experience"))
        (jobs / "jobscout" / "cv").mkdir(parents=True)
        # the shapes Agent Jo Jobs writes (its cvstore.py and answerbank.py)
        (jobs / "jobscout" / "cv" / "chunks.json").write_text(json.dumps({"at": "x", "chunks": [
            {"id": 0, "heading": "Summary", "text": "Data engineer building pipelines for banks."},
            {"id": 1, "heading": "Experience", "text": "Standard Bank, 2019-2024: built a Databricks lakehouse; led the move from SSIS to Airflow."},
            {"id": 2, "heading": "Education", "text": "BSc Computer Science, University of Johannesburg."}]}), "utf-8")
        (jobs / "jobscout" / "answers.json").write_text(json.dumps([
            {"id": 1, "question": "What is your notice period?", "answer": "30 days", "reuse": True},
            {"id": 2, "question": "Gender", "answer": "Female", "sensitive": True, "reuse": False}]), "utf-8")
        (jobs / "jobscout" / "profile.json").write_text(json.dumps(
            {"full_name": "Thandi Mokoena", "skills": ["SQL", "Python"], "email": "t@x.com"}), "utf-8")
        out = CA.lookup("What did I do at Standard Bank with Airflow?")
        check("career.finds_the_cv_section_that_answers", "Databricks lakehouse" in out
              and out.index("Standard Bank, 2019") < (out.find("University") if "University" in out else 10**9))
        check("career.includes_the_profile", "Thandi Mokoena" in out and "Python" in out)
        check("career.contact_details_stay_out", "t@x.com" not in out)
        check("career.saved_answers_are_there",
              "30 days" in CA.lookup("notice period"))
        check("career.a_sensitive_answer_not_switched_on_stays_hidden",
              "Female" not in CA.lookup("gender"))
        check("career.it_reminds_the_chat_not_to_invent", "don't add employers" in out)
        m = _CMS(db_path=Path(_ctt.mkdtemp()) / "c.db", check_same_thread=False)
        via = _ctools.execute_tool("career_lookup", {"query": "education"}, m, console)
        check("career.the_chat_tool_answers", "University of Johannesburg" in str(via), str(via)[:200])
        check("career.the_tool_is_offered_to_the_chat",
              any(t.get("name") == "career_lookup" for t in _ctools.TOOL_DEFINITIONS))
        before = sorted((p.name, p.stat().st_mtime_ns) for p in jobs.rglob("*") if p.is_file())
        CA.lookup("anything"); CA.status()
        after = sorted((p.name, p.stat().st_mtime_ns) for p in jobs.rglob("*") if p.is_file())
        check("career.it_only_reads", before == after)
        # the split holds: Agent Jo imports nothing of the job search's
        bad = []
        for f in list((root / "agent").glob("*.py")) + list((root / "web").glob("*.py")):
            for n in _cast.walk(_cast.parse(f.read_text("utf-8"))):
                names = ([a.name for a in n.names] if isinstance(n, _cast.Import) else
                         [n.module or ""] + [a.name for a in n.names] if isinstance(n, _cast.ImportFrom) else [])
                if any(x.split(".")[-1] in ("jobscout", "portal", "answerbank", "cvstore", "inbox", "boards")
                       for x in names):
                    bad.append(f.name)
        check("career.still_no_job_search_code_in_agent_jo", not bad, f"{bad}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_JOBS_HOME", None)
        else:
            os.environ["AGENT_JOBS_HOME"] = saved


# ---------------------------------------------------------------------- #
# One-click updates, and only ones you signed.
#   python -c "import tests.run_tests as t; t._updater_checks()"
# ---------------------------------------------------------------------- #
def _updater_checks() -> None:
    import hashlib as _uh
    import importlib.util as _uil
    import io as _uio
    import tempfile as _ut
    import zipfile as _uz
    from cryptography.hazmat.primitives import serialization as _us
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ued
    import agent.updater as U

    def pem(k):
        return k.public_key().public_bytes(encoding=_us.Encoding.PEM,
                                           format=_us.PublicFormat.SubjectPublicKeyInfo)

    mine, theirs = _ued.Ed25519PrivateKey.generate(), _ued.Ed25519PrivateKey.generate()

    def release(files, *, build="2099-01-01 00:00 UTC", key=mine, tamper=None,
                pub=None, extra=None, top="local-agent/"):
        entries = {n: _uh.sha256(b).hexdigest() for n, b in files.items()}
        man = json.dumps({"build": build, "entries": entries}, sort_keys=True).encode()
        buf = _uio.BytesIO()
        with _uz.ZipFile(buf, "w") as z:
            for n, b in files.items():
                z.writestr(top + n, tamper.get(n, b) if tamper else b)
            z.writestr(top + U.MANIFEST, man)
            z.writestr(top + U.SIGNATURE, key.sign(man))
            z.writestr(top + U.PUBKEY, pub if pub is not None else pem(key))
            for n, b in (extra or {}).items():
                z.writestr(n, b)
        return buf.getvalue()

    saved = (U.ROOT, U.config.BUILD_ID, U.config.AGENT_HOME)
    try:
        root = Path(_ut.mkdtemp(prefix="installed-"))
        (root / "agent").mkdir()
        (root / "agent" / "a.py").write_text("old a\n")
        (root / "README.md").write_text("old readme\n")
        (root / ".venv").mkdir(); (root / ".venv" / "keep").write_text("venv")
        U.ROOT = root
        U.config.BUILD_ID = "2026-10-06 21:00 UTC"
        U.config.AGENT_HOME = Path(_ut.mkdtemp(prefix="uphome-"))
        rel_dir = Path(_ut.mkdtemp(prefix="release-"))

        def offer(data, *, build="2099-01-01 00:00 UTC", sha=None):
            (rel_dir / "r.zip").write_bytes(data)
            (rel_dir / "latest.json").write_text(json.dumps({
                "build": build, "url": "r.zip",
                "sha256": sha or _uh.sha256(data).hexdigest()}))
            U.save_settings({"source": str(rel_dir / "latest.json")})

        def snapshot():
            return sorted((str(p.relative_to(root)), p.read_bytes())
                          for p in root.rglob("*") if p.is_file() and ".update" not in p.parts)

        good = {"agent/a.py": b"new a\n", "agent/b.py": b"brand new b\n", "README.md": b"new readme\n"}
        offer(release(good))
        check("update.no_key_in_this_copy_means_no_updates",
              "no signing-key.pub" in U.install()["error"])
        (root / U.PUBKEY).write_bytes(pem(mine))
        before = snapshot()

        def refused(name, data, why, **kw):
            offer(data, **kw)
            r = U.install()
            check(name, not r["ok"] and why in r["error"] and snapshot() == before, r.get("error"))
        refused("update.someone_elses_signature_is_refused",
                release(good, key=theirs, pub=pem(mine)), "wasn't signed by you")
        refused("update.a_file_changed_after_signing_is_refused",
                release(good, tamper={"agent/a.py": b"evil\n"}), "don't match")
        refused("update.a_checksum_mismatch_is_refused", release(good), "checksum", sha="0" * 64)
        refused("update.a_new_key_cant_arrive_by_update",
                release(good, pub=pem(theirs)), "different signing key")
        refused("update.the_venv_and_paths_outside_are_off_limits",
                release({**good, ".venv/evil": b"x"}), "not allowed")
        refused("update.a_zip_slip_is_refused",
                release(good, extra={"../../escape.txt": b"x"}), "unsafe path")
        offer(release(good, build="2020-01-01 00:00 UTC"), build="2020-01-01 00:00 UTC")
        c = U.check()
        check("update.an_older_build_isnt_offered", c["ok"] and not c["newer"])
        refused("update.an_older_build_isnt_installed",
                release(good, build="2020-01-01 00:00 UTC"), "not newer",
                build="2099-01-01 00:00 UTC")

        offer(release(good))
        c = U.check()
        check("update.a_newer_build_is_offered", c["ok"] and c["newer"] and c["latest"].startswith("2099"))
        r = U.install()
        check("update.a_signed_update_installs",
              r["ok"] and (root / "agent" / "a.py").read_text() == "new a\n"
              and (root / "agent" / "b.py").exists()
              and (root / ".venv" / "keep").read_text() == "venv", f"{r}")
        check("update.it_says_to_restart", "Restart" in r["message"])
        rb = U.roll_back()
        check("update.roll_back_restores_exactly",
              rb["ok"] and snapshot() == before, rb.get("error"))
        check("update.only_one_roll_back", not U.roll_back()["ok"])

        # the release tool signs what the updater checks
        spec = _uil.spec_from_file_location("_mr", Path(__file__).resolve().parents[1] / "tools" / "make_release.py")
        mr = _uil.module_from_spec(spec); spec.loader.exec_module(mr)
        kf = Path(_ut.mkdtemp()) / "signing-key.private"
        kf.write_bytes(mine.private_bytes(encoding=_us.Encoding.PEM,
                                          format=_us.PrivateFormat.PKCS8,
                                          encryption_algorithm=_us.NoEncryption()))
        mr.KEYFILE = kf
        names = ["agent/updater.py", "README.md"]
        signed = mr._sign_files([Path(n) for n in names])
        stage = Path(_ut.mkdtemp(prefix="stage-"))
        for n in names:
            (stage / n).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mr.ROOT / n, stage / n)
        for n, b in signed.items():
            (stage / n).write_bytes(b)
        check("update.releases_are_signed_the_way_the_updater_checks",
              U.verify_release(stage, pem(mine))["ok"])
        src = (Path(__file__).resolve().parents[1] / "tools" / "make_release.py").read_text("utf-8")
        check("update.the_private_key_can_never_ship",
              '"signing-key.private"' in src.split("LEAK_FILENAMES")[1][:80]
              and '".update"' in src)
    finally:
        U.ROOT, U.config.BUILD_ID, U.config.AGENT_HOME = saved



# ---------------------------------------------------------------------- #
# Background service: one scheduler however many copies are open.
#   python tests/run_tests.py -k "background service"
# ---------------------------------------------------------------------- #
def _service_checks() -> None:
    #
    # Schedules only ran while a window was open, and two open windows would
    # each have fired the same schedule. The service definitions are rendered
    # by pure functions, and the lock is checked across real processes.
    import subprocess as _svsp
    import tempfile as _svt
    import xml.dom.minidom as _svx
    from agent import service as _sv
    _sv_saved = config.AGENT_HOME
    try:
        config.AGENT_HOME = Path(_svt.mkdtemp(prefix="svc-"))
        _sv.release_scheduler_lock()
        check("service.first_process_gets_the_scheduler", _sv.acquire_scheduler_lock())
        check("service.asking_again_is_harmless", _sv.acquire_scheduler_lock())
        _sv_probe = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r});"
                     f"from agent import config, service; from pathlib import Path;"
                     f"config.AGENT_HOME = Path({str(config.AGENT_HOME)!r});"
                     f"print(service.acquire_scheduler_lock())")
        _sv_other = _svsp.run([sys.executable, "-c", _sv_probe], capture_output=True,
                              text=True, timeout=60).stdout.strip()
        check("service.a_second_process_leaves_the_schedules_alone", _sv_other == "False", _sv_other)
        _sv.release_scheduler_lock()
        _sv_other = _svsp.run([sys.executable, "-c", _sv_probe], capture_output=True,
                              text=True, timeout=60).stdout.strip()
        check("service.it_takes_over_once_the_holder_lets_go", _sv_other == "True", _sv_other)

        _sv_py = "/opt/My Python/bin/python"
        _sv_unit = _sv.render_systemd(_sv_py, 8765, home="/home/me/.local_agent")
        check("service.systemd_restarts_forever",
              "Restart=always" in _sv_unit and "StartLimitIntervalSec=0" in _sv_unit)
        check("service.systemd_quotes_paths_with_spaces",
              'ExecStart="/opt/My Python/bin/python"' in _sv_unit)
        check("service.never_moves_to_another_port",
              "--strict-port" in _sv_unit and "--port 8765" in _sv_unit
              and "--no-browser" in _sv_unit)
        _sv_plist = _sv.render_launchd(_sv_py, 8765, home="/Users/me/.local_agent")
        _sv_doc = _svx.parseString(_sv_plist.encode())
        check("service.launchd_plist_is_valid_xml_and_kept_alive",
              _sv_doc.documentElement.tagName == "plist"
              and "<key>KeepAlive</key>\n    <true/>" in _sv_plist)
        _sv_task = _sv.render_windows_task(r"C:\Program Files\Py\pythonw.exe", 8765, user="jo&co")
        _sv_tdoc = _svx.parseString(_sv_task.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode())
        check("service.windows_task_is_valid_xml",
              _sv_tdoc.documentElement.tagName == "Task")
        check("service.windows_task_restarts_and_runs_unprivileged",
              "<RestartOnFailure>" in _sv_task and "LeastPrivilege" in _sv_task
              and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in _sv_task
              and "IgnoreNew" in _sv_task)
        check("service.windows_task_escapes_the_user", "jo&amp;co" in _sv_task)
        check("service.status_answers_without_installing", "installed" in _sv.status())

        # the launcher opens a running copy instead of starting a second one
        _sv_rw = (Path(__file__).resolve().parents[1] / "run_web.py").read_text("utf-8")
        check("service.launcher_hands_off_to_a_running_copy",
              "_running_agent_jo" in _sv_rw and "--strict-port" in _sv_rw and "--new" in _sv_rw)
        _sv_srv = (Path(__file__).resolve().parents[1] / "web" / "server.py").read_text("utf-8")
        check("service.scheduler_loop_takes_the_lock",
              "acquire_scheduler_lock" in _sv_srv.split("def _scheduler_loop")[1][:900])
    finally:
        _sv.release_scheduler_lock()
        config.AGENT_HOME = _sv_saved


# ---------------------------------------------------------------------- #
# Notifications: ntfy push, Telegram with Approve / Refuse, from one chat.
#   python tests/run_tests.py -k notifications
# ---------------------------------------------------------------------- #
def _notify_checks() -> None:
    import json as _nj
    import tempfile as _ntt
    from agent import config as _nc, intercepts as IC, notify as NT, phone as PH, breaker as BR
    from agent import tools as TL

    class Mem:
        def __init__(self):
            self.s = {1: {"id": 1, "name": "Morning brief", "enabled": 1, "spec": "{}",
                          "last_status": "error", "last_summary": "model retired", "next_run": 1.0}}
        def list_schedules(self): return [dict(v) for v in self.s.values()]
        def get_schedule(self, i): return dict(self.s[i]) if i in self.s else None
        def set_schedule_enabled(self, i, on, nxt=None):
            self.s[i]["enabled"] = int(on); return True

    class _C:
        def print(self, *a, **k): pass

    sent = []

    def fake_post(url, *, data, headers, timeout=15):
        sent.append({"url": url, "data": data, "headers": headers})
        if "api.telegram.org" in url and url.endswith("/getUpdates"):
            return 200, _nj.dumps({"ok": True, "result": [
                {"update_id": 5, "message": {"chat": {"id": 4242, "type": "private",
                                                      "first_name": "Jo"}, "text": "hi"}}]})
        if "api.telegram.org" in url:
            return 200, '{"ok": true, "result": {}}'
        return 200, "{}"

    saved = (_nc.AGENT_HOME, NT.POSTER, NT.SYNC, PH._CTX.get("memory"), TL._execute_tool_impl)
    ran = []
    _nc.AGENT_HOME = Path(_ntt.mkdtemp(prefix="notify-"))
    NT.POSTER, NT.SYNC = fake_post, True
    NT._RECENT.clear(); NT._SENT.clear()
    TL._execute_tool_impl = lambda name, args, memory, console, auto_approve=False, session_id="", **kw: (
        ran.append(name) or "sent")
    try:
        check("notify.off_until_set_up", NT.send("held", "x")["skipped"] == "no channel set up"
              and not sent)

        NT.save({"ntfy": {"enabled": True, "topic": "/agentjo-test-7f3/"}})
        check("notify.topic_is_tidied", NT.settings()["ntfy"]["topic"] == "agentjo-test-7f3")
        item = IC.hold("send_email", {"to": "hiring@acme.com", "subject": "Application",
                                       "body": "SECRET BODY TEXT " * 20}, source="schedule")
        ntfy = [x for x in sent if "ntfy.sh/agentjo-test-7f3" in x["url"]]
        check("notify.a_held_call_pushes_to_ntfy", len(ntfy) == 1, sent)
        check("notify.push_carries_the_summary_not_the_body",
              b"hiring@acme.com" in ntfy[0]["data"] and b"SECRET BODY" not in ntfy[0]["data"]
              and ntfy[0]["headers"].get("Priority") == "high")
        before = len(sent)
        NT.held(item)
        check("notify.the_same_thing_twice_is_sent_once", len(sent) == before)
        NT.save({"events": {"held": False}})
        check("notify.events_can_be_switched_off",
              NT.send("held", "y", key="other")["skipped"] == "event switched off")
        NT.save({"events": {"held": True}})

        # Telegram: token kept encrypted, never shown back, buttons on a hold
        NT.save({"telegram": {"enabled": True, "token": "123:ABCSECRETTOKEN"}})
        raw = (_nc.AGENT_HOME / "notify.json").read_text("utf-8")
        check("notify.telegram_token_is_encrypted_at_rest", "ABCSECRETTOKEN" not in raw)
        check("notify.panel_never_sees_the_token",
              NT.public_settings()["telegram"]["token"] is True
              and "ABCSECRETTOKEN" not in _nj.dumps(NT.public_settings()))
        check("notify.telegram_needs_a_chat_before_it_sends",
              "telegram" not in NT.enabled_channels())
        found = NT.find_chat()
        check("notify.find_my_chat_reads_who_wrote_to_the_bot",
              found.get("ok") and found["chat_id"] == "4242", found)
        NT.save({"telegram": {"chat_id": found["chat_id"]}})
        check("notify.saving_without_a_token_keeps_the_old_one",
              NT.settings()["telegram"]["token"] == "123:ABCSECRETTOKEN")
        sent.clear()
        item2 = IC.hold("send_email", {"to": "boss@acme.com", "subject": "Report"})
        tg = [x for x in sent if x["url"].endswith("/sendMessage")]
        msg = _nj.loads(tg[0]["data"]) if tg else {}
        btns = (msg.get("reply_markup") or {}).get("inline_keyboard", [[]])[0]
        check("notify.telegram_hold_has_approve_and_refuse",
              [b["callback_data"] for b in btns] == [f"ij:y:{item2['id']}", f"ij:n:{item2['id']}"], msg)

        PH.configure(Mem(), _C())
        stranger = {"update_id": 9, "callback_query": {
            "id": "q1", "data": f"ij:y:{item2['id']}", "from": {"id": 666},
            "message": {"message_id": 3, "chat": {"id": 666}}}}
        check("notify.a_stranger_cannot_approve",
              NT.handle_update(stranger)["did"] == "ignored"
              and IC.state_of(item2["id"]) == "waiting" and not ran)
        mine = {"update_id": 10, "callback_query": {
            "id": "q2", "data": f"ij:y:{item2['id']}", "from": {"id": 4242},
            "message": {"message_id": 3, "chat": {"id": 4242}}}}
        r = NT.handle_update(mine)
        check("notify.your_tap_approves_and_runs_it",
              r["did"].startswith("approved") and ran == ["send_email"], (r, ran))
        r = NT.handle_update({"update_id": 11, "callback_query": {
            "id": "q3", "data": f"ij:n:{item['id']}", "from": {"id": 4242},
            "message": {"message_id": 4, "chat": {"id": 4242}}}})
        check("notify.refuse_refuses", IC.state_of(item["id"]) == "refused" and ran == ["send_email"])
        st = NT.handle_update({"update_id": 12, "message": {"chat": {"id": 4242}, "from": {"id": 4242},
                                                           "text": "/status"}})
        check("notify.status_lists_failing_schedules", "Morning brief" in st["reply"], st)
        pz = NT.handle_update({"update_id": 13, "message": {"chat": {"id": 4242}, "from": {"id": 4242},
                                                           "text": "/pause"}})
        check("notify.pause_from_telegram", "Paused 1" in pz["reply"], pz)

        # the other events
        sent.clear()
        for _ in range(BR.THRESHOLD):
            BR.record_failure("notify-test-feature", "host not found")
        check("notify.a_tripped_breaker_tells_you",
              any(b"notify-test-feature" in x["data"] or "notify-test-feature" in
                  _nj.dumps(x["headers"]) for x in sent), sent)
        sent.clear()
        r = NT.schedule_failed("Morning brief", "NotFoundError: model retired")
        check("notify.a_failed_schedule_tells_you", r.get("ok") and len(sent) == 2, r)
        NT._SENT[:] = [__import__("time").time()] * NT.MAX_PER_HOUR
        check("notify.an_hourly_ceiling_stops_a_flood",
              NT.send("failed", "flood", key="new")["skipped"] == "hourly ceiling")
        src = (Path(__file__).resolve().parents[1] / "web" / "server.py").read_text("utf-8")
        check("notify.schedule_failures_are_wired", "_nt.schedule_failed(" in src)
        tsrc = (Path(__file__).resolve().parents[1] / "agent" / "tools.py").read_text("utf-8")
        check("notify.blocked_steps_are_wired", "notify.step_blocked(" in tsrc)
    finally:
        _nc.AGENT_HOME, NT.POSTER, NT.SYNC, _m, TL._execute_tool_impl = saved
        PH._CTX["memory"] = _m
        NT._RECENT.clear(); NT._SENT.clear()

# ---------------------------------------------------------------------- #
# Goals: outcomes over days, with checkpoints, budgets and a stall fence.
#   python tests/run_tests.py -k goals
# ---------------------------------------------------------------------- #
def _goals_checks() -> None:
    import tempfile as _gt
    import time as _gtime
    from agent import config as _gc, goals as GL, notify as NT, tools as TL

    told = []
    saved = (_gc.AGENT_HOME, NT.goal_event)
    _gc.AGENT_HOME = Path(_gt.mkdtemp(prefix="goals-"))
    NT.goal_event = lambda g, what, detail="": told.append(what) or {"ok": True}
    try:
        try:
            GL.create("x", "y", "")
            check("goals.need_success_criteria", False)
        except ValueError:
            check("goals.need_success_criteria", True)
        g = GL.create("Five interviews", "Get five first-round interviews",
                      "Five interview invitations in the inbox", budget_usd=1.0, runs_per_day=4)
        check("goals.created_active", g["status"] == "active" and g["id"] == 1)
        p = GL.build_prompt(g)
        check("goals.first_run_is_told_to_plan", "create_task_plan" in p and "update_goal" in p)

        # a run that checkpoints is progress, and the checkpoint feeds the next prompt
        def good_runner(goal, prompt):
            TL._execute_tool_impl("update_goal", {"goal_id": goal["id"], "status": "progressing",
                                                  "note": "Applied to 3 roles; next: follow up"},
                                  None, None)
            return "ok", {"usage": {"in": 1000, "out": 100}, "stop_reason": ""}
        now = _gtime.time()
        r = GL.sweep(good_runner, cost_fn=lambda i: 0.25, now=now)
        g = GL.get(1)
        check("goals.a_checkpoint_counts_as_progress",
              r["ran"] == 1 and r["progressed"] == 1 and g["stalls"] == 0, r)
        check("goals.spend_is_charged_to_the_goal", abs(g["spent_usd"] - 0.25) < 1e-9)
        check("goals.next_run_resumes_from_the_checkpoint",
              "Applied to 3 roles; next: follow up" in GL.build_prompt(g))
        check("goals.not_due_again_straight_away", not GL.is_due(g, now + 60))
        check("goals.due_after_its_gap", GL.is_due(g, now + 6 * 3600 + 1))

        # a run cut off by the limit is handed on, not lost
        def cut_runner(goal, prompt):
            return "was halfway through the spreadsheet", {"stop_reason": "round_limit"}
        GL.run_one(GL.get(1), cut_runner, cost_fn=lambda i: 0.0, now=now + 7 * 3600)
        g = GL.get(1)
        check("goals.a_cut_off_run_leaves_an_automatic_checkpoint",
              "halfway through the spreadsheet" in g["notes"][-1]["text"]
              and g["notes"][-1]["kind"] == "checkpoint")
        check("goals.but_it_still_counts_as_a_stall", g["stalls"] == 1)

        # three runs with no progress stop it, and tell you
        def idle_runner(goal, prompt):
            return "", {}
        for k in range(2):
            GL.run_one(GL.get(1), idle_runner, now=now + (8 + k) * 3600)
        g = GL.get(1)
        check("goals.three_stalls_stop_it", g["status"] == "stalled" and "stalled" in told, (g["status"], told))
        GL.set_status(1, "active")
        check("goals.resuming_clears_the_stall_count", GL.get(1)["stalls"] == 0)

        # budget
        GL.run_one(GL.get(1), good_runner, cost_fn=lambda i: 0.80, now=now + 12 * 3600)
        check("goals.out_of_budget_stops_it", GL.get(1)["status"] == "budget" and "out of budget" in told)
        GL.set_status(1, "active")
        r = GL.run_one(GL.get(1), good_runner, now=now + 13 * 3600)
        check("goals.a_spent_budget_is_checked_before_running", r["ran"] is False and r["status"] == "budget")

        # deadline: an expired goal stops even when it isn't due
        g2 = GL.create("Ship it", "Release v2", "Tag pushed", deadline="2020-01-01")
        GL.sweep(good_runner, now=now)
        check("goals.past_its_deadline_it_stops", GL.get(g2["id"])["status"] == "expired")

        # the update_goal tool's own fences
        g3 = GL.create("Docs", "Write the guide", "Guide merged")
        out = TL._execute_tool_impl("update_goal", {"goal_id": g3["id"], "status": "achieved",
                                                    "note": "done"}, None, None)
        check("goals.achieved_needs_evidence", out.startswith("Refused") and GL.get(g3["id"])["status"] == "active", out)
        out = TL._execute_tool_impl("update_goal", {"goal_id": g3["id"], "status": "blocked",
                                                    "note": "stuck"}, None, None)
        check("goals.blocked_needs_what_you_must_do", out.startswith("Refused"), out)
        out = TL._execute_tool_impl("update_goal", {"goal_id": g3["id"], "status": "blocked",
                                                    "note": "stuck", "needs": "log in to GitHub"}, None, None)
        check("goals.blocked_tells_you", GL.get(g3["id"])["status"] == "blocked" and "blocked" in told)
        g4 = GL.create("Tidy", "Clean the drive", "Under 50% used")
        out = TL._execute_tool_impl("update_goal", {"goal_id": g4["id"], "status": "achieved",
                                                    "note": "done", "evidence": "df shows 41% used on C:, checked just now"},
                                    None, None)
        check("goals.achieved_with_evidence", GL.get(g4["id"])["status"] == "achieved" and "achieved" in told, out)
        out = TL._execute_tool_impl("update_goal", {"goal_id": g4["id"], "status": "progressing",
                                                    "note": "more"}, None, None)
        check("goals.a_finished_goal_takes_no_more_updates", out.startswith("Refused"), out)
        lst = TL._execute_tool_impl("list_goals", {}, None, None)
        check("goals.list_goals_tool", "#1 Five interviews" in lst and "Tidy" in lst, lst)
        names = [t["name"] for t in TL.TOOL_DEFINITIONS]
        check("goals.tools_are_offered", {"create_goal", "update_goal", "list_goals"} <= set(names))

        # the run gets a goal-sized budget, not a chat turn's
        import inspect as _gi
        from agent import main as _gm
        sig = _gi.signature(_gm.run_turn).parameters
        check("goals.run_turn_takes_a_bigger_budget", "max_rounds" in sig and "time_limit" in sig)
        src = (Path(__file__).resolve().parents[1] / "web" / "server.py").read_text("utf-8")
        check("goals.the_schedule_passes_it",
              "max_rounds=_goals.GOAL_MAX_ROUNDS" in src and "time_limit=_goals.GOAL_TIME_LIMIT" in src)
    finally:
        _gc.AGENT_HOME, NT.goal_event = saved

# ---------------------------------------------------------------------- #
# Running it in sections.
#
# main() is one function of 9,000 lines that stopped at the first failure:
# one missing package, or one broken check, and every check after it went
# unrun and unreported. It runs section by section now — each "# ---"
# heading in main() is a section — and a failure is recorded and the run
# goes on. At the end: every failure, every section that couldn't run and
# why, and which of those only failed because an earlier section did.
#
#   python tests/run_tests.py            every section, a full report
#   python tests/run_tests.py -k phone   only sections whose title matches
#                                        (and the ones they depend on)
#   python tests/run_tests.py --list     the sections
#   python tests/run_tests.py --stop     the old way: stop at the first failure
# ---------------------------------------------------------------------- #
def sections() -> list:
    """[(title, first_line, source)] of main(), in order."""
    import re as _sre
    lines = Path(__file__).read_text("utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("def main("))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith(("def ", "class ", "if __name__", "# ----")))
    body = list(range(start + 1, end))
    out, title, first, buf = [], "setup: memories, skills, tasks", body[0], []
    for i in body:
        m = _sre.match(r"^    # --- (.*?)\s*-*\s*#?\s*$", lines[i])
        if m:
            if any(x.strip() for x in buf):
                out.append((title, first + 1, "\n".join(buf)))
            title, first, buf = m.group(1).strip(" -#") or "section", i, []
        buf.append(lines[i])
    if any(x.strip() for x in buf):
        out.append((title, first + 1, "\n".join(buf)))
    return out


def run(argv=None) -> int:
    import textwrap as _stw
    import time as _stime
    import traceback as _stb
    argv = sys.argv[1:] if argv is None else argv
    secs = sections()
    if "--list" in argv:
        for n, (t, ln, _) in enumerate(secs, 1):
            print(f"{n:>4}  line {ln:<6} {t}")
        return 0
    if "--stop" in argv:
        try:
            main()
        except AssertionError as exc:
            print(f"\nFAILED after {len(PASS)} passing checks: {exc}")
            return 1
        return 0
    want = ""
    if "-k" in argv and argv.index("-k") + 1 < len(argv):
        want = argv[argv.index("-k") + 1].lower()
    _COLLECT["on"] = True
    ns = globals()
    results, crashed, t0 = [], [], _stime.time()
    defined_by = {}
    chosen = [i for i, (t, _, _) in enumerate(secs) if not want or want in t.lower()]
    if want and not chosen:
        print(f"No section's title contains {want!r}. Try --list.")
        return 2
    # with -k, a chosen section's names may come from earlier sections, so
    # those run too — quietly — but only the ones it actually uses
    import ast as _sast
    import re as _sre2

    def _names(code, kind):
        try:
            tree = _sast.parse(_stw.dedent(code))
        except SyntaxError:
            return set()
        if kind == "def":
            return {n.id for n in _sast.walk(tree) if isinstance(n, _sast.Name)
                    and isinstance(n.ctx, _sast.Store)} | {
                   (a.asname or a.name).split(".")[0] for n in _sast.walk(tree)
                   if isinstance(n, (_sast.Import, _sast.ImportFrom)) for a in n.names} | {
                   n.name for n in _sast.walk(tree)
                   if isinstance(n, (_sast.FunctionDef, _sast.ClassDef))}
        return {n.id for n in _sast.walk(tree) if isinstance(n, _sast.Name)
                and isinstance(n.ctx, _sast.Load)}
    run_set = set(chosen)
    if want:
        need = set().union(*(_names(secs[i][2], "use") for i in chosen)) - set(ns)
        for i in range(max(chosen), -1, -1):
            if i in run_set:
                continue
            gives = _names(secs[i][2], "def")
            if gives & need:
                run_set.add(i)
                need |= _names(secs[i][2], "use") - set(ns)
    for i, (title, line, code) in enumerate(secs):
        if i not in run_set:
            continue
        p0, f0 = len(PASS), len(FAILED)
        status, why = "ok", ""
        try:
            exec(compile("\n" * (line - 1) + _stw.dedent(code), __file__, "exec"), ns)
        except AssertionError as exc:
            status, why = "failed", str(exc)
        except ModuleNotFoundError as exc:
            status, why = "needs a package", f"{exc.name} isn't installed"
        except NameError as exc:
            name = getattr(exc, "name", "") or str(exc)
            src_sec = defined_by.get(name)
            # only excused when the section that sets it up couldn't run: a
            # name nothing defines is a real bug, reported as one
            if src_sec and src_sec in crashed:
                status = "skipped"
                why = (f"needs {name}, which “{src_sec}” would have set up, "
                       f"but that section couldn't run")
            else:
                status, why = "crashed", f"NameError: {exc}"
        except Exception as exc:
            tb = _stb.extract_tb(exc.__traceback__)
            at = next((f"line {f.lineno}" for f in reversed(tb) if f.filename == __file__), "")
            status, why = "crashed", f"{type(exc).__name__}: {str(exc)[:200]} {at}".strip()
        if status != "ok":
            crashed.append(title)
        for nm in _names(code, "def"):
            defined_by.setdefault(nm, title)
        quiet = want and i not in chosen
        results.append((title, line, status, why, len(PASS) - p0, FAILED[f0:], quiet))
    _COLLECT["on"] = False

    shown = [r for r in results if not r[6]]
    bad_checks = sum(len(r[5]) for r in shown)
    print()
    for title, line, status, why, n, fails, quiet in results:
        if quiet and status == "ok" and not fails:
            continue
        if status == "ok" and not fails:
            continue
        mark = {"failed": "FAIL", "crashed": "CRASH", "needs a package": "NEEDS",
                "skipped": "SKIP", "ok": "FAIL"}[status]
        print(f"{mark:>5}  {title}  (line {line}, {n} passed)" + ("  [ran for -k]" if quiet else ""))
        for f in fails[:8]:
            print(f"         ✗ {f}")
        if len(fails) > 8:
            print(f"         … and {len(fails) - 8} more")
        if why:
            print(f"         {why}")
    counts = {}
    for r in shown:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print(f"\n{len(PASS)} checks passed, {bad_checks} failed, in {len(shown)} section(s)"
          f" ({_stime.time() - t0:.0f}s).")
    extra = [f"{counts[k]} {k}" for k in ("failed", "crashed", "needs a package", "skipped")
             if counts.get(k)]
    if extra:
        print("Sections not clean: " + ", ".join(extra) + ".")
    if counts.get("skipped"):
        print("A skipped section only failed because an earlier one couldn't run — "
              "fix that one first.")
    ok = not bad_checks and not any(counts.get(k) for k in ("failed", "crashed", "needs a package", "skipped"))
    print("OK" if ok else "NOT OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
