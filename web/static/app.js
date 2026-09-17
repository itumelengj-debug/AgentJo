/* ===========================================================================
   Agent Jo - web client logic (vanilla JS, no build step)
   Talks to the FastAPI backend: streams chat over SSE, manages engines, and
   keeps a client-side conversation list (the server holds each conversation's
   model-level message history, keyed by id).
   ========================================================================== */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };

/* ---- automatic error pickup ------------------------------------------------
   Uncaught JS errors and every API call that fails or returns an error status
   report themselves to /api/client-errors (throttled, fire-and-forget), with
   the element that was active — usually the button just clicked — attached.
   They land in the 🐞 Issues panel without anyone having to remember them. */
const _errSeen = new Map();
let _errBudget = [];
function reportClientError(p) {
  try {
    const key = (p.url || "") + "|" + (p.status || "") + "|" + (p.message || "").slice(0, 60);
    const now = Date.now();
    if (_errSeen.has(key) && now - _errSeen.get(key) < 20000) return;
    _errBudget = _errBudget.filter(t => now - t < 60000);
    if (_errBudget.length >= 15) return;
    _errSeen.set(key, now); _errBudget.push(now);
    fetch("/api/client-errors", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p),
    }).catch(() => {});
  } catch (e) {}
}
window.addEventListener("error", (ev) => {
  reportClientError({ message: String(ev.message || ev.error || "script error").slice(0, 200),
                      url: String(ev.filename || "").slice(-60) + (ev.lineno ? ":" + ev.lineno : ""),
                      ui: (document.activeElement && document.activeElement.id) || "" });
});
window.addEventListener("unhandledrejection", (ev) => {
  reportClientError({ message: "unhandled: " + String((ev.reason && (ev.reason.message || ev.reason)) || "rejection").slice(0, 200),
                      ui: (document.activeElement && document.activeElement.id) || "" });
});
const _origFetch = window.fetch.bind(window);
window.fetch = async function (url, opts) {
  const u = String(url || "");
  const track = u.includes("/api/") && !u.includes("/api/client-errors");
  const ui = track ? ((document.activeElement && document.activeElement.id) || "") : "";
  let res;
  try {
    res = await _origFetch(url, opts);
  } catch (err) {
    if (track) reportClientError({ message: "network: " + String(err && err.message || err).slice(0, 150), url: u, ui });
    throw err;
  }
  if (track && !res.ok) {
    let detail = "";
    try {
      const t = await res.clone().text();
      try { detail = (JSON.parse(t).detail || "").toString(); }
      catch (e) { detail = t; }
    } catch (e) {}
    reportClientError({ message: "request failed", url: u, status: res.status,
                        detail: String(detail).slice(0, 150), ui });
  }
  return res;
};

const state = {
  engines: [],
  engine: "Auto",
  engineModel: "smart routing",
  conversations: [],          // {id, title, project_id, messages, streaming, ...}
  activeId: null,
  pendingFiles: [],           // File objects staged for the next message
  engineStats: {},            // label -> {score, observed} for the engine menu
  projects: [],
  // Ungrouped by default: moving a conversation into a group should take it
  // out of the general list, which is the whole point of grouping. The old
  // default showed everything, so a filed conversation appeared twice and
  // the list never got shorter.
  projectFilter: "none",
  autoSpeak: false,
  voiceStt: false,
  recording: false,
  mediaRecorder: null,
  audioChunks: [],
};

// Mirrors the desktop app's managed settings (agent/config.py _USER_KEYS).
const SETTINGS_SCHEMA = [
  { group: "Behaviour", items: [
    { key: "AUTO_LEARN", type: "bool", name: "Learn facts from chats", desc: "Save durable facts to memory after turns" },
    { key: "ROUTING", type: "bool", name: "Smart routing", desc: "Auto-pick cloud vs local per turn (Auto engine)" },
    { key: "PROMPT_CACHE", type: "bool", name: "Prompt caching", desc: "Cache the system prompt to cut token cost" },
    { key: "SUBAGENTS", type: "bool", name: "Sub-agents", desc: "Allow spawning helper agents for subtasks" },
  ]},
  { group: "Limits", items: [
    { key: "TURN_TIMEOUT", type: "int", name: "Turn timeout (seconds)", desc: "Abort a turn after this long" },
    { key: "MAX_TOOL_ROUNDS", type: "int", name: "Max tool rounds", desc: "Tool call/observe loops per turn" },
    { key: "MAX_TOKENS", type: "int", name: "Max output tokens", desc: "Cap on tokens generated per reply" },
    { key: "OLLAMA_NUM_CTX", type: "int", name: "Local context window", desc: "Ollama context budget (prompt + reply). Raise for longer local replies; must be within what the model supports." },
    { key: "MAX_MEMORIES_IN_CONTEXT", type: "int", name: "Memories in context", desc: "How many saved memories to include" },
  ]},
  { group: "History & compaction", items: [
    { key: "MAX_HISTORY_TURNS", type: "int", name: "History turns kept", desc: "Conversation turns retained in context" },
    { key: "COMPACT_TRIGGER_TURNS", type: "int", name: "Compact after N turns", desc: "Summarise once history exceeds this" },
    { key: "COMPACT_KEEP_TURNS", type: "int", name: "Keep N turns verbatim", desc: "Recent turns left uncompacted" },
  ]},
];

/* ----------------------------- boot ----------------------------- */
async function boot() {
  let authState = { enabled: false, authenticated: true };
  try { authState = await fetch("/api/auth/status").then(r => r.json()); } catch (e) {}
  if (authState.enabled && !authState.authenticated) { showAuthOverlay(); return; }
  closeEngineModal();          // CSS shows .modal-backdrop by default; force it shut on load
  closeSettings();
  closeDocuments();
  closeMemory();
  closePerms();
  closeSched();
  try {
    const meta = await fetch("/api/meta").then(r => r.json());
    state.engines = meta.engines || [];
    state.defaultEngine = meta.default_engine || "Auto";
    state.voiceStt = !!(meta.voice && meta.voice.stt);
    if (state.voiceStt) $("#micBtn").hidden = false;
    // The footer named a supplier where the product's name belongs. The
    // ENGINE is still shown, because when something fails the first question
    // is always which engine was serving it — that's diagnostics, not a
    // masthead.
    // The app already has its lockup at the top of the sidebar; a second
    // mark at the bottom competed with it. The brand still heads the Health
    // and Issues reports, where it identifies whose tool produced them.
    window.__brand = meta.brand || "Symbolic Synapse";
    $("#backendMeta").textContent =
      "engine: " + (meta.default_engine || meta.backend || "not set");
    if (!meta.configured) {
      showToast("No engine configured yet - add one under Manage engines, or set an API key.", true);
    }
  } catch (e) {
    showToast("Could not reach the server.", true);
  }
  const savedId = (typeof state.defaultEngine === "string" && state.defaultEngine) || "Auto";
  const startEngine = state.engines.find(e => e.id === savedId)
    || state.engines.find(e => e.id === "Auto") || state.engines[0];
  applyEngine(startEngine);
  setGreeting();
  await loadProjects();
  await loadConversations();
  newConversation();
  wireEvents();
  initKpiToggle();
  initTurnStatus();
  refreshStats();
  setInterval(refreshStats, 45000);
}

function initTurnStatus() {
  setInterval(renderTurnStatus, 1000);        // live elapsed timer
  $("#turnStop").addEventListener("click", () => {
    const c = activeStreamingConv();
    if (!c) return;
    c._stopRequested = true;
    $("#turnPhase").textContent = "stopping…";
    // stop the server-side turn at its next checkpoint, then drop the stream
    fetch("/api/chat/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: c.id }),
    }).catch(() => {});
    try { c._abort && c._abort.abort(); } catch (e) {}
  });
}

function setGreeting() {
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  $("#greeting").textContent = `${part}. How can I help?`;
}

/* ----------------------------- engines ----------------------------- */
function applyEngine(eng) {
  if (!eng) return;
  state.engine = eng.id;
  state.engineModel = eng.model;
  $("#engineName").textContent = eng.label;
  $("#engineModel").textContent = eng.model;
  const pill = $("#enginePill");
  pill.classList.toggle("cloud", eng.kind === "cloud" || eng.kind === "custom");
}

// User picked an engine in the topbar — apply it AND remember it as the default
// so it persists across reloads (server-side, like the other settings).
function selectEngine(eng) {
  if (!eng) return;
  applyEngine(eng);
  if (state.defaultEngine !== eng.id) {
    state.defaultEngine = eng.id;
    fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ DEFAULT_ENGINE: eng.id }),
    }).catch(() => {});
  }
}

async function loadEngineStats() {
  try {
    const data = await fetch("/api/auto-chain").then(r => r.json());
    const map = {};
    (data.ladder || []).forEach(x => { map[x.engine] = x; });
    state.engineStats = map;
  } catch (e) {}
}

function renderEngineMenu() {
  const menu = $("#engineMenu");
  menu.innerHTML = "";
  const stats = state.engineStats || {};
  state.engines.forEach(eng => {
    const opt = el("div", "engine-opt" + (eng.id === state.engine ? " sel" : ""));
    opt.setAttribute("data-kind", eng.kind);
    opt.setAttribute("role", "option");
    const kind = el("span", "eo-kind"); kind.textContent = eng.kind;
    const txt = el("div", "eo-text");
    const nm = el("span", "eo-name"); nm.textContent = eng.label;
    const md = el("span", "eo-model"); md.textContent = eng.model;
    txt.append(nm, md); opt.append(kind, txt);
    const s = stats[eng.label];
    if (s) {                                // live smartness score + observed speed
      const badge = el("span", "eo-score");
      const tps = s.observed && s.observed.tokens_per_sec;
      badge.textContent = tps ? `${s.score} · ${tps}t/s` : `${s.score}`;
      badge.title = "Capability score" + (tps ? `, ~${tps} tokens/sec observed` : "");
      opt.append(badge);
    }
    opt.addEventListener("click", () => {
      selectEngine(eng); menu.hidden = true;
    });
    menu.appendChild(opt);
  });
}

function positionEngineMenu() {
  const pill = $("#enginePill");
  const menu = $("#engineMenu");
  const r = pill.getBoundingClientRect();
  menu.style.top = (r.bottom + 8) + "px";
  menu.style.left = "auto";
  menu.style.right = Math.max(8, window.innerWidth - r.right) + "px";
}

function toggleEngineMenu() {
  const menu = $("#engineMenu");
  if (menu.hidden) {
    if (menu.parentElement !== document.body) document.body.appendChild(menu);
    renderEngineMenu();
    positionEngineMenu();
    menu.hidden = false;
    loadEngineStats().then(() => { if (!menu.hidden) renderEngineMenu(); });
  } else {
    menu.hidden = true;
  }
}

/* ----------------------------- projects ----------------------------- */
async function loadProjects() {
  try { state.projects = (await fetch("/api/projects").then(r => r.json())).projects || []; }
  catch (e) { state.projects = []; }
  renderProjectFilter();
}
function renderProjectFilter() {
  const sel = $("#projectFilter");
  const cur = state.projectFilter;
  sel.innerHTML = "";
  sel.append(new Option("Ungrouped", "none"));
  sel.append(new Option("Everything", "all"));
  state.projects.forEach(p => sel.append(new Option(`${p.name} (${p.count})`, String(p.id))));
  sel.value = cur;
}
async function createProject() {
  const name = (window.prompt("New project name:") || "").trim();
  if (!name) return;
  try {
    const d = await fetch("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    state.projects = d.projects || [];
    const created = state.projects.find(p => p.name === name);
    state.projectFilter = created ? String(created.id) : "none";
    renderProjectFilter();
    await loadConversations();
    newConversation();
    showToast("Project created.");
  } catch (e) { showToast("Could not create project.", true); }
}

/* ----------------------------- conversations ----------------------------- */
let _convSearchTimer = null;

async function searchConversations(term) {
  const list = $("#convList"), label = $("#convLabel");
  const q = (term || "").trim();
  if (q.length < 2) {
    if (label) label.textContent = "Conversations";
    await loadConversations();
    return;
  }
  try {
    // searches across groups on purpose: when you're looking for something
    // you remember saying, you rarely remember which group you filed it in
    const d = await fetch("/api/conversations/search?q="
      + encodeURIComponent(q) + "&project=all").then(r => r.json());
    list.innerHTML = "";
    if (label) {
      label.textContent = d.results.length
        ? `${d.results.length} conversation(s) mentioning “${q}”`
        : `Nothing mentions “${q}”`;
    }
    (d.results || []).forEach(r => {
      const row = el("button", "conv-item");
      row.type = "button";
      const t = el("div", "conv-title");
      t.textContent = r.title;
      const s = el("div", "conv-snippet");
      s.textContent = (r.who === "user" ? "you: " : r.who ? "Jo: " : "")
        + r.snippet;
      row.append(t, s);
      if (r.hits > 1) {
        const n = el("span", "conv-hits");
        n.textContent = `${r.hits}×`;
        t.appendChild(n);
      }
      row.addEventListener("click", () => {
        const box = $("#convSearch");
        if (box) box.value = "";
        openConversation(r.id);
      });
      list.appendChild(row);
    });
    if (!(d.results || []).length) {
      emptyPane(list, "Nothing found",
                d.note || "Try a word you remember using.");
    }
  } catch (e) {
    if (label) label.textContent = "Conversations";
  }
}

async function loadConversations() {
  try {
    const d = await fetch("/api/conversations?project=" + encodeURIComponent(state.projectFilter)).then(r => r.json());
    state.conversations = (d.conversations || []).map(c => ({
      id: c.id, title: c.title, project_id: c.project_id,
      messages: [], loaded: false, unsaved: false,
    }));
  } catch (e) { state.conversations = []; }
  renderConvList();
}
function newConversation() {
  const pid = (/^\d+$/.test(state.projectFilter)) ? parseInt(state.projectFilter, 10) : null;
  const conv = {
    id: "web-" + cryptoId(), title: "New conversation",
    messages: [], project_id: pid, loaded: true, unsaved: true,
  };
  state.conversations.unshift(conv);
  state.activeId = conv.id;
  renderConvList();
  renderActive();
}
function activeConv() { return state.conversations.find(c => c.id === state.activeId); }

async function openConversation(cid) {
  const conv = state.conversations.find(c => c.id === cid);
  if (!conv) return;
  state.activeId = cid;
  if (!conv.loaded) {
    try {
      const d = await fetch("/api/conversations/" + encodeURIComponent(cid)).then(r => r.json());
      conv.messages = (d.messages || []).map(m => ({ role: m.role, text: m.text }));
      conv.project_id = d.project_id;
      conv.loaded = true;
    } catch (e) { showToast("Could not load that conversation.", true); }
  }
  renderConvList(); renderActive(); closeSidebar();
}

function renderConvList() {
  const list = $("#convList");
  list.innerHTML = "";
  if (!state.conversations.length) {
    const e = el("div", "conv-empty"); e.textContent = "No conversations yet."; list.appendChild(e); return;
  }
  state.conversations.forEach(c => {
    const item = el("div", "conv-item" + (c.id === state.activeId ? " active" : ""));
    const dot = el("span", "dot");
    const txt = el("span", "txt"); txt.textContent = c.title;
    const keb = el("button", "conv-kebab"); keb.textContent = "⋯"; keb.title = "Options";
    keb.addEventListener("click", (e) => { e.stopPropagation(); openConvMenu(c, keb); });
    item.append(dot, txt, keb);
    item.addEventListener("click", () => openConversation(c.id));
    list.appendChild(item);
  });
}

function openConvMenu(conv, anchor) {
  closeConvMenu();
  const menu = el("div", "conv-menu");
  const mk = (label, cls, fn) => {
    const b = el("button", cls || "");
    b.textContent = label;
    b.addEventListener("click", (e) => { e.stopPropagation(); closeConvMenu(); fn(); });
    return b;
  };
  menu.appendChild(mk("Rename", "", () => renameConversation(conv)));
  const lbl = el("div", "menu-label"); lbl.textContent = "Move to"; menu.appendChild(lbl);
  menu.appendChild(mk("No project", "", () => moveConversation(conv, null)));
  state.projects.forEach(p => menu.appendChild(mk(p.name, "", () => moveConversation(conv, p.id))));
  menu.appendChild(mk("Delete", "danger", () => deleteConversation(conv)));
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + "px";
  menu.style.left = Math.min(r.left, window.innerWidth - 190) + "px";
  state._convMenu = menu;
  setTimeout(() => document.addEventListener("click", closeConvMenu, { once: true }), 0);
}
function closeConvMenu() { if (state._convMenu) { state._convMenu.remove(); state._convMenu = null; } }

async function renameConversation(conv) {
  const title = (window.prompt("Rename conversation:", conv.title) || "").trim();
  if (!title) return;
  conv.title = title; renderConvList();
  if (conv.id === state.activeId) $("#convTitle").textContent = title;
  if (!conv.unsaved) {
    try {
      await fetch("/api/conversations/" + encodeURIComponent(conv.id) + "/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
    } catch (e) {}
  }
}
async function moveConversation(conv, projectId) {
  conv.project_id = projectId;
  if (!conv.unsaved) {
    try {
      await fetch("/api/conversations/" + encodeURIComponent(conv.id) + "/project", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
    } catch (e) {}
  }
  await loadProjects();
  if (state.projectFilter !== "all") {
    await loadConversations();
    if (!activeConv()) newConversation();
  }
  showToast("Moved.");
}
async function deleteConversation(conv) {
  if (!window.confirm("Delete this conversation? This removes its messages too.")) return;
  if (!conv.unsaved) {
    try { await fetch("/api/conversations/" + encodeURIComponent(conv.id), { method: "DELETE" }); } catch (e) {}
  }
  state.conversations = state.conversations.filter(c => c.id !== conv.id);
  if (state.activeId === conv.id) {
    if (state.conversations.length) openConversation(state.conversations[0].id);
    else newConversation();
  } else renderConvList();
  loadProjects();
}

function renderActive() {
  const conv = activeConv();
  $("#convTitle").textContent = conv ? conv.title : "New conversation";
  const wrap = $("#messages");
  wrap.innerHTML = "";
  if (conv) { conv._liveRow = null; conv._liveContent = null; }
  const empty = $("#emptyState");
  if (!conv || conv.messages.length === 0) {
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
    conv.messages.forEach(m => {
      const r = renderMessage(m);
      // a conversation still streaming in the background: re-bind its live row
      if (conv.streaming && m === conv.streamingMsg) {
        conv._liveRow = r;
        conv._liveContent = r.querySelector(".msg-content");
        if (!m.text) {
          conv._liveContent.innerHTML =
            `<div class="status-row"><span class="spinner"></span><span class="status-phase">${escapeHtml(conv.streamingPhase || "working")}</span></div>`;
        } else {
          conv._liveContent.classList.add("caret-blink");
        }
      }
      wrap.appendChild(r);
    });
    scrollDown();
  }
  setComposerBusy();
  renderTurnStatus();
}

/* ----------------------------- message rendering ----------------------------- */
function renderMessage(m) {
  const row = el("div", "msg " + (m.role === "user" ? "you-row" : "ai-row") + (m.error ? " error" : ""));
  const av = el("div", "msg-avatar " + (m.role === "user" ? "you" : "ai"));
  if (m.role === "user") av.textContent = initials();
  else { const img = el("img"); img.src = "/avatar"; img.alt = ""; av.appendChild(img); }
  const body = el("div", "msg-body");
  const name = el("div", "msg-name"); name.textContent = m.role === "user" ? "You" : "Agent Jo";
  body.append(name);
  if (m.files && m.files.length) {
    const at = el("div", "msg-attachments");
    m.files.forEach(fn => { const a = el("span", "msg-attach"); a.textContent = fn; at.appendChild(a); });
    body.appendChild(at);
  }
  const content = el("div", "msg-content");
  if (m.role === "user") {
    content.innerHTML = escapeHtml(m.text).replace(/\n/g, "<br>");
  } else if (m.error) {
    content.innerHTML = escapeHtml(m.text || "");
    row.classList.add("error");
  } else {
    content.innerHTML = renderMarkdown(m.text || "");
  }
  body.append(content);
  if (m.tag) body.appendChild(renderTag(m.tag));
  if (m.role === "assistant") {
    const sp = el("button", "msg-speak"); sp.type = "button";
    sp.innerHTML = "<span>◍</span> Speak";
    sp.addEventListener("click", () => {
      if (window.speechSynthesis && window.speechSynthesis.speaking) { stopSpeaking(); return; }
      speakText(m.text);
    });
    body.appendChild(sp);
  }
  row.append(av, body);
  return row;
}

function renderTag(tag) {
  const t = el("div", "msg-tag");
  const parts = [];
  if (tag.engine) parts.push(`<b>${escapeHtml(tag.engine)}</b>`);
  if (tag.model) parts.push(escapeHtml(shortModel(tag.model)));
  if (tag.usage) {
    const u = tag.usage;
    const tot = (u.in || 0) + (u.out || 0);
    if (tot) parts.push(`${fmt(u.in)}→${fmt(u.out)} tok`);
  }
  t.innerHTML = parts.join('<span class="sep"> · </span>');
  return t;
}

function initials() { return "ME"; }
function shortModel(m) { return String(m).split("/").pop(); }
function fmt(n) { n = n || 0; return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }

/* ----------------------------- sending / streaming ----------------------------- */
// Streaming state lives on the conversation, so several conversations can run at
// once. The live DOM target is resolved through conv._liveContent (re-acquired by
// renderActive when you switch back), and the composer's busy state tracks only
// the *active* conversation.
function setComposerBusy() {
  const c = activeConv();
  const busy = !!(c && c.streaming);
  $("#sendBtn").disabled = busy;
  $("#input").disabled = busy;
  $("#enginePill").classList.toggle("busy", busy);
}
let _paintRaf = 0;
function paintToken(conv) {
  if (conv.id !== state.activeId || !conv._liveContent) return;
  conv._needsPaint = true;
  if (_paintRaf) return;                  // coalesce bursts of tokens into one paint/frame
  _paintRaf = requestAnimationFrame(() => {
    _paintRaf = 0;
    const c = activeConv();
    if (!c || !c._needsPaint || !c._liveContent || !c.streamingMsg) return;
    c._needsPaint = false;
    c._liveContent.innerHTML = renderMarkdown(c.streamingMsg.text);
    c._liveContent.classList.add("caret-blink");
    scrollDownSoft();
  });
}
function paintStatus(conv, phase) {
  conv.streamingPhase = phase;
  if (conv.id !== state.activeId) return;
  const ph = conv._liveContent && conv._liveContent.querySelector(".status-phase");
  if (ph) ph.textContent = phase;
  renderTurnStatus();
}

// A persistent progress bar for the active conversation's turn: phase + a live
// elapsed timer + a Stop button + an indeterminate bar. Visible for the whole
// turn (the in-bubble spinner vanishes once text starts streaming), so a long
// step never looks like a frozen app.
function activeStreamingConv() {
  const c = activeConv();
  return c && c.streaming ? c : null;
}
function renderTurnStatus() {
  const bar = $("#turnStatus");
  const c = activeStreamingConv();
  if (!c) { bar.hidden = true; bar.classList.remove("stalled"); return; }
  bar.hidden = false;
  const phase = c.streamingPhase || "Working…";
  $("#turnPhase").textContent = phase;
  bar.classList.toggle("stalled", /still working|⚠|failed/i.test(phase));
  const secs = c._streamStart ? Math.floor((Date.now() - c._streamStart) / 1000) : 0;
  $("#turnElapsed").textContent = secs + "s";
}

async function send(text) {
  const conv = activeConv();
  if (!conv || conv.streaming) return;            // one in-flight turn per conversation
  const files = state.pendingFiles.slice();
  state.pendingFiles = []; renderChips();
  if (!text && !files.length) return;
  if (conv.messages.length === 0) {
    const base = text || (files[0] && files[0].name) || "New conversation";
    conv.title = base.slice(0, 42) + (base.length > 42 ? "…" : "");
    renderConvList();
    $("#convTitle").textContent = conv.title;
  }
  $("#emptyState").classList.add("hidden");

  const userMsg = { role: "user", text, files: files.map(f => f.name) };
  conv.messages.push(userMsg);
  $("#messages").appendChild(renderMessage(userMsg));

  const aiMsg = { role: "assistant", text: "" };
  conv.messages.push(aiMsg);
  conv.streaming = true;
  conv.streamingMsg = aiMsg;
  conv.streamingPhase = "preparing";
  conv._streamStart = Date.now();
  conv._abort = new AbortController();
  const row = renderMessage(aiMsg);
  conv._liveRow = row;
  conv._liveContent = row.querySelector(".msg-content");
  conv._liveContent.innerHTML =
    `<div class="status-row"><span class="spinner"></span><span class="status-phase">preparing</span></div>`;
  $("#messages").appendChild(row);
  scrollDown();
  setComposerBusy();
  renderTurnStatus();

  let started = false;
  try {
    const fd = new FormData();
    fd.append("message", text);
    fd.append("conversation_id", conv.id);
    fd.append("engine", state.engine);
    fd.append("full_access", $("#fullAccess").checked ? "true" : "false");
    fd.append("second_opinion", $("#secondOpinion").checked ? "true" : "false");
    files.forEach(f => fd.append("files", f, f.name));

    const resp = await fetch("/api/chat", { method: "POST", body: fd, signal: conv._abort.signal });
    if (!resp.ok || !resp.body) {
      let detail = "Request failed.";
      try { detail = (await resp.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop();
      for (const ev of events) {
        const line = ev.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        let data;
        try { data = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }

        if (data.type === "status") {
          paintStatus(conv, data.phase || "working");
        } else if (data.type === "ping") {
          renderTurnStatus();                 // heartbeat: keep the timer alive
        } else if (data.type === "token") {
          if (!started) {
            started = true;
            if (conv.id === state.activeId && conv._liveContent) {
              conv._liveContent.innerHTML = "";
              conv._liveContent.classList.add("caret-blink");
            }
          }
          aiMsg.text += data.text;
          paintToken(conv);
        } else if (data.type === "done") {
          aiMsg.text = data.reply || aiMsg.text;
          aiMsg.tag = { engine: data.engine, model: data.model, usage: data.usage };
          if (conv.id === state.activeId && conv._liveContent) {
            conv._liveContent.classList.remove("caret-blink");
            conv._liveContent.innerHTML = renderMarkdown(aiMsg.text);
            if (aiMsg.tag.engine || aiMsg.tag.model) conv._liveRow.querySelector(".msg-body").appendChild(renderTag(aiMsg.tag));
          }
        } else if (data.type === "error") {
          throw new Error(data.message || "Something went wrong.");
        }
      }
    }
  } catch (err) {
    const aborted = err && (err.name === "AbortError" || conv._stopRequested);
    aiMsg.text = aborted ? "_(Stopped.)_" : (err.message || String(err));
    aiMsg.error = !aborted;
    aiMsg.stopped = aborted;
    if (conv.id === state.activeId && conv._liveContent) {
      conv._liveContent.classList.remove("caret-blink");
      if (aborted) {
        conv._liveContent.innerHTML = renderMarkdown(aiMsg.text);
      } else {
        conv._liveRow.classList.add("error");
        conv._liveContent.innerHTML = escapeHtml(aiMsg.text);
      }
    }
  } finally {
    conv.streaming = false;
    conv.streamingMsg = null;
    conv._abort = null;
    conv._stopRequested = false;
    if (conv.id === state.activeId) { setComposerBusy(); scrollDown(); }
    renderTurnStatus();
    refreshStats();
  }

  // first turn just persisted this conversation server-side: mark it saved,
  // apply any pending project assignment, and refresh project counts
  if (conv.unsaved && !aiMsg.error) {
    conv.unsaved = false;
    if (conv.project_id != null) {
      try {
        await fetch("/api/conversations/" + encodeURIComponent(conv.id) + "/project", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: conv.project_id }),
        });
      } catch (e) {}
    }
    loadProjects();
  }

  if (state.autoSpeak && !aiMsg.error && !aiMsg.stopped && aiMsg.text) speakText(aiMsg.text);
}

function renderChips() {
  const wrap = $("#attachChips");
  wrap.innerHTML = "";
  if (!state.pendingFiles.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  state.pendingFiles.forEach((f, i) => {
    const chip = el("div", "attach-chip");
    const ext = el("span", "ext"); ext.textContent = (f.name.split(".").pop() || "file").slice(0, 5);
    const nm = el("span", "nm"); nm.textContent = f.name;
    const rm = el("button", "rm"); rm.type = "button"; rm.textContent = "✕";
    rm.addEventListener("click", () => { state.pendingFiles.splice(i, 1); renderChips(); });
    chip.append(ext, nm, rm); wrap.appendChild(chip);
  });
}

/* ----------------------------- engines modal ----------------------------- */
function openEngineModal() {
  const m = $("#engineModal");
  m.hidden = false; m.style.display = "flex";   // inline beats .modal-backdrop{display:flex}
  renderCustomEngines();
  setEngineStatus("", "");
}
function closeEngineModal() {
  const m = $("#engineModal");
  m.hidden = true; m.style.display = "none";
}

async function enableLocalEngines() {
  const btn = $("#enableLocalBtn");
  btn.disabled = true; btn.textContent = "Enabling…";
  try {
    const r = await fetch("/api/engines/enable-local", { method: "POST" }).then(r => r.json());
    state.engines = r.engines || state.engines;
    renderCustomEngines();
    if (r.reachable === false) {
      showToast(r.detail || "Ollama isn't reachable — start it and try again");
    } else if (r.added && r.added.length) {
      showToast(`${r.added.length} local model(s) now selectable: ${r.added.join(", ")}`);
    } else {
      showToast(`Local models already enabled (installed: ${(r.installed || []).join(", ") || "none"})`);
    }
  } catch (e) { showToast("Could not enable local models"); }
  btn.disabled = false; btn.textContent = "Make local models selectable";
}
function renderCustomEngines() {
  const list = $("#customEngineList");
  list.innerHTML = "";
  // `kind` now says cloud or local — what the engine IS, which is what
  // routing and the spend cap need. Whether YOU added it is a separate fact,
  // and filtering on kind here would have emptied this list.
  // The built-in Claude row is shown here too, with a Take over button —
  // "add an engine called Claude" was the right mechanism but an indirect
  // instruction, and the whole complaint was that it wasn't editable.
  const customs = state.engines.filter(e => e.custom || e.seed);
  if (customs.length === 0) {
    const empty = el("div", "ce-empty"); empty.textContent = "None yet - add one above.";
    list.appendChild(empty); return;
  }
  customs.forEach(e => {
    const row = el("div", "ce-row");
    const info = el("div", "ce-info");
    const nm = el("div", "ce-name"); nm.textContent = e.label;
    const meta = el("div", "ce-meta");
    const pin = e.price_in || 0, pout = e.price_out || 0;
    meta.textContent = e.model + (pin || pout
      ? ` · $${pin}/$${pout} per M tok` : " · no price set (cost shown as $0)");
    info.append(nm, meta);
    const tag = el("span", "ce-kind " + (e.kind || ""));
    tag.textContent = e.kind === "local" ? "local · free" : "cloud";
    tag.title = e.kind === "local"
      ? "Runs on this machine, so it costs nothing"
      : "Calls leave this machine and are billed";
    info.appendChild(tag);
    const ed = el("button", "ce-remove");
    ed.textContent = e.seed ? "Take it over" : "Edit";
    ed.addEventListener("click", () => {
      if (!e.seed) { editEngine(e.id); return; }
      // seed the form from the built-in so it can be edited like any other
      $("#fName").value = e.id;
      $("#fModel").value = e.model || "claude-sonnet-4-6";
      $("#fUrl").value = "https://api.anthropic.com/v1";
      $("#fKey").value = "";
      $("#fKey").placeholder = "your Anthropic API key";
      $("#fTools").checked = true;
      $("#fStream").checked = true;
      _editingEngine = "";
      setEngineStatus("Fill in your key and press Save engine. Yours will "
                      + "replace the built-in, and can be edited or removed "
                      + "afterwards.", "");
    });
    row.append(info, ed);
    if (!e.seed) {
      const rm = el("button", "ce-remove"); rm.textContent = "Remove";
      rm.addEventListener("click", () => removeEngine(e.id));
      row.appendChild(rm);
    }
    list.appendChild(row);
  });
}

function engineForm() {
  return {
    name: $("#fName").value.trim(),
    model: $("#fModel").value.trim(),
    base_url: $("#fUrl").value.trim(),
    api_key: $("#fKey").value.trim(),
    tools: $("#fTools").checked,
    stream: $("#fStream").checked,
    price_in: parseFloat($("#fPriceIn").value) || 0,
    price_out: parseFloat($("#fPriceOut").value) || 0,
  };
}

async function saveEngine() {
  const f = engineForm();
  if (!f.name || !f.model || !f.base_url) {
    setEngineStatus("Name, model id and base URL are required.", "err"); return;
  }
  setEngineStatus("Saving…", "");
  try {
    if (_editingEngine) {
      const f2 = engineForm();
      const res = await fetch("/api/engines/edit", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: _editingEngine,
          new_name: f2.name !== _editingEngine ? f2.name : null,
          base_url: f2.base_url, model: f2.model,
          api_key: f2.api_key || null,
          tools: f2.tools, stream: f2.stream,
          price_in: f2.price_in, price_out: f2.price_out }) });
      const d2 = await res.json();
      if (!res.ok) { setEngineStatus(d2.detail || "Could not save.", "err"); return; }
      state.engines = d2.engines;
      _editingEngine = "";
      const b2 = $("#saveEngineBtn");
      if (b2) b2.textContent = "Save engine";
      clearEngineForm();
      renderCustomEngines();
      showToast(d2.message);
      return;
    }
    const r = await fetch("/api/engines", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(f),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Could not save engine.");
    state.engines = data.engines;
    renderCustomEngines();
    setEngineStatus(`Saved "${f.name}".`, "ok");
    clearEngineForm();
    showToast(`Engine "${f.name}" added.`);
  } catch (e) { setEngineStatus(e.message, "err"); }
}

async function editEngine(name) {
  // Correcting a typo used to mean deleting the engine and adding it again,
  // which lost the key and any feature pinned to it.
  try {
    const e = await fetch("/api/engines/" + encodeURIComponent(name))
      .then(r => r.json());
    $("#fName").value = e.name || "";
    $("#fModel").value = e.model || "";
    $("#fUrl").value = e.base_url || "";
    $("#fKey").value = "";
    $("#fKey").placeholder = e.has_key
      ? `leave blank to keep the saved key (${e.key_hint})`
      : "API key";
    $("#fPriceIn").value = e.price_in || "";
    $("#fPriceOut").value = e.price_out || "";
    $("#fTools").checked = e.tools !== false;
    $("#fStream").checked = e.stream !== false;
    _editingEngine = name;
    const btn = $("#saveEngineBtn");
    if (btn) btn.textContent = "Save changes";
    setEngineStatus(`Editing “${name}”. Leave the key blank to keep the one `
                    + `already saved.`, "");
  } catch (err) { showToast("Could not load that engine.", true); }
}

let _editingEngine = "";

async function removeEngine(name) {
  try {
    const r = await fetch("/api/engines/" + encodeURIComponent(name), { method: "DELETE" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Could not remove.");
    state.engines = data.engines;
    if (state.engine === name) applyEngine(state.engines[0]);
    renderCustomEngines();
    showToast(`Removed "${name}".`);
  } catch (e) { showToast(e.message, true); }
}

async function testEngine() {
  const f = engineForm();
  if (!f.base_url || !f.model) { setEngineStatus("Enter a base URL and model id to test.", "err"); return; }
  setEngineStatus("Testing connection…", "");
  // Save-then-noop isn't ideal; instead do a lightweight add+chat probe would
  // need a key. We surface the server's validation by attempting a save in a
  // dry manner: here we just validate fields client-side and tell the user to
  // save, since a real probe runs server-side on first use.
  setEngineStatus("Looks valid. Save it, then select it and send a message to confirm the endpoint responds.", "ok");
}

function setEngineStatus(msg, kind) {
  const s = $("#engineStatus"); s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}
function clearEngineForm() {
  _editingEngine = "";
  const b = $("#saveEngineBtn");
  if (b) b.textContent = "Save engine";
  $("#fKey").placeholder = "API key";
  ["fName", "fModel", "fUrl", "fKey", "fPriceIn", "fPriceOut"].forEach(id => $("#" + id).value = "");
  $("#fTools").checked = true; $("#fStream").checked = true;
}

/* ----------------------------- helpers ----------------------------- */
function scrollDown() { const t = $("#thread"); t.scrollTop = t.scrollHeight; }
let softTimer = null;
function scrollDownSoft() {
  const t = $("#thread");
  const near = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
  if (near) t.scrollTop = t.scrollHeight;
}
function closeSidebar() { $("#sidebar").classList.remove("open"); }

function showToast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 3600);
}

function cryptoId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "c" + Date.now() + Math.random().toString(16).slice(2);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Minimal, safe markdown: code fences, inline code, bold, italic, headings,
   lists, links, paragraphs. Everything is escaped first. */
function isTableRow(line) {
  // Any line with a pipe is a candidate; what makes it a TABLE is the row of
  // dashes underneath. Demanding an internal pipe rejected single-column
  // tables ("| A |"), which is how prose either side of one went unstyled.
  const t = (line || "").trim();
  return t.includes("|") && t.replace(/[|\s]/g, "").length > 0;
}

function isTableDivider(line) {
  const t = (line || "").trim();
  if (!t.includes("-") || !t.includes("|")) return false;
  return splitRow(t).every(c => /^:?-{1,}:?$/.test(c.trim()));
}

function splitRow(line) {
  let t = (line || "").trim();
  // a leading and trailing pipe are optional in markdown
  t = t.replace(/^\|/, "").replace(/\|$/, "");
  const out = [];
  let cur = "";
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && t[i + 1] === "|") { cur += "|"; i++; continue; }
    if (c === "|") { out.push(cur.trim()); cur = ""; continue; }
    cur += c;
  }
  out.push(cur.trim());
  return out;
}

function alignOf(spec) {
  const s = (spec || "").trim();
  if (s.startsWith(":") && s.endsWith(":")) return "center";
  if (s.endsWith(":")) return "right";
  return "";
}

function buildTable(head, align, body) {
  const cell = (text, tag, i) => {
    const a = align[i] ? ` style="text-align:${align[i]}"` : "";
    // numbers should line up; a column of figures reads badly ragged
    const num = /^[-+]?[\d,. %$£€R]+$/.test(text) && /\d/.test(text);
    const cls = num && !align[i] ? ' class="num"' : "";
    return `<${tag}${a}${cls}>${text}</${tag}>`;
  };
  let h = '<div class="table-wrap"><table><thead><tr>';
  head.forEach((c, i) => { h += cell(c, "th", i); });
  h += "</tr></thead><tbody>";
  body.forEach(row => {
    h += "<tr>";
    head.forEach((_, i) => { h += cell(row[i] === undefined ? "" : row[i], "td", i); });
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

function renderMarkdown(src) {
  src = String(src);
  const blocks = [];
  // fenced code
  src = src.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, code) => {
    const i = blocks.length;
    blocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000B${i}\u0000`;
  });
  src = escapeHtml(src);
  // inline code
  src = src.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  // bold / italic
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  // links [text](url)
  src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const lines = src.split("\n");
  let html = "", listType = null, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.join("<br>")}</p>`; para = []; } };
  const flushList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, "");
    // A markdown table is a header row, a separator of dashes, then rows.
    // Without this they fell through as paragraphs and you read the pipes.
    if (isTableRow(line) && isTableDivider(lines[i + 1] || "")) {
      flushPara(); flushList();
      const head = splitRow(line);
      const align = splitRow(lines[i + 1]).map(alignOf);
      const body = [];
      let k = i + 2;
      while (k < lines.length && isTableRow(lines[k])) {
        body.push(splitRow(lines[k]));
        k++;
      }
      html += buildTable(head, align, body);
      i = k - 1;
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const ul = line.match(/^[-*]\s+(.*)$/);
    const ol = line.match(/^\d+\.\s+(.*)$/);
    if (h) { flushPara(); flushList(); html += `<h${h[1].length}>${h[2]}</h${h[1].length}>`; }
    else if (ul) { flushPara(); if (listType !== "ul") { flushList(); html += "<ul>"; listType = "ul"; } html += `<li>${ul[1]}</li>`; }
    else if (ol) { flushPara(); if (listType !== "ol") { flushList(); html += "<ol>"; listType = "ol"; } html += `<li>${ol[1]}</li>`; }
    else if (line.trim() === "") { flushPara(); flushList(); }
    else { if (listType) flushList(); para.push(line); }
  }
  flushPara(); flushList();
  // restore code blocks
  html = html.replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);
  return html;
}

/* ----------------------------- settings ----------------------------- */
async function openSettings() {
  const m = $("#settingsModal");
  m.hidden = false; m.style.display = "flex";
  setSettingsStatus("Loading…", "");
  try {
    const data = await fetch("/api/settings").then(r => r.json());
    renderSettings(data.settings || {});
    loadModelPickers();
    setSettingsStatus("", "");
  } catch (e) { setSettingsStatus("Could not load settings.", "err"); }
  renderSecurity();
}
function closeSettings() {
  const m = $("#settingsModal");
  m.hidden = true; m.style.display = "none";
}

/* ------------------------------- outreach ------------------------------- */
async function openOutreach() {
  const m = $("#outreachModal");
  m.hidden = false; m.style.display = "flex";
  switchOutTab("setup");
  setOut("emStatus", "Loading…", "");
  try {
    const s = await fetch("/api/email/status").then(r => r.json());
    $("#emHost").value = s.host || "";
    $("#emPort").value = s.port || 587;
    $("#emUser").value = s.username || "";
    $("#emFrom").value = s.from_addr || "";
    $("#emName").value = s.from_name || "";
    $("#emFooter").value = s.footer || "";
    $("#emTls").checked = !!s.use_tls;
    $("#emEnabled").checked = !!s.enabled;
    $("#emPass").placeholder = s.has_password ? "•••••• (saved — leave blank to keep)" : "Password / app-password";
    setOut("emStatus", s.configured
      ? (s.enabled ? "Configured · sending ON" : "Configured · sending OFF (drafts only)")
      : "Not configured yet", s.enabled ? "ok" : "");
    renderAutopilot(s);
  } catch (e) { setOut("emStatus", "Could not load settings.", "err"); }
}
function renderAutopilot(s) {
  $("#apEnabled").checked = !!s.autonomous_enabled;
  $("#apRequireAllow").checked = s.require_allowlist !== false;
  $("#apMaxPerRun").value = s.max_per_run || 25;
  $("#apRecipients").value = (s.allowed_recipients || []).join(", ");
  $("#apDomains").value = (s.allowed_domains || []).join(", ");
  const armed = !!s.autonomous_enabled;
  const st = $("#apState");
  st.textContent = "Auto-pilot: " + (armed ? "ARMED — sending unattended" : "disarmed");
  st.className = "ap-state" + (armed ? " armed" : "");
  $("#apArmRow").classList.toggle("armed", armed);
}
function closeOutreach() {
  const m = $("#outreachModal");
  m.hidden = true; m.style.display = "none";
}
function switchOutTab(tab) {
  document.querySelectorAll(".out-tab").forEach(t =>
    t.classList.toggle("sel", t.getAttribute("data-tab") === tab));
  document.querySelectorAll(".out-pane").forEach(p =>
    p.hidden = p.getAttribute("data-pane") !== tab);
  if (tab === "log") loadEmailLog();
}
function setOut(id, msg, cls) {
  const e = $("#" + id); if (!e) return;
  e.textContent = msg || "";
  e.className = "engine-status" + (cls === "err" ? " err" : cls === "ok" ? " ok" : "");
}
async function saveEmailConfig() {
  const body = {
    host: $("#emHost").value.trim(), port: parseInt($("#emPort").value, 10) || 587,
    username: $("#emUser").value.trim(), from_addr: $("#emFrom").value.trim(),
    from_name: $("#emName").value.trim(), footer: $("#emFooter").value,
    use_tls: $("#emTls").checked, enabled: $("#emEnabled").checked,
  };
  const pw = $("#emPass").value;
  if (pw) body.password = pw;
  setOut("emStatus", "Saving…", "");
  try {
    const s = await fetch("/api/email/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => r.json());
    $("#emPass").value = "";
    $("#emPass").placeholder = s.has_password ? "•••••• (saved — leave blank to keep)" : "Password / app-password";
    setOut("emStatus", s.enabled ? "Saved · sending ON" : "Saved · sending OFF (drafts only)",
      s.enabled ? "ok" : "");
    showToast("Email settings saved");
  } catch (e) { setOut("emStatus", "Save failed.", "err"); }
}
async function sendEmailTest() {
  const from = $("#emFrom").value.trim();
  if (!from) { setOut("emStatus", "Set a From address first.", "err"); return; }
  setOut("emStatus", "Sending test…", "");
  try {
    const r = await fetch("/api/email/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to: from, subject: "Agent Jo test email",
        body: "This is a test from your Agent Jo Outreach panel. If you got it, sending works.",
        dry_run: false }),
    }).then(r => r.json());
    setOut("emStatus", r.ok ? "Test sent — check your inbox." : ("Test: " + r.error), r.ok ? "ok" : "err");
  } catch (e) { setOut("emStatus", "Test failed.", "err"); }
}
async function quickSend(dry) {
  const to = $("#qsTo").value.trim(), subject = $("#qsSubject").value, body = $("#qsBody").value;
  if (!to) { setOut("qsStatus", "Add a recipient.", "err"); return; }
  setOut("qsStatus", dry ? "Rendering preview…" : "Sending…", "");
  try {
    const r = await fetch("/api/email/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to, subject, body, dry_run: dry }),
    }).then(r => r.json());
    if (r.ok && dry) setOut("qsStatus", "Looks good — preview rendered, nothing sent.", "ok");
    else if (r.ok) { setOut("qsStatus", "Sent to " + r.to, "ok"); showToast("Email sent"); }
    else setOut("qsStatus", r.error, "err");
  } catch (e) { setOut("qsStatus", "Failed.", "err"); }
}
function parseContacts() {
  return $("#cpContacts").value.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const [email, first_name, company] = line.split(",").map(s => (s || "").trim());
    return { email: email || "", first_name: first_name || "", company: company || "" };
  });
}
async function renderCampaignDrafts() {
  const contacts = parseContacts();
  if (!contacts.length) { setOut("cpStatus", "Add at least one recipient.", "err"); return; }
  setOut("cpStatus", "Rendering…", "");
  try {
    const r = await fetch("/api/campaign/draft", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: $("#cpSubject").value, body: $("#cpBody").value, contacts }),
    }).then(r => r.json());
    const box = $("#cpDrafts"); box.innerHTML = "";
    (r.drafts || []).forEach(d => {
      const card = el("div", "draft-card" + (d.valid ? "" : " bad"));
      const h = el("div", "draft-to"); h.textContent = (d.valid ? "✓ " : "✗ ") + (d.to || "(no address)");
      const s = el("div", "draft-subj"); s.textContent = d.subject;
      const b = el("div", "draft-body"); b.textContent = d.body;
      card.append(h, s, b); box.appendChild(card);
    });
    setOut("cpStatus", `${r.valid}/${r.count} ready to send`, r.valid ? "ok" : "err");
  } catch (e) { setOut("cpStatus", "Render failed.", "err"); }
}
async function sendCampaign() {
  const contacts = parseContacts();
  if (!contacts.length) { setOut("cpStatus", "Add recipients and render first.", "err"); return; }
  const confirm = $("#cpConfirm").checked;
  setOut("cpStatus", confirm ? "Sending campaign…" : "Dry run (tick the box to really send)…", "");
  try {
    const r = await fetch("/api/campaign/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: $("#cpSubject").value, body: $("#cpBody").value, contacts, confirm }),
    }).then(r => r.json());
    const verb = r.dry_run ? "previewed" : "sent";
    setOut("cpStatus", `${r.sent} ${verb}, ${r.failed} skipped of ${r.total}` +
      (r.dry_run ? " (dry run — nothing delivered)" : ""), "ok");
    if (!r.dry_run) showToast(`Campaign: ${r.sent} sent`);
  } catch (e) { setOut("cpStatus", "Campaign failed.", "err"); }
}
async function saveAutopilot() {
  const body = {
    autonomous_enabled: $("#apEnabled").checked,
    require_allowlist: $("#apRequireAllow").checked,
    max_per_run: parseInt($("#apMaxPerRun").value, 10) || 25,
    allowed_recipients: $("#apRecipients").value,
    allowed_domains: $("#apDomains").value,
  };
  setOut("apStatus", "Saving…", "");
  try {
    const s = await fetch("/api/email/autopilot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => r.json());
    renderAutopilot(s);
    setOut("apStatus", s.autonomous_enabled ? "Saved · ARMED" : "Saved · disarmed",
      s.autonomous_enabled ? "ok" : "");
    showToast(s.autonomous_enabled ? "Auto-pilot armed" : "Auto-pilot policy saved");
  } catch (e) { setOut("apStatus", "Save failed.", "err"); }
}
async function pauseAutopilot() {
  setOut("apStatus", "Pausing…", "");
  try {
    const s = await fetch("/api/email/autopilot/pause", { method: "POST" }).then(r => r.json());
    renderAutopilot(s);
    setOut("apStatus", "Paused — all autonomous sending disarmed.", "");
    showToast("Auto-pilot paused");
  } catch (e) { setOut("apStatus", "Pause failed.", "err"); }
}
async function runAutopilot(dry) {
  const contacts = $("#apJobContacts").value.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const [email, first_name, company] = line.split(",").map(s => (s || "").trim());
    return { email: email || "", first_name: first_name || "", company: company || "" };
  });
  if (!contacts.length) { setOut("apRunStatus", "Add recipients first.", "err"); return; }
  setOut("apRunStatus", dry ? "Dry run…" : "Running autonomously…", "");
  try {
    const r = await fetch("/api/email/autopilot/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: $("#apJobSubject").value, body: $("#apJobBody").value,
        contacts, dry_run: dry }),
    }).then(r => r.json());
    if (r.ok === false) { setOut("apRunStatus", r.error, "err"); return; }
    const msg = `${r.sent} ${r.dry_run ? "previewed" : "sent"}` +
      (r.blocked ? `, ${r.blocked} blocked (not approved)` : "") +
      (r.skipped ? `, ${r.skipped} skipped` : "") +
      (r.dry_run ? " — dry run, nothing delivered" : "");
    setOut("apRunStatus", msg, "ok");
    if (!r.dry_run && r.sent) showToast(`Auto-pilot: ${r.sent} sent`);
  } catch (e) { setOut("apRunStatus", "Run failed.", "err"); }
}
async function scheduleAutopilot() {
  const contacts = $("#apJobContacts").value.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const [email, first_name, company] = line.split(",").map(s => (s || "").trim());
    return { email: email || "", first_name: first_name || "", company: company || "" };
  });
  if (!contacts.length) { setOut("apSchedStatus", "Add recipients first.", "err"); return; }
  const kind = $("#apSchedKind").value;
  setOut("apSchedStatus", "Scheduling…", "");
  try {
    const r = await fetch("/api/email/autopilot/schedule", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "Outreach: " + ($("#apJobSubject").value || "auto-pilot"),
        subject: $("#apJobSubject").value, body: $("#apJobBody").value, contacts,
        kind, time: $("#apSchedTime").value || "07:00", n: 60, dow: 0,
      }),
    }).then(r => r.json());
    if (r.ok) {
      setOut("apSchedStatus", "Scheduled — runs " + (r.describe || kind) +
        ". Manage it in the Scheduler tab.", "ok");
      showToast("Auto-pilot scheduled");
    } else setOut("apSchedStatus", r.detail || "Could not schedule.", "err");
  } catch (e) { setOut("apSchedStatus", "Schedule failed.", "err"); }
}
async function createWatcher() {
  const source = $("#wSource").value.trim();
  if (!source) { setOut("wStatus", "Add a URL or search query.", "err"); return; }
  setOut("wStatus", "Creating…", "");
  try {
    const r = await fetch("/api/watchers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#wName").value || "Watcher", source_type: $("#wType").value,
        source, instruction: $("#wInstruction").value,
        mode: $("#wModeSend").checked ? "send" : "draft",
        kind: $("#wKind").value, time: $("#wTime").value || "07:00",
        n: 60, dow: 0, full_access: $("#wFull").checked,
      }),
    }).then(r => r.json());
    if (r.ok) {
      setOut("wStatus", "Watching — checks " + (r.describe || "") + ". First check sets a baseline.", "ok");
      showToast("Watcher created");
      $("#wSource").value = ""; $("#wName").value = ""; $("#wInstruction").value = "";
    } else setOut("wStatus", r.detail || "Could not create.", "err");
  } catch (e) { setOut("wStatus", "Failed.", "err"); }
}
/* ------------------------------- autonomy ------------------------------- */
async function openAutonomy() {
  const m = $("#autonomyModal");
  m.hidden = false; m.style.display = "flex";
  await loadAutonomy();
}
function closeAutonomy() {
  const m = $("#autonomyModal");
  m.hidden = true; m.style.display = "none";
}
function fmtTiming(t) {
  if (!t) return "";
  const fmt = (s) => s ? new Date(s * 1000).toLocaleString() : null;
  const bits = [];
  if (t.next_run) bits.push("next " + fmt(t.next_run));
  if (t.last_run) bits.push("last " + fmt(t.last_run) + (t.last_status ? ` (${t.last_status})` : ""));
  return bits.join(" · ");
}
function autoCard(title, armed, lines, onToggle, timing) {
  const c = el("div", "auto-card" + (armed ? " on" : ""));
  const h = el("div", "auto-card-h");
  const nm = el("span", "auto-card-name"); nm.textContent = title;
  if (onToggle) {
    const sw = el("button", "auto-toggle" + (armed ? " on" : ""));
    sw.textContent = armed ? "Armed" : "Off";
    sw.title = "Click to " + (armed ? "disarm" : "arm");
    sw.addEventListener("click", async () => { sw.disabled = true; await onToggle(!armed); });
    h.append(nm, sw);
  } else {
    const pill = el("span", "auto-pill " + (armed ? "on" : "off"));
    pill.textContent = armed ? "ARMED" : "off";
    h.append(nm, pill);
  }
  c.appendChild(h);
  lines.forEach(t => { const l = el("div", "auto-card-line"); l.textContent = t; c.appendChild(l); });
  const tm = fmtTiming(timing);
  if (tm) { const l = el("div", "auto-card-line tm"); l.textContent = tm; c.appendChild(l); }
  return c;
}
async function loadAutonomy() {
  const cards = $("#autoCards"); cards.innerHTML = "<div class='out-empty'>Loading…</div>";
  loadBudget();
  try {
    const d = await fetch("/api/autonomy").then(r => r.json());
    cards.innerHTML = "";
    cards.appendChild(autoCard("Outreach auto-pilot", d.autopilot.armed, [
      `${d.autopilot.allow_recipients} approved recipients, ${d.autopilot.allow_domains} domains`,
      `cap ${d.autopilot.max_per_run}/run · ${d.autopilot.schedules} scheduled job(s)`,
    ], async (on) => {
      await fetch("/api/email/autopilot", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ autonomous_enabled: on }) });
      loadAutonomy();
    }, d.autopilot.timing));
    cards.appendChild(autoCard("Watchers", d.watchers.enabled > 0, [
      `${d.watchers.enabled} active of ${d.watchers.total}`,
      `${d.watchers.send_mode} in send mode, rest observe-only`,
    ], d.watchers.total ? async (on) => {
      await fetch("/api/watchers/enable-all", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
      loadAutonomy();
    } : null, d.watchers.timing));
    cards.appendChild(autoCard("Auto-resume", d.autoresume.armed, [
      `sweep ${d.autoresume.cadence} · up to ${d.autoresume.max_attempts} tries/task`,
      d.autoresume.full_access ? "full access ON (runs commands)" : "non-destructive (full access off)",
    ], async (on) => {
      await fetch("/api/autoresume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
      loadAutonomy();
    }, d.autoresume.timing));
    const feed = $("#autoFeed"); feed.innerHTML = "";
    if (!(d.feed || []).length) { feed.innerHTML = "<div class='out-empty'>No autonomous actions yet.</div>"; }
    (d.feed || []).forEach(e => {
      const row = el("div", "log-row");
      const src = el("span", "log-status"); src.textContent = e.source;
      const meta = el("span", "log-meta");
      const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
      meta.textContent = `${when} · ${e.text}`;
      row.append(src, meta); feed.appendChild(row);
    });
  } catch (e) { cards.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
function renderBudget(b) {
  const fig = $("#budgetFigure"), wrap = $("#budgetBarWrap"), fill = $("#budgetFill");
  if (!b || !b.enabled) {
    fig.textContent = "No cap set";
    fig.className = "budget-figure";
    wrap.hidden = true;
    return;
  }
  fig.textContent = `$${Number(b.spent).toFixed(2)} of $${Number(b.cap).toFixed(2)} (${b.pct}%)`;
  fig.className = "budget-figure" + (b.exceeded ? " over" : b.warn ? " warn" : "");
  wrap.hidden = false;
  fill.style.width = Math.min(100, b.pct) + "%";
  fill.className = "budget-fill" + (b.exceeded ? " over" : b.warn ? " warn" : "");
}
async function loadBudget() {
  try {
    const b = await fetch("/api/budget").then(r => r.json());
    if (document.activeElement !== $("#budgetInput")) {
      $("#budgetInput").value = b.enabled ? b.cap : "";
    }
    renderBudget(b);
  } catch (e) {}
}
async function saveBudget() {
  const cap = parseFloat($("#budgetInput").value) || 0;
  try {
    const b = await fetch("/api/budget", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cap }),
    }).then(r => r.json());
    renderBudget(b);
    showToast(b.enabled ? `Spend cap set to $${Number(b.cap).toFixed(2)}` : "Spend cap cleared");
  } catch (e) {}
}
/* ------------------------------- issues -------------------------------- */
async function copyToClipboard(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove(); return true;
    } catch (e2) { return false; }
  }
}
/* --------------------- drag & drop attach (whole app) ------------------- */
function initChatDragDrop() {
  const overlay = $("#dropOverlay");
  let depth = 0;                       // dragenter/leave fire per child; count
  const hasFiles = (e) => e.dataTransfer &&
    Array.from(e.dataTransfer.types || []).includes("Files");
  const inOtherDropZone = (e) => e.target && e.target.closest &&
    (e.target.closest("#docDrop") || e.target.closest(".modal-backdrop"));

  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e) || inOtherDropZone(e)) return;
    e.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();                // required or the browser opens the file
    if (!inOtherDropZone(e)) e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) overlay.hidden = true;
  });
  window.addEventListener("drop", (e) => {
    depth = 0; overlay.hidden = true;
    if (!hasFiles(e)) return;
    if (inOtherDropZone(e)) return;    // Documents drop zone keeps its job
    e.preventDefault();                // never let the browser navigate away
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) return;
    state.pendingFiles.push(...files); // identical path to the attach button
    renderChips();
    showToast(files.length === 1
      ? `Attached ${files[0].name}`
      : `Attached ${files.length} files`);
    const inp = $("#input");
    if (inp) inp.focus();
  });
}
/* ------------------------- collapsible sidebar groups ------------------- */
function initFootGroups() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("footGroups") || "{}"); } catch (e) {}
  document.querySelectorAll(".foot-group").forEach(g => {
    const key = g.dataset.group;
    if (saved[key]) g.classList.add("collapsed");
    const t = g.querySelector(".foot-group-toggle");
    if (!t) return;
    t.addEventListener("click", () => {
      g.classList.toggle("collapsed");
      saved[key] = g.classList.contains("collapsed");
      try { localStorage.setItem("footGroups", JSON.stringify(saved)); } catch (e) {}
    });
  });
}
function closeAllModals() {
  $("#engineMenu").hidden = true;
  closeEngineModal();
  closeSettings();
  closeDocuments();
  closeMemory();
  closePerms();
  closeSched();
  closeOutreach();
  closeAutonomy();
  closeIssues();
  closeMcp();
  closeAudit();
  closeUndo();
  closeSelf();
  closeTrends();
  closeCrew();
  closeBackup();
  closeJobs();
  closeHealth();
  closeCaps();
  closeSetup();
  closeSkills();
  closeTour();
  closeChallenges();
}

/* ------------------------------- feedback ------------------------------- */
function errText(d, fallback) {
  // FastAPI returns 422 details as a LIST of objects. Interpolating that
  // into a string gives "[object Object]", which is how an error that names
  // the exact missing field arrives telling you nothing.
  const x = d && d.detail !== undefined ? d.detail : d;
  if (x === undefined || x === null
      || (typeof x === "object" && !Array.isArray(x)
          && Object.keys(x).length === 0)) {
    return fallback || "Something went wrong.";
  }
  if (typeof x === "string") return x;
  if (Array.isArray(x)) {
    const parts = x.map(e => {
      if (typeof e === "string") return e;
      const where = Array.isArray(e.loc) ? e.loc.filter(
        p => p !== "body").join(".") : "";
      return (where ? where + ": " : "") + (e.msg || JSON.stringify(e));
    }).filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (x && typeof x === "object") {
    return x.message || x.msg || x.error || JSON.stringify(x);
  }
  return fallback || "Something went wrong.";
}

function toast(message, kind) {
  // Panels each had their own status line, so an action taken in one place
  // and finishing elsewhere reported into a box nobody was looking at.
  const host = $("#toasts");
  if (!host) return;
  const t = el("div", "toast" + (kind ? " " + kind : ""));
  t.textContent = message;
  t.addEventListener("click", () => t.remove());
  host.appendChild(t);
  setTimeout(() => {
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 200);
  }, kind === "bad" ? 7000 : 4000);
  while (host.children.length > 4) host.removeChild(host.firstChild);
  return t;
}

/* --------------------------- command palette ---------------------------- */
// What each panel is FOR, so searching "spend" or "cv" finds it without
// knowing what it's called. Panels themselves come from the sidebar, so this
// can't drift out of date when one is added.
const PALETTE_HINTS = {
  jobsBtn: "search jobs, auto-apply, tailor a cv, resume, interview prep, track applications",
  settingsBtn: "engine, model, spending cap, budget, cost, turbo, themes, dark mode, voice",
  enginesBtn: "add or switch engine, api keys, local models",
  memoryBtn: "what it remembers about you, skills, facts",
  documentsBtn: "upload pdfs, word, excel, search your own files, rag",
  schedBtn: "scheduled jobs, watchers, recurring work",
  crewBtn: "delivery, bizdev, ops, intel specialists, chains, tenders, leads, proposals",
  trendsBtn: "what's new in ai agents, adopt skills",
  challengesBtn: "south african problems, tenders, opportunities worth solving",
  healthBtn: "what is broken right now and how to fix it",
  auditBtn: "everything it did, verify or clear the trail",
  undoBtn: "restore a file it changed",
  backupBtn: "back up and restore everything it has learned",
  selfBtn: "let it improve its own code, history, rollback",
  capsBtn: "which features have actually been used here",
  issuesBtn: "report a problem, recent errors",
  mcpBtn: "connect external tool servers",
  permsBtn: "what it is allowed to touch",
  skillsBtn: "run a skill you have taught it",
  tourBtn: "what every part of this app is for",
  presenterBtn: "run a narrated demo of the app",
  phoneBtn: "install it on your phone",
  outreachBtn: "email, campaigns, sending, smtp, mail",
  autonomyBtn: "how much it may do unattended",
};

let _palItems = [], _palAt = 0;

function paletteItems() {
  const out = [];
  document.querySelectorAll(".foot-btn").forEach(b => {
    const name = (b.textContent || "").trim();
    if (!name) return;
    out.push({ name, group: "Open", hint: PALETTE_HINTS[b.id] || "",
               run: () => b.click() });
  });
  out.push(
    { name: "New conversation", group: "Do", hint: "start a fresh chat",
      run: () => newConversation() },
    { name: "Search my job sites", group: "Do",
      hint: "find roles across your boards",
      run: () => { $("#jobsBtn").click(); setTimeout(() => jobsTab("find"), 0); } },
    { name: "Held drafts", group: "Do",
      hint: "claims blocking your applications",
      run: () => { $("#jobsBtn").click(); setTimeout(() => jobsTab("claims"), 0); } },
    { name: "Run auto-apply", group: "Do", hint: "score, draft and send",
      run: () => { $("#jobsBtn").click(); setTimeout(() => jobsTab("auto"), 0); } },
    { name: "Verify the audit chain", group: "Do",
      hint: "prove the log wasn't altered",
      run: () => { $("#auditBtn").click(); setTimeout(() => {
        const b = $("#auditVerifyBtn"); if (b) b.click(); }, 300); } },
    { name: "Back up now", group: "Do", hint: "save everything it has learned",
      run: () => $("#backupBtn").click() });
  return out;
}

function openPalette() {
  _palItems = paletteItems();
  _palAt = 0;
  const bd = $("#paletteBackdrop");
  bd.hidden = false;
  const input = $("#paletteInput");
  input.value = "";
  renderPalette("");
  setTimeout(() => input.focus(), 0);
}

function closePalette() {
  const bd = $("#paletteBackdrop");
  if (bd) bd.hidden = true;
}

function paletteMatches(q) {
  const s = (q || "").trim().toLowerCase();
  if (!s) return _palItems;
  const words = s.split(/\s+/);
  return _palItems
    .map(it => {
      const hay = (it.name + " " + it.hint).toLowerCase();
      let score = 0;
      for (const w of words) {
        const at = hay.indexOf(w);
        if (at < 0) return null;              // every word must appear
        // a hit in the name beats a hit in the description
        score += it.name.toLowerCase().includes(w) ? 10 : 3;
        if (at === 0) score += 4;
      }
      return { it, score };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score)
    .map(x => x.it);
}

function renderPalette(q) {
  const list = $("#paletteList");
  list.innerHTML = "";
  const hits = paletteMatches(q);
  if (!hits.length) {
    const e = el("div", "palette-empty");
    e.textContent = "Nothing matches that.";
    list.appendChild(e);
    return;
  }
  _palAt = Math.min(_palAt, hits.length - 1);
  let group = "";
  hits.forEach((it, i) => {
    if (it.group !== group) {
      group = it.group;
      const g = el("div", "palette-row-group");
      g.textContent = group;
      list.appendChild(g);
    }
    const row = el("button", "palette-row" + (i === _palAt ? " is-on" : ""));
    row.type = "button";
    const n = el("span", "palette-row-name"); n.textContent = it.name;
    const hint = el("span", "palette-row-hint"); hint.textContent = it.hint;
    row.append(n, hint);
    row.addEventListener("click", () => { closePalette(); it.run(); });
    list.appendChild(row);
  });
  _palHits = hits;
}

let _palHits = [];

async function modelFit() {
  const host = $("#modelFits");
  if (!host) return;
  const vram = parseFloat(($("#modelVram") || {}).value) || 24;
  host.innerHTML = "";
  try {
    const d = await fetch(`/api/models/catalogue?vram=${vram}`)
      .then(r => r.json());
    (d.recommended || []).forEach(m => {
      const row = el("div", "dash-edit-row");
      const lab = el("label");
      lab.textContent = `${m.model} — ${m.needs_gb} GB — ${m.fit}`
        + ` · ${m.licence} · ${m.good_at.join(", ")}`;
      const use = el("button", "btn ghost btn-mini");
      use.textContent = "Build on this";
      use.addEventListener("click", () => {
        $("#modelBase").value = m.model;
        toast(`${m.model} — ${m.why}`, m.fit === "tight" ? "warn" : "");
      });
      row.append(lab, use);
      host.appendChild(row);
    });
    if ((d.derived || []).length) {
      const h = el("div", "log-meta");
      h.style.marginTop = "8px";
      h.textContent = "Yours: " + d.derived
        .map(x => `${x.name} (from ${x.base})`).join(", ");
      host.appendChild(h);
    }
  } catch (e) {
    const p = el("div", "log-meta");
    p.textContent = "Could not work that out.";
    host.appendChild(p);
  }
}

/* -------------------------------- code map ------------------------------ */
function openMl() {
  const m = $("#mlModal");
  m.hidden = false; m.style.display = "flex";
  loadMlSaved();
  fetch("/api/models/hardware").then(r => r.json()).then(hw => {
    const el2 = $("#mlHardware");
    if (!el2) return;
    // the honest line: the card is not always the answer
    el2.textContent = hw.cuda
      ? `${hw.name} · ${hw.vram_gb} GB — used for text and images`
      : "no CUDA — tables train on the CPU anyway";
  }).catch(() => {});
}

function closeMl() {
  const m = $("#mlModal");
  m.hidden = true; m.style.display = "none";
}

function mlTab(which) {
  const map = { data: "Data", build: "Build", engineer: "Engineer",
                features: "Features", test: "Test", predict: "Predict",
                saved: "Saved" };
  Object.keys(map).forEach(k => {
    const b = $("#mlTab" + map[k]), p = $("#mlPane" + map[k]);
    if (b) b.classList.toggle("is-on", k === which);
    if (p) p.hidden = k !== which;
  });
  if (which === "saved") loadMlSaved();
  if (which === "test") loadMlSaved();
}

async function mlLook() {
  const path = ($("#mlPath") || {}).value || "";
  const target = ($("#mlTarget") || {}).value || "";
  if (!path) { toast("Point it at a file or folder first.", "warn"); return; }
  const head = $("#mlDataHead"), issues = $("#mlIssues"), plan = $("#mlPlan");
  head.innerHTML = ""; issues.innerHTML = ""; plan.innerHTML = "";
  mlTab("data");
  try {
    const d = await fetch("/api/models/diagnose", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }) }).then(r => r.json());
    const line = el("div", "auto-line");
    line.textContent = d.detail || `${(d.rows || 0).toLocaleString()} rows, `
      + `${d.columns} columns — ${d.summary || ""}`;
    head.appendChild(line);
    const n = el("div", "log-meta");
    n.textContent = d.note || "";
    head.appendChild(n);
    (d.issues || []).forEach(i => {
      const row = el("div", "auto-row" + (i.fix === "flag_only" ? " warn" : ""));
      const t = el("div", "auto-row-title"); t.textContent = i.what;
      const w = el("div", "log-meta"); w.textContent = i.risk;
      row.append(t, w);
      issues.appendChild(row);
    });
    if (!(d.issues || []).length) {
      emptyPane(issues, "Nothing obviously wrong", "Worth building on.");
    }
  } catch (e) { toast("Could not read that.", "bad"); }

  try {
    const p = await fetch("/api/models/plan", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, target }) }).then(r => r.json());
    plan.innerHTML = "";
    const h = el("div", "detail-head");
    h.textContent = `${p.kind} — ${p.approach}`;
    const s = el("div", "detail-sub"); s.textContent = p.why || "";
    plan.append(h, s);
    const blk = (title, body) => {
      if (!body) return;
      const b = el("div", "detail-block");
      const bh = el("h4"); bh.textContent = title;
      const pre = el("pre"); pre.textContent = body;
      b.append(bh, pre); plan.appendChild(b);
    };
    blk("Why this approach", p.why_this);
    blk(p.use_gpu ? "Where the card helps" : "Why the card wouldn't help",
        p.why_gpu || p.why_not_gpu);
  } catch (e) { /* the plan is advisory */ }
}

async function mlTrain() {
  const path = ($("#mlPath") || {}).value || "";
  const target = ($("#mlTarget") || {}).value || "";
  const btn = $("#mlTrainBtn");
  if (!path) { toast("Point it at a file or folder first.", "warn"); return; }
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "Building…";
  mlTab("build");
  $("#mlVerdict").innerHTML = "";
  $("#mlSteps").innerHTML = "";
  $("#mlDetail").innerHTML = "";
  const waiting = el("div", "auto-line");
  waiting.textContent = "Training and measuring against a do-nothing "
    + "baseline — this takes a moment.";
  $("#mlVerdict").appendChild(waiting);
  try {
    const r = await fetch("/api/models/train", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, target,
                             name: ($("#mlTarget") || {}).value || "" }) });
    const d = await r.json();
    if (!r.ok) {
      $("#mlVerdict").innerHTML = "";
      const bad = el("div", "auto-line");
      bad.textContent = errText(d, "Could not build that.");
      $("#mlVerdict").appendChild(bad);
      btn.textContent = was; btn.disabled = false;
      return;
    }
    renderMlResult(d);
    loadMlSaved();
  } catch (e) { toast("Could not build that.", "bad"); }
  btn.textContent = was; btn.disabled = false;
}

function renderMlResult(d) {
  const head = $("#mlVerdict"), steps = $("#mlSteps"), detail = $("#mlDetail");
  head.innerHTML = ""; steps.innerHTML = ""; detail.innerHTML = "";
  const line = el("div", "auto-line" + (d.beats_baseline ? "" : " is-off"));
  line.textContent = d.verdict || "";
  head.appendChild(line);
  // the number next to what guessing scores — on its own it means nothing
  const chips = el("div", "auto-chips");
  const chip = (label, val, tone) => {
    const c = el("span", "auto-chip" + (tone ? " " + tone : ""));
    const b = el("b"); b.textContent = val;
    const s = el("span"); s.textContent = " " + label;
    c.append(b, s); chips.appendChild(c);
  };
  chip(d.score_name || "score", d.test_score);
  chip("guessing scores", d.baseline_test_score, "warn");
  if (d.choice_was_close) chip("models tied", "≈", "warn");
  if (d.rows) chip("rows tested on", d.rows.test);
  head.appendChild(chips);

  (d.steps || []).forEach(s => {
    const row = el("div", "auto-row");
    const t = el("div", "auto-row-title"); t.textContent = s;
    row.appendChild(t);
    steps.appendChild(row);
  });
  (d.honest || []).forEach(s => {
    const row = el("div", "log-meta");
    row.style.margin = "6px 0";
    row.textContent = s;
    steps.appendChild(row);
  });

  const blk = (title, body) => {
    if (!body) return;
    const b = el("div", "detail-block");
    const h = el("h4"); h.textContent = title;
    const pre = el("pre"); pre.textContent = body;
    b.append(h, pre); detail.appendChild(b);
  };
  if ((d.validation_scores || []).length) {
    // the spread is the point: without it a 0.002 gap looks like a decision
    blk("What it tried", d.validation_scores.map(
      r => r.reading ? `${r.model}: ${r.reading}`
                     : `${r.model}: ${r.validation ?? r.error}`).join("\n")
        + (d.choice_note ? `\n\n${d.choice_note}` : ""));
  }
  if (d.features) {
    blk("Columns used",
        `numeric: ${(d.features.numeric || []).join(", ") || "none"}\n`
        + `categorical: ${(d.features.categorical || []).join(", ") || "none"}`
        + (d.features.dropped_as_ids.length
           ? `\ndropped as ids: ${d.features.dropped_as_ids.join(", ")}` : "")
        + (d.features.dropped_by_you.length
           ? `\ndropped as leaks: ${d.features.dropped_by_you.join(", ")}`
           : ""));
  }
  if (d.leaks && (d.leaks.suspects || []).length) {
    blk("Leakage found", d.leaks.suspects.map(s => s.why).join("\n\n"));
  }
}

async function loadMlSaved() {
  const host = $("#mlSaved"), sel = $("#mlPredictModel");
  try {
    const d = await fetch("/api/models/saved").then(r => r.json());
    if (host) {
      host.innerHTML = "";
      if (!(d.models || []).length) {
        emptyPane(host, "No models yet",
                  "Point it at a spreadsheet and press Build a model.");
      }
      (d.models || []).forEach(m => {
        const row = el("div", "auto-row" + (m.beats_baseline ? " good" : " warn"));
        const t = el("div", "auto-row-title");
        t.textContent = `${m.name} — predicts ${m.target}`;
        const w = el("div", "log-meta");
        w.textContent = m.beats_baseline
          ? `${m.test_score} against ${m.baseline} for guessing · ${m.chose}`
          : `${m.test_score} against ${m.baseline} for guessing — does NOT `
            + `beat doing nothing`;
        row.append(t, w);
        host.appendChild(row);
      });
    }
    const tsel = $("#mlTestModel");
    if (tsel) {
      tsel.innerHTML = "";
      (d.models || []).forEach(m => {
        const o = el("option");
        o.value = m.name; o.textContent = m.name;
        tsel.appendChild(o);
      });
    }
    if (sel) {
      sel.innerHTML = "";
      (d.models || []).forEach(m => {
        const o = el("option");
        o.value = m.name; o.textContent = m.name;
        sel.appendChild(o);
      });
    }
  } catch (e) { /* panel may not be open */ }
}

async function mlColumns() {
  const path = ($("#mlPath") || {}).value || "";
  const host = $("#mlColumns"), detail = $("#mlColDetail");
  if (!path) { toast("Point it at a file first.", "warn"); return; }
  host.innerHTML = ""; detail.innerHTML = "";
  try {
    const d = await fetch("/api/data/columns", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }) }).then(r => r.json());
    $("#mlEngStatus").textContent = `${d.rows.toLocaleString()} rows`;
    (d.columns || []).forEach(c => {
      const row = el("button", "job-row");
      row.type = "button";
      row.appendChild(monogram(c.name));
      const body = el("div", "job-row-body");
      const t = el("div", "job-row-title");
      const n = el("span"); n.textContent = c.name;
      t.appendChild(n);
      const m = el("div", "job-row-meta");
      m.textContent = `${c.kind} · ${c.distinct} distinct`
        + (c.missing ? ` · ${c.missing_pct}% missing` : "");
      body.append(t, m);
      row.appendChild(body);
      row.addEventListener("click", () => showColumn(path, c));
      host.appendChild(row);
    });
  } catch (e) { toast("Could not read that.", "bad"); }
}

function showColumn(path, c) {
  const detail = $("#mlColDetail");
  detail.innerHTML = "";
  const h = el("div", "detail-head"); h.textContent = c.name;
  const s = el("div", "detail-sub");
  s.textContent = `${c.kind} · ${c.distinct} distinct · example: ${c.example}`;
  detail.append(h, s);
  if (c.stats && Object.keys(c.stats).length) {
    const b = el("div", "detail-block");
    const bh = el("h4"); bh.textContent = "What's in it";
    const pre = el("pre");
    pre.textContent = Object.entries(c.stats)
      .map(([k, v]) => `${k}: ${typeof v === "object"
        ? JSON.stringify(v) : v}`).join("\n");
    b.append(bh, pre); detail.appendChild(b);
  }
  const acts = el("div", "mcp-actions");
  acts.style.flexWrap = "wrap";
  (c.can || []).forEach(a => {
    const btn = el("button", "btn ghost btn-mini");
    btn.textContent = a.replace(/_/g, " ");
    btn.addEventListener("click", async () => {
      const payload = { action: a, column: c.name };
      if (a === "rename") {
        const to = prompt(`Rename '${c.name}' to:`, c.name);
        if (!to) return;
        payload.to = to;
      }
      if (a === "fill_value") payload.value = "(missing)";
      btn.disabled = true;
      try {
        const r = await fetch("/api/data/actions", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, actions: [payload] }) });
        const d = await r.json();
        if (!r.ok) { toast(errText(d, "Could not do that."), "bad"); }
        else {
          const eff = (d.applied[0] || {}).effect || "done";
          toast(`${c.name}: ${eff}`, "ok");
          // work continues on the edited copy, so actions compose
          $("#mlPath").value = d.output;
          $("#mlEngStatus").textContent = `now working on ${d.output}`;
          mlColumns();
        }
      } catch (e) { toast("Could not do that.", "bad"); }
      btn.disabled = false;
    });
    acts.appendChild(btn);
  });
  detail.appendChild(acts);
}

async function mlFeatures() {
  const path = ($("#mlPath") || {}).value || "";
  const target = ($("#mlTarget") || {}).value || "";
  const host = $("#mlFeatures");
  if (!path) { toast("Point it at a file first.", "warn"); return; }
  host.innerHTML = "";
  try {
    const d = await fetch("/api/models/features", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }) }).then(r => r.json());
    $("#mlFeatStatus").textContent = target
      ? "click one to build it and measure whether it helps"
      : "name a target column to measure them";
    (d.ideas || []).forEach(idea => {
      const row = el("div", "auto-row");
      const t = el("div", "auto-row-title");
      t.textContent = `${idea.kind} from ${idea.from}`;
      const w = el("div", "log-meta"); w.textContent = idea.why;
      row.append(t, w);
      const b = el("button", "btn ghost btn-mini");
      b.style.marginTop = "6px";
      b.textContent = target ? "Build and measure" : "Name a target first";
      b.disabled = !target;
      b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "Measuring…";
        try {
          const r = await fetch("/api/data/feature", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path, target, idea }) });
          const res = await r.json();
          if (!r.ok) { toast(errText(res, "Could not."), "bad"); }
          else {
            const box = $("#mlFeatDetail");
            box.innerHTML = "";
            const hh = el("div", "detail-head");
            hh.textContent = res.helped ? "Worth keeping" : "No real gain";
            const ss = el("div", "detail-sub"); ss.textContent = res.verdict;
            box.append(hh, ss);
            const pre = el("pre");
            pre.textContent = `built: ${res.made.join(", ")}\n`
              + `written to: ${res.output}`;
            box.appendChild(pre);
            toast(res.verdict, res.helped ? "ok" : "warn");
          }
        } catch (e) { toast("Could not measure that.", "bad"); }
        b.disabled = false; b.textContent = "Build and measure";
      });
      row.appendChild(b);
      host.appendChild(row);
    });
    if (!(d.ideas || []).length) {
      emptyPane(host, "Nothing obvious to add",
                "No dates, high-cardinality categories or related amounts.");
    }
  } catch (e) { toast("Could not read that.", "bad"); }
}

async function mlScoreHidden() {
  const name = ($("#mlTestModel") || {}).value || "";
  const path = ($("#mlTestPath") || {}).value || "";
  const head = $("#mlTestHead"), mets = $("#mlTestMetrics");
  const detail = $("#mlTestDetail");
  if (!name || !path) { toast("Pick a model and a file.", "warn"); return; }
  head.innerHTML = ""; mets.innerHTML = ""; detail.innerHTML = "";
  try {
    const r = await fetch("/api/models/evaluate", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path }) });
    const d = await r.json();
    if (!r.ok) {
      emptyPane(mets, "Couldn't score it", errText(d, "Try again."));
      return;
    }
    const line = el("div", "auto-line");
    line.textContent = `${d.headline.name}: ${d.headline.value} on `
      + `${d.rows.toLocaleString()} rows it has never seen.`;
    head.appendChild(line);
    const why = el("div", "log-meta");
    why.textContent = d.headline.why;
    head.appendChild(why);
    const cmp = el("div", "log-meta");
    cmp.style.marginTop = "4px";
    cmp.textContent = d.compared_to_training.reading;
    head.appendChild(cmp);

    // per-class recall is what accuracy hides
    const pc = (d.metrics || {}).per_class || {};
    Object.entries(pc).forEach(([k, v]) => {
      if (["accuracy", "macro avg", "weighted avg"].includes(k)) return;
      const row = el("div", "auto-row"
        + (v.recall < 0.5 ? " warn" : v.recall > 0.8 ? " good" : ""));
      const t = el("div", "auto-row-title");
      t.textContent = `${k}: catches ${(v.recall * 100).toFixed(0)}%`;
      const w = el("div", "log-meta");
      w.textContent = `right ${(v.precision * 100).toFixed(0)}% of the times `
        + `it says so · ${v.rows} rows`;
      row.append(t, w);
      mets.appendChild(row);
    });

    const blk = (title, body) => {
      if (!body) return;
      const b = el("div", "detail-block");
      const h2 = el("h4"); h2.textContent = title;
      const pre = el("pre"); pre.textContent = body;
      b.append(h2, pre); detail.appendChild(b);
    };
    if (d.metrics.roc_auc !== undefined) {
      blk("Ranking quality", `ROC-AUC ${d.metrics.roc_auc}\n`
          + (d.metrics.auc_note || ""));
    }
    if (d.metrics.confusion) {
      blk("What it got right and wrong",
          (d.metrics.labels || []).join("  ") + "\n"
          + d.metrics.confusion.map(r => r.join("  ")).join("\n"));
    }
    blk("Why this test matters", d.note);

    // the threshold dial
    const tr = await fetch("/api/models/threshold", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path }) }).then(x => x.json());
    if (tr.ok) {
      const b = el("div", "detail-block");
      const h2 = el("h4"); h2.textContent = "Moving the cut-off";
      const pre = el("pre");
      pre.textContent = tr.curve
        .filter((_, i) => i % 3 === 0)
        .map(r => `${r.threshold.toFixed(2)}  catches `
          + `${(r.caught * 100).toFixed(0)}%  right `
          + `${(r.right_when_flagged * 100).toFixed(0)}%`).join("\n")
        + "\n\n" + tr.note;
      b.append(h2, pre);
      detail.appendChild(b);
    }
  } catch (e) { toast("Could not score that.", "bad"); }
}

async function mlPredict() {
  const name = ($("#mlPredictModel") || {}).value || "";
  const raw = ($("#mlRow") || {}).value.trim();
  const host = $("#mlPredictions");
  host.innerHTML = "";
  if (!name) { toast("Build a model first.", "warn"); return; }
  let body = { name };
  if (raw.startsWith("{")) {
    try { body.row = JSON.parse(raw); }
    catch (e) { toast("That isn't valid JSON.", "warn"); return; }
  } else if (raw) {
    body.path = raw;
  } else {
    toast("Enter a row or a file path.", "warn");
    return;
  }
  try {
    const r = await fetch("/api/models/predict", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) {
      emptyPane(host, "Couldn't predict", errText(d, "Try again."));
      return;
    }
    (d.predictions || []).forEach(p => {
      const row = el("div", "auto-row");
      const t = el("div", "auto-row-title");
      t.textContent = String(p.prediction);
      row.appendChild(t);
      if (p.confidence !== undefined) {
        const w = el("div", "log-meta");
        w.textContent = `${p.confidence}% — ${p.reading}`;
        row.appendChild(w);
      }
      if (p.actual !== undefined) {
        const a = el("div", "log-meta");
        a.textContent = `actual: ${p.actual}${p.right ? " ✓" : " ✗"}`;
        row.appendChild(a);
      }
      host.appendChild(row);
    });
    if (d.checked) {
      const n = el("div", "log-meta");
      n.style.marginTop = "8px";
      n.textContent = `${d.checked.right} of ${d.checked.of} right `
        + `(${d.checked.rate}%). ${d.checked.note}`;
      host.appendChild(n);
    }
  } catch (e) { toast("Could not predict.", "bad"); }
}

function openCodemap() {
  // the app opens modals by clearing `hidden` AND setting display — the
  // global [hidden] rule is !important, so one without the other does nothing
  const m = $("#codemapModal");
  m.hidden = false; m.style.display = "flex";
  if (!$("#cmFolder").value) $("#cmFolder").value = ".";
}

function closeCodemap() {
  const m = $("#codemapModal");
  m.hidden = true; m.style.display = "none";
}

function cmTab(which) {
  const map = { findings: "Findings", diagram: "Diagram" };
  Object.keys(map).forEach(k => {
    const b = $("#cmTab" + map[k]), p = $("#cmPane" + map[k]);
    if (b) b.classList.toggle("is-on", k === which);
    if (p) p.hidden = k !== which;
  });
  if (which === "diagram") drawCodemap();
}

let _cmFocus = "";
let _cmSvg = null;

async function drawCodemap() {
  const host = $("#cmDiagram");
  const folder = ($("#cmFolder") || {}).value || ".";
  host.textContent = "";
  $("#cmDiagramNote").textContent = "Drawing\u2026";
  try {
    const d = await fetch("/api/codemap/diagram", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, module: _cmFocus }) }).then(r => r.json());
    $("#cmDiagramNote").textContent =
      `${d.showing}. ${d.note}`
      + (_cmFocus ? "  Click the background to see the whole map." : "");
    const svg = diagramSvg(d, folder);
    host.appendChild(svg);
    _cmSvg = svg;
  } catch (e) {
    $("#cmDiagramNote").textContent = "Could not draw that.";
  }
}

function diagramSvg(d, folder) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `-20 -20 ${d.width} ${d.height}`);
  svg.setAttribute("class", "codemap-svg");
  const mk = (tag, attrs) => {
    const el2 = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => el2.setAttribute(k, v));
    return el2;
  };
  // arrowhead
  const defs = mk("defs", {});
  const marker = mk("marker", { id: "cmArrow", viewBox: "0 0 8 8",
    refX: "7", refY: "4", markerWidth: "7", markerHeight: "7",
    orient: "auto-start-reverse" });
  marker.appendChild(mk("path", { d: "M0 0 L8 4 L0 8 z", class: "cm-arrow" }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  const at = {};
  d.nodes.forEach(n => { at[n.id] = n; });

  // edges first, so boxes sit on top of their lines
  d.edges.forEach(e => {
    const a = at[e.from], b = at[e.to];
    if (!a || !b) return;
    // left-to-right, so the curve bends horizontally
    const x1 = a.x + a.w, y1 = a.y + a.h / 2;
    const x2 = b.x, y2 = b.y + b.h / 2;
    const mid = (x1 + x2) / 2;
    svg.appendChild(mk("path", {
      d: `M${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
      class: "cm-edge", "marker-end": "url(#cmArrow)" }));
  });

  d.nodes.forEach(n => {
    const g = mk("g", { class: "cm-node" + (n.focus ? " is-focus" : ""),
                        transform: `translate(${n.x},${n.y})` });
    g.appendChild(mk("rect", { width: n.w, height: n.h, rx: 8,
                               class: "cm-box" }));
    const t = mk("text", { x: 11, y: 19, class: "cm-label" });
    t.textContent = n.label;
    const s = mk("text", { x: 11, y: 34, class: "cm-sub" });
    s.textContent = `${n.lines.toLocaleString()} lines · used by ${n.used_by}`;
    g.append(t, s);
    g.addEventListener("click", () => {
      // clicking a box asks the question you actually had about it
      _cmFocus = (_cmFocus === n.full) ? "" : n.full;
      drawCodemap();
      showImpact(folder, n.full);
    });
    const title = mk("title", {});
    title.textContent = `${n.full}\nuses ${n.uses}, used by ${n.used_by}`;
    g.appendChild(title);
    svg.appendChild(g);
  });
  svg.addEventListener("click", (e) => {
    if (e.target === svg && _cmFocus) { _cmFocus = ""; drawCodemap(); }
  });

  // A fixed viewBox in a panel is always too small to read. Zoom and pan by
  // moving the viewBox rather than scaling the element: text stays crisp at
  // any magnification, which a CSS transform would not give you.
  const view = { x: -20, y: -20, w: d.width, h: d.height };
  const apply = () => svg.setAttribute("viewBox",
    `${view.x} ${view.y} ${view.w} ${view.h}`);
  const home = { ...view };
  svg._zoom = (factor, cx, cy) => {
    const nw = Math.max(180, Math.min(d.width * 6, view.w * factor));
    const scale = nw / view.w;
    // keep the point under the cursor still, which is what makes wheel
    // zoom feel like zooming rather than jumping
    view.x = cx - (cx - view.x) * scale;
    view.y = cy - (cy - view.y) * scale;
    view.w = nw;
    view.h *= scale;
    apply();
  };
  svg._reset = () => { Object.assign(view, home); apply(); };
  svg._fitWidth = () => {
    Object.assign(view, home);
    apply();
  };

  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect ? svg.getBoundingClientRect()
      : { left: 0, top: 0, width: 1, height: 1 };
    const cx = view.x + ((e.clientX - r.left) / r.width) * view.w;
    const cy = view.y + ((e.clientY - r.top) / r.height) * view.h;
    svg._zoom(e.deltaY > 0 ? 1.15 : 0.87, cx, cy);
  }, { passive: false });

  let dragging = null;
  svg.addEventListener("pointerdown", (e) => {
    dragging = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
    svg.classList.add("is-dragging");
  });
  svg.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const r = svg.getBoundingClientRect
      ? svg.getBoundingClientRect() : { width: 1, height: 1 };
    view.x = dragging.vx - (e.clientX - dragging.x) * (view.w / r.width);
    view.y = dragging.vy - (e.clientY - dragging.y) * (view.h / r.height);
    apply();
  });
  const stop = () => { dragging = null; svg.classList.remove("is-dragging"); };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointerleave", stop);

  apply();
  return svg;
}

async function runCodemap() {
  const btn = $("#cmScanBtn"), list = $("#cmList"), detail = $("#cmDetail");
  const folder = ($("#cmFolder") || {}).value || ".";
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "Reading\u2026";
  list.innerHTML = ""; detail.innerHTML = "";
  try {
    const r = await fetch("/api/codemap", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }) });
    const d = await r.json();
    if (!r.ok) { toast(errText(d, "Could not read that."), "bad"); }
    else { renderCodemap(d, folder); }
  } catch (e) { toast("Could not read that.", "bad"); }
  btn.textContent = was; btn.disabled = false;
}

function renderCodemap(d, folder) {
  const list = $("#cmList"), detail = $("#cmDetail"), sum = $("#cmSummary");
  sum.innerHTML = "";
  const line = el("div", "auto-line");
  line.textContent = d.summary;
  sum.appendChild(line);

  const group = (title, rows, render) => {
    if (!rows || !rows.length) return;
    const h = el("div", "palette-row-group");
    h.textContent = `${title} (${rows.length})`;
    list.appendChild(h);
    rows.forEach(x => list.appendChild(render(x)));
  };

  // what everything leans on — where a mistake is expensive
  group("Everything leans on these", d.hotspots || [], (x) => {
    const row = el("button", "job-row has-score");
    row.type = "button";
    row.appendChild(monogram(x.module.split(".").pop()));
    const body = el("div", "job-row-body");
    const t = el("div", "job-row-title");
    const n = el("span"); n.textContent = x.module;
    t.appendChild(n);
    const m = el("div", "job-row-meta");
    m.textContent = `${x.lines.toLocaleString()} lines`;
    body.append(t, m);
    row.appendChild(body);
    const sc = el("div", "job-score" + (x.imported_by >= 10 ? " good" : ""));
    const b = el("b"); b.textContent = x.imported_by;
    const i = el("i"); i.textContent = "used by";
    sc.append(b, i);
    row.appendChild(sc);
    row.addEventListener("click", () => showImpact(folder, x.module));
    return row;
  });

  // cycles are not a style complaint
  group("Import cycles", (d.cycles || []).map(c => ({ c })), (x) => {
    const row = el("div", "auto-row warn");
    const t = el("div", "auto-row-title");
    t.textContent = x.c.join("  \u2194  ");
    const w = el("div", "log-meta");
    w.textContent = "These can't be understood, tested or moved separately.";
    row.append(t, w);
    return row;
  });

  const orp = (d.orphans || {});
  group("Nothing imports these", (orp.unreferenced || []).map(m => ({ m })),
        (x) => {
          const row = el("div", "auto-row");
          const t = el("div", "auto-row-title"); t.textContent = x.m;
          row.appendChild(t);
          return row;
        });
  if ((orp.unreferenced || []).length) {
    const note = el("div", "log-meta");
    note.style.margin = "4px 0 8px";
    note.textContent = orp.note;
    list.appendChild(note);
  }

  detail.innerHTML = "";
  const h = el("div", "detail-head");
  h.textContent = "Pick a module";
  const p = el("div", "detail-sub");
  p.textContent = "to see what would feel a change to it.";
  detail.append(h, p);
  const ext = el("div", "detail-block");
  const eh = el("h4"); eh.textContent = "Outside dependencies";
  const pre = el("pre");
  pre.textContent = Object.entries(d.external || {})
    .map(([k, v]) => `${k} (${v})`).join(", ") || "none";
  ext.append(eh, pre);
  detail.appendChild(ext);
  if ((d.problems || []).length) {
    const pb = el("div", "detail-block");
    const ph = el("h4"); ph.textContent = "Wouldn't parse";
    const pp = el("pre");
    pp.textContent = d.problems.map(x => `${x.module}: ${x.problem}`).join("\n");
    pb.append(ph, pp);
    detail.appendChild(pb);
  }
}

async function showImpact(folder, module) {
  const detail = $("#cmDetail");
  try {
    const d = await fetch("/api/codemap/impact", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, module }) }).then(r => r.json());
    detail.innerHTML = "";
    const h = el("div", "detail-head"); h.textContent = module;
    const s = el("div", "detail-sub"); s.textContent = d.verdict;
    detail.append(h, s);
    const blk = (title, items) => {
      if (!items || !items.length) return;
      const b = el("div", "detail-block");
      const bh = el("h4"); bh.textContent = `${title} (${items.length})`;
      const pre = el("pre"); pre.textContent = items.join("\n");
      b.append(bh, pre); detail.appendChild(b);
    };
    blk("Imports it directly", d.direct);
    blk("Would feel a change", d.all_affected);
  } catch (e) { toast("Could not work that out.", "bad"); }
}

/* --------------------------------- phone -------------------------------- */
let _phoneUrl = "";
function openPhone() {
  const m = $("#phoneModal");
  m.hidden = false; m.style.display = "flex";
  loadPhone();
}
function closePhone() {
  const m = $("#phoneModal");
  m.hidden = true; m.style.display = "none";
}
async function loadPhone() {
  const list = $("#phoneSteps");
  list.innerHTML = "<div class='out-empty'>Checking\u2026</div>";
  try {
    const d = await fetch("/api/phone").then(r => r.json());
    _phoneUrl = d.url || "";
    $("#phoneUrl").textContent = d.url || "";
    list.innerHTML = "";
    if (!d.reachable_on_lan && d.why) {
      const bad = el("div", "log-row");
      bad.classList.add("tier", "tier-bronze");
      const t = el("div", "log-meta"); t.textContent = d.why;
      bad.appendChild(t);
      list.appendChild(bad);
    }
    (d.steps || []).forEach((s, i) => {
      const row = el("div", "log-row");
      const n = el("span", "log-meta");
      n.textContent = (i + 1) + ".";
      n.style.minWidth = "18px";
      const t = el("span");
      t.textContent = s;
      row.append(n, t);
      list.appendChild(row);
    });
    $("#phoneWarn").textContent = (d.caution || "") + " " + (d.installed_hint || "");
  } catch (e) {
    list.innerHTML = "<div class='out-empty'>Could not work out this machine's address.</div>";
  }
}

/* ------------------------------- presenter ------------------------------ */
const _pres = { scenes: [], i: 0, timer: null, playing: false, notes: false,
                keys: null };

async function startPresenter() {
  try {
    const d = await fetch("/api/presenter").then(r => r.json());
    _pres.scenes = d.scenes || [];
  } catch (e) { return; }
  if (!_pres.scenes.length) return;
  _pres.i = 0;
  $("#presenter").hidden = false;
  document.body.classList.add("presenting");
  // keyboard is how a demo is actually driven — no hunting for buttons
  _pres.keys = (e) => {
    if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") {
      e.preventDefault(); presenterGo(1);
    } else if (e.key === "ArrowLeft" || e.key === "PageUp") {
      e.preventDefault(); presenterGo(-1);
    } else if (e.key === "Escape") { exitPresenter(); }
  };
  document.addEventListener("keydown", _pres.keys);
  showScene(0);
}

function exitPresenter() {
  stopAuto();
  if (_pres.keys) document.removeEventListener("keydown", _pres.keys);
  _pres.keys = null;
  $("#presenter").hidden = true;
  document.body.classList.remove("presenting");
  closeAllModals();          // leave the app as we found it
}

function showScene(i) {
  const s = _pres.scenes[i];
  if (!s) return;
  _pres.i = i;
  $("#presAct").textContent = s.act || "";
  $("#presTitle").textContent = s.title || "";
  $("#presSay").textContent = s.say || "";
  const note = $("#presNote");
  note.textContent = s.note ? "Speaker note: " + s.note : "";
  note.hidden = !(_pres.notes && s.note);
  $("#presCount").textContent = `${i + 1}/${_pres.scenes.length}`;
  $("#presFill").style.width =
    (((i + 1) / _pres.scenes.length) * 100).toFixed(1) + "%";
  $("#presPrev").disabled = i === 0;
  $("#presNext").textContent =
    i === _pres.scenes.length - 1 ? "Finish" : "Next →";
  // open the real panel this scene is about, so the audience sees the app
  closeAllModals();
  if (s.panel) {
    const b = $("#" + s.panel);
    if (b) b.click();
  }
  if (_pres.playing) queueAuto(s.seconds || 14);
}

function presenterGo(step) {
  const next = _pres.i + step;
  if (next < 0) return;
  if (next >= _pres.scenes.length) { exitPresenter(); return; }
  showScene(next);
}

function queueAuto(seconds) {
  stopAuto();
  _pres.timer = setTimeout(() => presenterGo(1), Math.max(4, seconds) * 1000);
}
function stopAuto() {
  if (_pres.timer) { clearTimeout(_pres.timer); _pres.timer = null; }
}

/* --------------------------------- guide -------------------------------- */
let _tourStops = [], _tourChapter = "";
function openTour() {
  const m = $("#tourModal");
  m.hidden = false; m.style.display = "flex";
  loadTour();
}
function closeTour() {
  const m = $("#tourModal");
  m.hidden = true; m.style.display = "none";
}
async function loadTour() {
  try {
    const d = await fetch("/api/tour").then(r => r.json());
    _tourStops = d.stops || [];
    const bar = $("#tourChapters");
    bar.innerHTML = "";
    ["All", ...(d.chapters || [])].forEach(ch => {
      const b = el("button", "btn ghost btn-mini");
      b.textContent = ch;
      b.addEventListener("click", () => {
        _tourChapter = ch === "All" ? "" : ch;
        renderTour();
      });
      bar.appendChild(b);
    });
    renderTour();
  } catch (e) { $("#tourList").innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
function renderTour() {
  const list = $("#tourList");
  list.innerHTML = "";
  let chapter = "";
  _tourStops
    .filter(s => !_tourChapter || s.chapter === _tourChapter)
    .forEach(s => {
      if (s.chapter !== chapter) {
        chapter = s.chapter;
        const h = el("div", "foot-group-label");
        h.textContent = chapter;
        list.appendChild(h);
      }
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier", "tier-silver");
      const t = el("div"); t.style.fontWeight = "600"; t.textContent = s.title;
      const what = el("div", "log-meta"); what.textContent = s.what;
      const why = el("div", "log-meta"); why.style.opacity = ".9";
      why.textContent = s.why;
      row.append(t, what, why);
      const act = el("div", "mcp-actions"); act.style.marginTop = "6px";
      const tryIt = el("div", "log-meta");
      tryIt.style.flex = "1"; tryIt.style.opacity = ".85";
      tryIt.textContent = "Try: " + s["try"];
      act.appendChild(tryIt);
      if (s.panel) {
        const open = el("button", "btn ghost btn-mini");
        open.textContent = "Open it";
        open.addEventListener("click", () => {
          closeTour();
          const b = $("#" + s.panel);
          if (b) b.click();
        });
        act.appendChild(open);
      }
      row.appendChild(act);
      list.appendChild(row);
    });
}

/* ------------------------------ challenges ------------------------------ */
function openChallenges() {
  const m = $("#challengesModal");
  m.hidden = false; m.style.display = "flex";
  $("#chDetail").textContent = ""; $("#chStatus").textContent = "";
  loadChallenges();
}
function closeChallenges() {
  const m = $("#challengesModal");
  m.hidden = true; m.style.display = "none";
}
function renderProposal(d) {
  const box = $("#chDetail");
  box.textContent = "";
  const add = (label, body, strong) => {
    if (!body) return;
    const h = el("div", strong ? "detail-head" : "palette-row-group");
    h.textContent = label;
    const p = el("pre");
    p.style.cssText = "white-space:pre-wrap;margin:2px 0 10px";
    p.textContent = body;
    box.append(h, p);
  };
  const v = d.verdict;
  add(d.title,
      v === "app" ? "This one is buildable — the model can already do the "
                    + "work, what's missing is scaffolding."
      : v === "model" ? "This needs the model itself to change. An app can "
                        + "detect it, not prevent it."
      : "Part of this is buildable; part needs the model to change.", true);

  const aj = d.agent_jo || {};
  if (aj.can_help) {
    add("Agent Jo could build", aj.what);
    if ((aj.how || []).length) add("How", aj.how.map(x => "• " + x).join("\n"));
    if ((aj.uses || []).length) {
      add("Using what it already has", aj.uses.map(x => "• " + x).join("\n"));
    }
    if ((aj.needs_building || []).length) {
      add("New work", aj.needs_building.map(x => "• " + x).join("\n"));
    }
    add("Effort", aj.effort);
    add("Why it might fail", aj.why_it_might_fail);
  }
  const mg = d.model_gap || {};
  if (mg.is_one) {
    add("What the model can't do", mg.what_the_model_cannot_do);
    add("Proposal", mg.proposal);
    add("Why it matters", mg.why_it_matters);
    add("How to verify a fix", mg.how_to_verify);
  }

  const acts = el("div", "mcp-actions");
  acts.style.marginTop = "8px";
  if (aj.can_help) {
    const build = el("button", "btn primary btn-mini");
    build.textContent = "Send to self-improvement";
    build.addEventListener("click", async () => {
      const r = await fetch("/api/challenges/build", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal: d }) });
      const dd = await r.json();
      if (!r.ok) { toast(derrText(d, "Could not."), "warn"); return; }
      closeAllModals();
      send("Build this into yourself: " + dd.request);
      toast(dd.note, "ok");
    });
    acts.appendChild(build);
  }
  if (mg.is_one) {
    const note = el("button", "btn ghost btn-mini");
    note.textContent = "Write it up as a feature note";
    note.addEventListener("click", async () => {
      const r = await fetch("/api/challenges/feature-note", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal: d }) });
      const dd = await r.json();
      if (!r.ok) { toast(derrText(d, "Could not."), "warn"); return; }
      box.textContent = dd.markdown;
      toast(dd.note, "ok");
    });
    acts.appendChild(note);
  }
  box.appendChild(acts);
}

async function loadChallenges() {
  const list = $("#chList");
  try {
    const d = await fetch("/api/challenges").then(r => r.json());
    const rep = d.report || {};
    const briefs = rep.briefs || [];
    $("#chInfo").textContent = (rep.at
      ? `Last scan ${rep.at} \u00b7 ${briefs.length} brief(s)`
      : "No scan yet.")
      + (d.backlog ? ` \u00b7 ${d.backlog} item(s) seen but never briefed` : "");
    const sel = $("#chEngine");
    if (sel && !sel.children.length) {
      const eng = await fetch("/api/trends").then(r => r.json()).catch(() => ({}));
      (eng.engines || ["Auto"]).forEach(n => {
        const o = el("option"); o.value = n; o.textContent = n;
        sel.appendChild(o);
      });
    }
    list.innerHTML = "";
    if (!briefs.length) {
      list.innerHTML = "<div class='out-empty'>Nothing yet. Scan to look for problems worth solving.</div>";
      return;
    }
    briefs.forEach((b, i) => {
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier",
        b.confidence === "high" ? "tier-gold"
          : b.confidence === "medium" ? "tier-silver" : "tier-bronze");
      const t = el("div"); t.style.fontWeight = "600"; t.textContent = b.title;
      const p = el("div", "log-meta"); p.textContent = b.problem;
      row.append(t, p);
      const act = el("div", "mcp-actions"); act.style.marginTop = "6px";
      const detail = el("button", "btn ghost btn-mini");
      detail.textContent = "Details";
      detail.addEventListener("click", () => {
        $("#chDetail").textContent = [
          b.title, "",
          "PROBLEM: " + b.problem,
          "WHO FEELS IT: " + b.who,
          "DATA THAT LIKELY EXISTS: " + b.data,
          "POSSIBLE FIRST ENGAGEMENT: " + b.first_engagement,
          "WHY IT MIGHT FAIL: " + b.why_it_might_fail,
          "CONFIDENCE: " + b.confidence,
          "SOURCES: " + (b.sources || []).join(", ")].join("\n");
      });
      // Two very different answers to "what would fix this", and saying
      // which is which is the useful part.
      const prop = el("button", "btn primary btn-mini");
      prop.textContent = "What would fix this?";
      prop.addEventListener("click", async () => {
        prop.disabled = true;
        const was = prop.textContent;
        prop.textContent = "Thinking\u2026";
        try {
          const eng2 = $("#chEngine");
          const r = await fetch("/api/challenges/propose", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ brief: b,
                                   engine: eng2 ? eng2.value : "" }) });
          const d = await r.json();
          if (!r.ok) { toast(errText(d, "Could not work that out."), "bad"); }
          else { renderProposal(d); }
        } catch (e) { toast("Could not work that out.", "bad"); }
        prop.textContent = was; prop.disabled = false;
      });
      act.appendChild(prop);
      const send = el("button", "btn ghost btn-mini");
      send.textContent = "Send to BizDev";
      send.addEventListener("click", async () => {
        send.disabled = true;
        $("#chStatus").textContent = "BizDev is qualifying it\u2026";
        try {
          const eng = $("#chEngine");
          const r = await fetch("/api/challenges/to-crew", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ index: i,
                                   engine: eng ? eng.value : "" })
          }).then(r => r.json());
          $("#chDetail").textContent = r.report || r.detail || "";
          $("#chStatus").textContent = "";
        } catch (e) { $("#chStatus").textContent = "Could not send it."; }
        send.disabled = false;
      });
      act.append(detail, send);
      row.appendChild(act);
      list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function scanChallenges(deep) {
  const b = deep ? $("#chDeepBtn") : $("#chScanBtn");
  const label = b.textContent;
  b.disabled = true; b.textContent = "Scanning\u2026";
  $("#chStatus").textContent = deep
    ? "Reconsidering everything seen before that never became a brief\u2026"
    : "Reading SA feeds and looking for problems\u2026";
  try {
    const sel = $("#chEngine");
    const r = await fetch(deep ? "/api/challenges/deep-scan"
                               : "/api/challenges/scan", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine: sel ? sel.value : "" }) });
    const d = await r.json();
    $("#chStatus").textContent = r.ok
      ? ((d.report || {}).no_new
          ? "Nothing left in the backlog — everything seen has been considered."
          : (d.report || {}).reconsidered
            ? `Reconsidered ${d.report.reconsidered} previously unbriefed item(s).`
            : "")
      : errText(d, "Scan failed.");
    if (r.ok) loadChallenges();
  } catch (e) { $("#chStatus").textContent = "Scan failed."; }
  b.disabled = false; b.textContent = label;
}
/* -------------------------------- skills -------------------------------- */
function openSkills() {
  const m = $("#skillsModal");
  m.hidden = false; m.style.display = "flex";
  $("#skillsStatus").textContent = "";
  loadSkills();
}
function closeSkills() {
  const m = $("#skillsModal");
  m.hidden = true; m.style.display = "none";
}
async function loadSkills() {
  const list = $("#skillsList");
  list.innerHTML = "<div class='out-empty'>Loading\u2026</div>";
  try {
    const d = await fetch("/api/skills/list").then(r => r.json());
    const skills = d.skills || [];
    $("#skillsInfo").textContent = skills.length
      ? `${skills.length} skill(s) \u00b7 ${(d.unused || []).length} never used`
      : "";
    list.innerHTML = "";
    if (!skills.length) {
      list.innerHTML = "<div class='out-empty'>No skills yet. Teach one in chat (\u201cremember how I like BI reviews done\u2026\u201d), or adopt one from the Trends panel.</div>";
      return;
    }
    skills.forEach(s => {
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier", s.times_used ? "tier-gold" : "tier-silver");
      const head = el("div"); head.style.fontWeight = "600";
      head.textContent = s.name;
      const meta = el("div", "log-meta");
      meta.textContent = [
        s.times_used ? `used ${s.times_used}\u00d7` : "never used",
        s.last_used ? `last ${s.last_used}` : null,
        s.description].filter(Boolean).join(" \u00b7 ");
      row.append(head, meta);
      if ((s.steps || []).length) {
        const steps = el("div", "log-meta");
        steps.style.opacity = ".85";
        steps.textContent = s.steps.map((t, i) => `${i + 1}. ${t}`).join("  ");
        row.appendChild(steps);
      }
      const act = el("div", "issues-toolbar"); act.style.marginTop = "6px";
      const inp = el("input"); inp.type = "text"; inp.className = "model-input";
      inp.style.flex = "1";
      inp.placeholder = "What should it work on? (optional)";
      const run = el("button", "btn primary btn-mini"); run.textContent = "Run";
      const go = async () => {
        run.disabled = true;
        $("#skillsStatus").textContent = `Running ${s.name}\u2026`;
        try {
          const r = await fetch("/api/skills/run", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: s.name, input: inp.value }) });
          const d2 = await r.json();
          if (!r.ok) { $("#skillsStatus").textContent = d2.detail || "Could not run that."; }
          else {
            // hand it to the normal chat path, so tools, permissions and the
            // audit trail behave exactly as they do for any other turn
            closeSkills();
            // send(text) is the normal chat path — tools, permissions and
            // the audit trail behave exactly as for any other turn
            send(d2.prompt);
          }
        } catch (e) { $("#skillsStatus").textContent = "Could not run that."; }
        run.disabled = false;
      };
      run.addEventListener("click", go);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      act.append(inp, run);
      row.appendChild(act);
      list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
/* ------------------------------ first run ------------------------------- */
function openSetup() {
  const m = $("#setupModal");
  m.hidden = false; m.style.display = "flex";
  loadSetup();
}
function closeSetup() {
  const m = $("#setupModal");
  m.hidden = true; m.style.display = "none";
}
async function loadSetup(openIfUnconfigured) {
  try {
    const s = await fetch("/api/setup").then(r => r.json());
    const oll = $("#setupOllama");
    if (oll) {
      oll.textContent = s.ollama
        ? "Ready — Ollama is running on this machine. Nothing leaves your computer."
        : "Free and fully private — nothing leaves your computer. Install from ollama.com, then run: ollama pull qwen3";
    }
    const st = $("#setupStatus");
    if (st) {
      st.textContent = s.configured
        ? (s.has_key ? "Anthropic key is set." : "")
          + (s.ollama ? " Ollama is available." : "")
        : "No engine yet — chat stays disabled until one is set up.";
    }
    if (openIfUnconfigured && !s.configured) openSetup();
    return s;
  } catch (e) { return null; }
}
async function saveSetupKey() {
  const b = $("#setupSaveBtn"), input = $("#setupKey");
  const key = (input.value || "").trim();
  b.disabled = true;
  $("#setupStatus").textContent = "Saving\u2026";
  try {
    const r = await fetch("/api/setup/key", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }) });
    const d = await r.json();
    if (!r.ok) { $("#setupStatus").textContent = errText(d, "Could not save that."); }
    else {
      input.value = "";
      $("#setupStatus").textContent = d.note || "Saved.";
      setTimeout(closeSetup, 1200);
      refreshStats();
    }
  } catch (e) { $("#setupStatus").textContent = "Could not save that."; }
  b.disabled = false;
}
/* ----------------------------- capabilities ----------------------------- */
function openCaps() {
  const m = $("#capsModal");
  m.hidden = false; m.style.display = "flex";
  loadCaps();
}
function closeCaps() {
  const m = $("#capsModal");
  m.hidden = true; m.style.display = "none";
}
async function loadCaps() {
  const list = $("#capsList");
  list.innerHTML = "<div class='out-empty'>Checking\u2026</div>";
  try {
    const d = await fetch("/api/capabilities").then(r => r.json());
    const c = d.counts || {};
    $("#capsInfo").textContent =
      `${c.used || 0} of ${d.total} exercised \u00b7 ${c.ready || 0} ready and untried \u00b7 ${c["needs setup"] || 0} need setup`;
    list.innerHTML = "";
    let group = "";
    (d.capabilities || []).forEach(cap => {
      if (cap.group !== group) {
        group = cap.group;
        const g = el("div", "foot-group-label");
        g.textContent = group;
        list.appendChild(g);
      }
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier",
        cap.state === "used" ? "tier-gold"
          : cap.state === "ready" ? "tier-silver" : "tier-bronze");
      const head = el("div"); head.style.fontWeight = "600";
      head.textContent = `${cap.state === "used" ? "\u2713" : cap.state === "ready" ? "\u00b7" : "!"} ${cap.name}`;
      row.appendChild(head);
      if (cap.blocker) {
        const b = el("div", "log-meta");
        b.textContent = "needs: " + cap.blocker;
        row.appendChild(b);
      }
      if (cap.state !== "used") {
        const t = el("div", "log-meta");
        t.style.opacity = ".85";
        t.textContent = "\u2192 " + cap.test;
        row.appendChild(t);
      }
      list.appendChild(row);
    });
  } catch (e) {
    list.innerHTML = "<div class='out-empty'>Could not load.</div>";
  }
}

/* -------------------------------- health -------------------------------- */
let _healthLast = null;
function openHealth() {
  const m = $("#healthModal");
  m.hidden = false; m.style.display = "flex";
  loadHealth();
}
function closeHealth() {
  const m = $("#healthModal");
  m.hidden = true; m.style.display = "none";
}
async function loadHealth() {
  const list = $("#healthList");
  list.innerHTML = "<div class='out-empty'>Checking\u2026</div>";
  try {
    const d = await fetch("/api/health/board").then(r => r.json());
    _healthLast = d;
    const c = d.counts || {};
    $("#healthInfo").textContent =
      `${c.ok || 0} ok \u00b7 ${c.warn || 0} warning \u00b7 ${c.fail || 0} failing`
      + (c.unknown ? ` \u00b7 ${c.unknown} unknown` : "") + ` \u00b7 build ${d.build}`;
    list.innerHTML = "";
    let group = "";
    (d.checks || []).forEach(ch => {
      if (ch.group !== group) {
        group = ch.group;
        const g = el("div", "foot-group-label");
        g.textContent = group;
        list.appendChild(g);
      }
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier",
        ch.state === "ok" ? "tier-live"
          : ch.state === "warn" ? "tier-gold"
          : ch.state === "fail" ? "tier-bronze" : "tier-silver");
      const head = el("div");
      head.style.fontWeight = "600";
      head.textContent = `${ch.state === "ok" ? "\u2713" : ch.state === "warn" ? "!" : ch.state === "fail" ? "\u2715" : "?"} ${ch.name}`;
      const det = el("div", "log-meta"); det.textContent = ch.detail;
      row.append(head, det);
      if (ch.name === "Circuit breakers" && ch.state !== "ok") {
        const rb = el("button", "btn ghost btn-mini");
        rb.style.marginTop = "6px"; rb.textContent = "Reset all breakers";
        rb.addEventListener("click", async () => {
          rb.disabled = true;
          try {
            const bs = await fetch("/api/breakers").then(r => r.json());
            for (const b of bs.breakers || []) {
              await fetch(`/api/breakers/${encodeURIComponent(b.feature)}/reset`,
                          { method: "POST" });
            }
            loadHealth();
          } catch (e) { rb.disabled = false; }
        });
        row.appendChild(rb);
      }
      if (ch.fix) {
        const fix = el("div", "log-meta");
        fix.style.opacity = ".85";
        fix.textContent = "\u2192 " + ch.fix;
        row.appendChild(fix);
      }
      list.appendChild(row);
    });
  } catch (e) {
    list.innerHTML = "<div class='out-empty'>Could not run the checks.</div>";
  }
}

/* -------------------------------- backup -------------------------------- */
function openBackup() {
  const m = $("#backupModal");
  m.hidden = false; m.style.display = "flex";
  $("#backupStatus").textContent = "";
  loadBackups();
}
function closeBackup() {
  const m = $("#backupModal");
  m.hidden = true; m.style.display = "none";
}
function fmtSize(b) {
  return b > 1048576 ? (b / 1048576).toFixed(1) + " MB"
                     : Math.max(1, Math.round(b / 1024)) + " KB";
}
async function loadBackups() {
  const list = $("#backupList");
  try {
    const d = await fetch("/api/backups").then(r => r.json());
    $("#backupNightly").checked = !!d.nightly;
    const bs = d.backups || [];
    $("#backupInfo").textContent = bs.length
      ? `${bs.length} backup(s) \u00b7 newest ${bs[0].created || bs[0].name}`
      : "No backups yet \u2014 make one now.";
    list.innerHTML = "";
    if (!bs.length) {
      list.innerHTML = "<div class='out-empty'>Nothing backed up yet. Everything Agent Jo has learned lives on this one disk until you do.</div>";
      return;
    }
    bs.forEach(b => {
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      row.classList.add("tier", b.includes_secret_key ? "tier-gold" : "tier-silver");
      const head = el("div"); head.style.fontWeight = "600";
      head.textContent = b.created || b.name;
      const meta = el("div", "log-meta");
      const c = b.counts || {};
      meta.textContent = [fmtSize(b.bytes),
        c.memories != null ? `${c.memories} memories` : null,
        c.skills != null ? `${c.skills} skills` : null,
        c.documents ? `${c.documents} docs` : null,
        b.includes_bulk ? "with renders" : null,
        b.includes_secret_key ? "\u26a0 contains secret.key" : null,
        b.note || null].filter(Boolean).join(" \u00b7 ");
      const act = el("div", "mcp-actions"); act.style.marginTop = "6px";
      const vf = el("button", "btn ghost btn-mini"); vf.textContent = "Verify";
      vf.addEventListener("click", async () => {
        $("#backupStatus").textContent = "Checking every file\u2026";
        const r = await fetch(`/api/backups/${b.name}/verify`, { method: "POST" }).then(r => r.json());
        $("#backupStatus").textContent = (r.ok ? "\u2713 " : "\u2715 ")
          + (r.detail || r.error || "") + (r.problems ? " \u2014 " + r.problems.join("; ") : "");
      });
      const dl = el("a", "btn ghost btn-mini");
      dl.textContent = "Download"; dl.href = `/api/backups/${b.name}/download`;
      dl.setAttribute("download", b.name);
      const rs = el("button", "btn ghost btn-mini"); rs.textContent = "Restore";
      rs.addEventListener("click", async () => {
        if (!confirm(`Restore ${b.name}?\n\nThis replaces current memories, skills, settings and crew state. A rollback point is taken first.`)) return;
        rs.disabled = true;
        $("#backupStatus").textContent = "Verifying, taking a rollback point, then restoring\u2026";
        try {
          const r = await fetch(`/api/backups/${b.name}/restore`, { method: "POST" });
          const d2 = await r.json();
          $("#backupStatus").textContent = r.ok
            ? `Restored ${(d2.restored || []).join(", ")}. Rollback point: ${d2.rollback || "n/a"}. ${d2.note || ""}`
            : (d2.detail || "Restore failed.");
          loadBackups();
        } catch (e) { $("#backupStatus").textContent = "Restore failed."; }
        rs.disabled = false;
      });
      const del = el("button", "btn ghost btn-mini"); del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete ${b.name}?`)) return;
        await fetch(`/api/backups/${b.name}`, { method: "DELETE" });
        loadBackups();
      });
      act.append(vf, dl, rs, del);
      row.append(head, meta, act);
      list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function makeBackup() {
  const b = $("#backupNowBtn");
  b.disabled = true; b.textContent = "Backing up\u2026";
  $("#backupStatus").textContent = "Snapshotting databases and collecting state\u2026";
  try {
    const r = await fetch("/api/backups/create", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_bulk: $("#backupBulk").checked,
                             include_key: $("#backupKey").checked }) });
    const d = await r.json();
    $("#backupStatus").textContent = r.ok ? `Saved ${d.name}` : errText(d, "Backup failed.");
    loadBackups();
  } catch (e) { $("#backupStatus").textContent = "Backup failed."; }
  b.disabled = false; b.textContent = "Back up now";
}

/* --------------------------------- jobs --------------------------------- */
function setTabBadge(id, n) {
  const b = $("#" + id);
  if (!b) return;
  b.textContent = n > 99 ? "99+" : String(n);
  b.hidden = !n;
}

function jobsTab(which) {
  const tabs = { find: "Find", tracked: "Tracked", claims: "Claims",
                 results: "Results", auto: "Auto", where: "Where" };
  Object.keys(tabs).forEach(k => {
    const btn = $("#jobsTab" + tabs[k]), pane = $("#jobsPane" + tabs[k]);
    if (btn) btn.classList.toggle("is-on", k === which);
    if (pane) pane.hidden = k !== which;
  });
  if (which === "where") { loadJobSearchConfig(); loadSuggestedSites(); }
  if (which === "tracked") loadJobs();
  if (which === "claims") loadClaims();
  if (which === "auto") loadAutoPreview();
  if (which === "results") loadOutcomes();
}

async function searchThisPage(useBrowser) {
  const b = useBrowser ? $("#jobsUrlBrowserBtn") : $("#jobsUrlBtn");
  const url = $("#jobsUrl").value.trim();
  if (!url) { $("#jobsStatus").textContent = "Paste a web address first."; return; }
  const label = b.textContent;
  b.disabled = true; b.textContent = "Reading…";
  $("#jobsStatus").textContent = useBrowser
    ? "Opening the page in a browser — this takes a few seconds…"
    : "Reading that page…";
  try {
    const r = await fetch("/api/jobs/search/url", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, use_browser: !!useBrowser }) });
    const d = await r.json();
    if (!r.ok) { $("#jobsStatus").textContent = errText(d, "Could not read that page."); }
    else {
      renderJobResults(d.results || [], d,
                       d.site + (d.how === "browser" ? " (via browser)" : ""));
      $("#jobsStatus").textContent = d.note || "";
    }
  } catch (e) { $("#jobsStatus").textContent = "Could not read that page."; }
  b.disabled = false; b.textContent = label;
}

function monogram(name) {
  const s = String(name || "?").trim();
  const parts = s.split(/[\s&/-]+/).filter(Boolean);
  const txt = (parts.length > 1
    ? parts[0][0] + parts[1][0]
    : s.slice(0, 2)).toUpperCase();
  // a stable colour per company: the same employer looks the same every
  // time, which is what makes a list scannable rather than decorative
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  const d = el("div", "job-mono");
  d.textContent = txt;
  d.style.background = `hsl(${h} 34% 30%)`;
  d.style.borderColor = `hsl(${h} 34% 42%)`;
  return d;
}

function renderBulkBar() {
  const bar = $("#jobsBulk");
  if (!bar) return;
  bar.hidden = _jobPicked.size === 0;
  const c = $("#jobsBulkCount");
  if (c) c.textContent = `${_jobPicked.size} selected`;
}

async function bulkDo(what) {
  const keys = Array.from(_jobPicked);
  if (!keys.length) return;
  const eng = (($("#jobsEngine") || {}).value || "");
  if (what === "remove") {
    try {
      const d = await fetch("/api/jobs/remove", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys, forget: true }) }).then(r => r.json());
      toast(`Removed ${d.removed} role(s). They won't be found again.`, "ok");
    } catch (e) { toast("Could not remove those.", "bad"); }
  } else {
    // A fetch that returns 502 does NOT throw, so counting every call that
    // didn't raise reported "scored 15 of 15" while all fifteen failed.
    let done = 0;
    const failures = [];
    for (const key of keys) {
      markRowBusy(key, true);
      try {
        const r = await fetch("/api/jobs/score", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, engine: eng }) });
        if (r.ok) { done++; }
        else {
          const d = await r.json().catch(() => ({}));
          failures.push(d.detail || `HTTP ${r.status}`);
        }
      } catch (e) { failures.push("could not reach the app"); }
      markRowBusy(key, false);
    }
    if (!failures.length) {
      toast(`Scored ${done} of ${keys.length}.`, "ok");
    } else {
      // fifteen identical failures are one problem, not fifteen
      const same = new Set(failures);
      toast(same.size === 1
        ? `None scored — ${[...same][0]}`
        : `Scored ${done} of ${keys.length}; ${failures.length} failed: ${failures[0]}`,
        "bad");
    }
  }
  _jobPicked.clear();
  renderBulkBar();
  loadJobs();
}

function jobRow(r, onPick) {
  const row = el("button", "job-row");
  row.type = "button";
  if (onPick && onPick.bulk !== false && r.key) {
    // acting on twenty roles one at a time is the slow part of a long list
    const pick = el("input", "job-pick");
    pick.type = "checkbox";
    pick.checked = _jobPicked.has(r.key);
    pick.addEventListener("click", (e) => {
      e.stopPropagation();              // ticking is not selecting
      if (pick.checked) _jobPicked.add(r.key); else _jobPicked.delete(r.key);
      renderBulkBar();
    });
    row.appendChild(pick);
    // A class, not an inline style. The score column is appended AFTER this,
    // and an inline three-column grid can't know about it — the fourth child
    // wrapped onto a second row, which is the "block that doesn't fit".
    row.classList.add("has-pick");
  }
  row.appendChild(monogram(r.company || r.source || r.title));
  const body = el("div", "job-row-body");
  const t = el("div", "job-row-title");
  const name = el("span");
  name.textContent = r.title || "(untitled)";
  t.appendChild(name);
  if (r.company) {
    const co = el("span", "job-row-co");
    co.textContent = r.company;
    t.appendChild(co);
  }
  // Fields joined with middle dots is chrome that shows up in generated
  // interfaces whatever the subject. Location and source are different
  // kinds of fact, so let them look different instead of being strung
  // together with a character that means nothing.
  const m = el("div", "job-row-meta");
  const loc = String(r.location || "").replace(/[,\s]+$/, "");
  if (loc) {
    const l = el("span", "meta-place");
    l.textContent = loc;
    m.appendChild(l);
  }
  if (r.source) {
    const s = el("span", "meta-source");
    s.textContent = r.source;
    m.appendChild(s);
  }
  const tags = el("div", "job-row-tags");
  const tag = (text, cls) => {
    const s = el("span", "job-tag" + (cls ? " " + cls : ""));
    s.textContent = text; tags.appendChild(s);
  };
  if (r.apply_email) tag("can email", "good"); else tag("portal", "");
  if (r.stage) tag(r.stage, r.stage === "applied" ? "acc" : "");
  // The score is the thing you scan a list by, so it gets its own column
  // rather than a pill among five others.
  if (r.fit && typeof r.fit.score === "number") {
    const sc = el("div", "job-score"
      + (r.fit.score >= 75 ? " good" : r.fit.score >= 50 ? "" : " weak"));
    const n = el("b"); n.textContent = r.fit.score;
    const of = el("i"); of.textContent = "fit";
    sc.append(n, of);
    // held back and appended AFTER the body — appending it here put the
    // score in the content column and pushed the title into the narrow one,
    // which is why the row read as a wide gap with the title on the right
    row._score = sc;
    row.classList.add("has-score");
  }
  if (r.already_tracked) tag("tracked", "acc");
  // a vacancy is perishable: say so on the row rather than leaving a role
  // found in July looking like one found this morning
  if (r.expired) {
    tag("expired" + (r.closed_at ? " " + String(r.closed_at).slice(0, 10) : ""),
        "warn");
  } else if (r.stage === "closed" && r.closed_at) {
    tag("closed " + String(r.closed_at).slice(0, 10), "");
  } else if (r.stale) {
    tag(`listed ${r.days_listed}d`, "warn");
  }
  // Tags on their own line made every row three lines tall. They're
  // secondary information, same as the location and source, so they belong
  // on the same line rather than claiming one of their own.
  if (tags.children.length) m.appendChild(tags);
  body.append(t, m);
  row.appendChild(body);
  if (row._score) row.appendChild(row._score);   // last: title, then score
  row.addEventListener("click", () => {
    // a row that isn't attached yet must not throw — clearing siblings is a
    // convenience, selecting is the point
    const siblings = row.parentElement ? row.parentElement.children : [row];
    Array.from(siblings).forEach(
      c => c.classList && c.classList.remove("is-on"));
    row.classList.add("is-on");
    onPick(r);
  });
  return row;
}

function emptyPane(host, title, body) {
  host.innerHTML = "";
  const box = el("div", "portal-empty");
  const h = el("strong"); h.textContent = title;
  const p = el("p"); p.textContent = body;
  box.append(h, p);
  host.appendChild(box);
}

function renderJobResults(found, meta, label) {
  const list = $("#jobsResults"), detail = $("#jobsFindDetail");
  list.innerHTML = "";
  // The summary used to concatenate every distinct reason, which meant one
  // line per city — eighty of them. Show the three biggest and a total.
  const rj = Object.entries((meta && meta.rejected) || {})
    .sort((a, b) => b[1] - a[1]);
  const skipped = rj.reduce((n, [, v]) => n + v, 0);
  const top = rj.slice(0, 3).map(([k, n]) => `${n} ${k}`).join(", ");
  const more = rj.length > 3 ? ` +${rj.length - 3} more` : "";
  $("#jobsInfo").textContent =
    `${(meta && meta.total) || found.length} found`
    + (label ? ` · ${label}` : "");
  const skipLine = $("#jobsSkipped");
  if (skipLine) {
    skipLine.textContent = skipped
      ? `${skipped} skipped — ${top}${more}`
      : "";
    skipLine.title = rj.map(([k, n]) => `${n} ${k}`).join("\n");
  }
  if (!found.length) {
    const errs = (meta && meta.errors) || [];
    emptyPane(list, "Nothing found",
      errs.length ? errs.join(" ")
        : ((meta && meta.note) ||
           "Try fewer words, or paste a job page into the box above."));
    emptyPane(detail, "", "Pick a role to see it here.");
    return;
  }
  const chosen = new Set();
  found.forEach(r => {
    list.appendChild(jobRow(r, (role) => {
      detail.innerHTML = "";
      const head = el("div", "detail-head");
      head.textContent = role.title || "";
      const sub = el("div", "detail-sub");
      sub.textContent = [role.company, role.location, role.source]
        .filter(Boolean).join(" · ");
      const acts = el("div", "detail-actions");
      const track = el("button", "btn primary btn-mini");
      track.textContent = role.already_tracked ? "Already tracked" : "Track this role";
      track.disabled = !!role.already_tracked;
      track.addEventListener("click", async () => {
        await addFromSearch([role]);
        track.textContent = "Tracked ✓"; track.disabled = true;
      });
      acts.appendChild(track);
      if (role.url) {
        const a = el("a", "btn ghost btn-mini");
        a.href = role.url; a.target = "_blank"; a.rel = "noopener";
        a.textContent = "Open advert"; acts.appendChild(a);
      }
      detail.append(head, sub, acts);
      if (role.summary) {
        const b = el("div", "detail-block");
        const h4 = el("h4"); h4.textContent = "From the advert";
        const pre = el("pre"); pre.textContent = role.summary;
        b.append(h4, pre); detail.appendChild(b);
      }
      if (!role.apply_email) {
        const b = el("div", "detail-block");
        const h4 = el("h4"); h4.textContent = "How you'd apply";
        const pre = el("pre");
        pre.textContent = "No address on the advert, so this is a portal "
          + "application. Track it, draft a letter, then use Apply on portal "
          + "from the Tracked tab.";
        b.append(h4, pre); detail.appendChild(b);
      }
      chosen.add(role);
    }));
  });
  const fresh = found.filter(r => !r.already_tracked);
  if (fresh.length) {
    const bar = el("div", "mcp-actions");
    bar.style.cssText = "position:sticky;bottom:0;padding:8px 2px 2px;background:var(--surface)";
    const all = el("button", "btn primary btn-mini");
    all.textContent = `Track all ${fresh.length}`;
    all.addEventListener("click", () => addFromSearch(fresh));
    bar.appendChild(all);
    list.appendChild(bar);
  }
  emptyPane(detail, "", "Pick a role on the left to see it here.");
}

async function searchJobs() {
  const b = $("#jobsSearchBtn");
  b.disabled = true; b.textContent = "Searching…";
  $("#jobsStatus").textContent = "Looking across your enabled boards…";
  try {
    const d = await fetch("/api/jobs/search", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: $("#jobsQuery").value }) })
      .then(r => r.json());
    renderJobResults(d.results || [], d, "your boards");
    $("#jobsStatus").textContent = "";
  } catch (e) { $("#jobsStatus").textContent = "Search failed."; }
  b.disabled = false; b.textContent = "Search boards";
}

async function addFromSearch(items) {
  if (!items.length) { $("#jobsStatus").textContent = "Nothing selected."; return; }
  try {
    const d = await fetch("/api/jobs/search/add", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }) }).then(r => r.json());
    $("#jobsStatus").textContent = `Now tracking ${d.added} role(s). Score them, or run auto-apply.`;
    loadJobs();
  } catch (e) { $("#jobsStatus").textContent = "Could not add those."; }
}

async function loadSuggestedSites() {
  const host = $("#jobsSuggested");
  if (!host) return;
  try {
    const d = await fetch("/api/jobs/sources/suggested").then(r => r.json());
    host.innerHTML = "";
    (d.suggested || []).forEach(s => {
      const row = el("div", "dash-edit-row");
      const lab = el("label");
      const nm = el("span");
      nm.textContent = `${s.name} — ${s.about}`;
      lab.appendChild(nm);
      const add = el("button", "btn ghost btn-mini");
      add.textContent = s.added ? "Added" : "Add";
      add.disabled = !!s.added;
      add.addEventListener("click", async () => {
        await fetch("/api/jobs/sources", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: s.name, url: s.url, kind: s.kind }) });
        loadJobSearchConfig(); loadSuggestedSites();
      });
      row.append(lab, add);
      host.appendChild(row);
    });
  } catch (e) { /* the panel works without suggestions */ }
}

async function loadJobSearchConfig() {
  try {
    const d = await fetch("/api/jobs/search/config").then(r => r.json());
    const s = d.search || {};
    $("#jobsExclude").value = (s.exclude || []).join(", ");
    $("#jobsLocations").value = (s.locations || []).join(", ");
    $("#jobsRemoteOnly").checked = s.remote_only !== false;
    $("#jobsNeedEmail").checked = !!s.require_email;
    const list = $("#jobsSourceList");
    list.innerHTML = "";
    if (!(d.sources || []).length) {
      const p = el("div", "log-meta");
      p.textContent = "No sites yet. Add any job page below, or pick one from Suggestions.";
      list.appendChild(p);
    }
    (d.sources || []).forEach(src => {
      const row = el("div", "dash-edit-row");
      const lab = el("label");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = src.on !== false;
      cb.addEventListener("change", async () => {
        await fetch("/api/jobs/sources", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: src.url || src.name,
                                 on: cb.checked }) });
      });
      const nm = el("span"); nm.textContent = `${src.name} (${src.kind})`;
      lab.append(cb, nm);
      const del = el("button", "btn ghost btn-mini"); del.textContent = "Remove";
      del.addEventListener("click", async () => {
        const r = await fetch("/api/jobs/sources/remove", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: src.name, url: src.url }) });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          $("#jobsStatus").textContent = errText(d, "Could not remove that site.");
        } else {
          $("#jobsStatus").textContent = `Removed ${src.name}.`;
        }
        loadJobSearchConfig(); loadSuggestedSites();
      });
      row.append(lab, del);
      list.appendChild(row);
    });
  } catch (e) { /* panel still usable without it */ }
}

async function saveJobSearchConfig() {
  const split = (v) => (v || "").split(/[,;]/).map(s => s.trim()).filter(Boolean);
  try {
    await fetch("/api/jobs/search/config", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        exclude: split($("#jobsExclude").value),
        locations: split($("#jobsLocations").value),
        remote_only: $("#jobsRemoteOnly").checked,
        require_email: $("#jobsNeedEmail").checked }) });
    $("#jobsStatus").textContent = "Search settings saved — they apply to the daily run too.";
  } catch (e) { /* non-fatal */ }
}

function openJobs() {
  const m = $("#jobsModal");
  m.hidden = false; m.style.display = "flex";
  $("#jobsDraft").textContent = ""; $("#jobsStatus").textContent = "";
  jobsTab("find");
  loadJobs();
  // so the badges are right before you've opened those tabs
  fetch("/api/jobs/claims").then(r => r.json())
    .then(d => setTabBadge("jobsBadgeClaims", d.count || 0))
    .catch(() => {});
}
function closeJobs() {
  const m = $("#jobsModal");
  m.hidden = true; m.style.display = "none";
}
async function loadOutcomes() {
  const head = $("#jobsResultsHead"), list = $("#jobsFindings");
  const detail = $("#jobsBreakdown");
  list.innerHTML = ""; detail.innerHTML = "";
  try {
    const d = await fetch("/api/jobs/outcomes").then(r => r.json());
    head.innerHTML = "";
    const line = el("div", "auto-line");
    line.textContent = d.sent
      ? `${d.replied} repl${d.replied === 1 ? "y" : "ies"} from `
        + `${d.sent} application(s) — between ${d.overall.low}% and `
        + `${d.overall.high}%.`
      : "Nothing sent yet, so there's nothing to learn from.";
    head.appendChild(line);
    if (d.still_waiting.count) {
      const w = el("div", "log-meta");
      w.textContent = `${d.still_waiting.count} still waiting, the oldest `
        + `${d.still_waiting.longest_days} days.`;
      head.appendChild(w);
    }

    // findings first: the answers, before the tables they came from
    (d.findings || []).forEach(f => {
      const row = el("div", "auto-row" + (f.confident ? " good" : ""));
      const t = el("div", "auto-row-title");
      t.textContent = f.what;
      const why = el("div", "log-meta");
      why.textContent = f.why;
      const doit = el("div", "log-meta");
      doit.style.color = "var(--text)";
      doit.textContent = f.do;
      row.append(t, why, doit);
      if (!f.confident) {
        const tag = el("span", "job-tag warn");
        tag.textContent = "not conclusive";
        t.appendChild(tag);
      }
      list.appendChild(row);
    });

    // the tables underneath, with the range on every rate
    const table = (title, rows, key) => {
      if (!rows || !rows.length) return;
      const b = el("div", "detail-block");
      const h = el("h4"); h.textContent = title;
      b.appendChild(h);
      rows.forEach(r => {
        const line2 = el("div", "outcome-row");
        const name = el("span", "outcome-name");
        name.textContent = r[key];
        const bar = el("span", "outcome-bar");
        const fill = el("span", "outcome-fill");
        fill.style.left = r.low + "%";
        fill.style.width = Math.max(1, r.high - r.low) + "%";
        const dot = el("span", "outcome-dot");
        dot.style.left = r.rate + "%";
        bar.append(fill, dot);
        const num = el("span", "outcome-num");
        num.textContent = r.conclusive
          ? `${r.rate}%`
          : `${r.replied}/${r.sent}`;
        num.title = r.conclusive
          ? `${r.reading}`
          : `${r.reading} — ${r.needed} more before this means anything`;
        line2.append(name, bar, num);
        if (!r.conclusive) line2.classList.add("is-thin");
        b.appendChild(line2);
      });
      detail.appendChild(b);
    };
    table("By source", d.by_source, "source");
    table("By fit score", d.by_score, "band");
    table("By how you applied", d.by_method, "method");
    const n = el("div", "log-meta");
    n.style.marginTop = "8px";
    n.textContent = d.note;
    detail.appendChild(n);
  } catch (e) {
    emptyPane(list, "Couldn't work that out", "Try again.");
  }
}

async function loadAutoPreview() {
  const list = $("#jobsAutoPreview"), side = $("#jobsAutoSide");
  const sum = $("#jobsAutoSummary");
  if (!list) return;
  list.innerHTML = ""; side.innerHTML = "";
  try {
    const d = await fetch("/api/jobs/auto/preview").then(r => r.json());
    // the one line that answers "so what happens if I press it"
    sum.innerHTML = "";
    const head = el("div", "auto-line" + (d.enabled ? "" : " is-off"));
    head.textContent = d.summary;
    sum.appendChild(head);
    const chips = el("div", "auto-chips");
    const chip = (label, val, tone) => {
      const c = el("span", "auto-chip" + (tone ? " " + tone : ""));
      const b = el("b"); b.textContent = String(val);
      const s = el("span"); s.textContent = " " + label;
      c.append(b, s);
      chips.appendChild(c);
    };
    chip("on today", `${d.sent_today}/${d.daily_cap}`,
         d.room_today ? "" : "warn");
    chip("minimum fit", d.min_score);
    chip(d.dry_run ? "rehearsal" : "live", d.dry_run ? "✓" : "!",
         d.dry_run ? "" : "warn");
    sum.appendChild(chips);

    const group = (title, items, tone, describe) => {
      if (!items || !items.length) return;
      const h = el("div", "palette-row-group");
      h.textContent = `${title} (${items.length})`;
      list.appendChild(h);
      items.slice(0, 12).forEach(it => {
        const row = el("div", "auto-row" + (tone ? " " + tone : ""));
        const t = el("div", "auto-row-title");
        t.textContent = typeof it === "string" ? it : (it.title || "");
        row.appendChild(t);
        const why = describe ? describe(it) : "";
        if (why) {
          const w = el("div", "log-meta");
          w.textContent = why;
          row.appendChild(w);
        }
        list.appendChild(row);
      });
    };
    group("Would send", d.would_send, "good",
          it => `to ${it.to}${it.fit ? ` · fit ${it.fit}` : ""}`);
    group("Over today's limit", d.capped, "warn",
          () => "waits for tomorrow");
    group("Needs scoring first", d.needs_scoring, "");
    group("Held back", d.would_hold, "warn", it => it.why);
    group("No application address", d.no_address, "",
          () => "portal only — apply from Tracked");
    group("Ignored, no engine call spent", d.screened_out, "", it => it.why);
    if (!list.children.length) {
      emptyPane(list, "Nothing queued",
        "Find some roles first, or lower the minimum fit.");
    }

    // what it has actually been doing
    side.innerHTML = "";
    const sh = el("div", "detail-head");
    sh.textContent = "Recent unattended runs";
    side.appendChild(sh);
    if (!(d.runs || []).length) {
      const p = el("div", "log-meta");
      p.textContent = "It hasn't run on its own yet. “Run this daily” sets that up.";
      side.appendChild(p);
    } else {
      d.runs.forEach(r => {
        const row = el("div", "auto-row");
        const t = el("div", "auto-row-title"); t.textContent = r.at;
        const w = el("div", "log-meta"); w.textContent = `${r.what}: ${r.detail}`;
        row.append(t, w);
        side.appendChild(row);
      });
    }
    if ((d.follow_ups || []).length) {
      const b = el("div", "detail-block");
      const h = el("h4"); h.textContent = "Waiting on a reply";
      const pre = el("pre"); pre.textContent = d.follow_ups.join("\n");
      b.append(h, pre);
      side.appendChild(b);
    }
    const safety = el("div", "detail-block");
    const sh2 = el("h4"); sh2.textContent = "What it will never do";
    const pre2 = el("pre");
    pre2.textContent = "Send a draft that claims something your profile "
      + "can't support. Apply twice to the same role. Exceed the daily cap. "
      + "Submit a portal form — those always stop for you.";
    safety.append(sh2, pre2);
    side.appendChild(safety);
  } catch (e) {
    emptyPane(list, "Couldn't work that out", "Try again.");
  }
}

async function loadClaims() {
  const list = $("#jobsClaimList"), detail = $("#jobsClaimDetail");
  list.innerHTML = "";
  try {
    const d = await fetch("/api/jobs/claims").then(r => r.json());
    setTabBadge("jobsBadgeClaims", d.count || 0);
    $("#jobsInfo").textContent = d.count
      ? (d.profile_empty
          ? `${d.count} draft(s) held — profile empty`
          : `${d.count} draft(s) held · ${d.distinct} claim(s) to decide`)
      : "Nothing held.";
    if (d.profile_empty && d.count) {
      // listing 40 terms to approve one at a time is the wrong instruction
      // when the cause is a single missing thing
      emptyPane(list, "Your profile is empty",
        `All ${d.count} drafts are held because there's nothing to check them `
        + `against — every name and tool in them reads as unsourced `
        + `(${d.would_be_claims} of them). Ask in chat: “build my job profile `
        + `from my CV”, then redraft. Everything unblocks at once.`);
      emptyPane(detail, "", "");
      return;
    }
    // The tab listed CLAIMS and never the drafts themselves, so a draft held
    // for a reason naming no claim showed as an empty tab beside a non-zero
    // count. Show the drafts first — they're what's actually blocked.
    const held = d.held || [];
    if (!held.length && !d.claims.length) {
      emptyPane(list, "Nothing held",
        d.drafted_ready
          ? `No draft is held. ${d.drafted_ready} are drafted and ready — you'll find them in Tracked.`
          : "Every draft is backed by your profile. Auto-apply won't be blocked.");
      emptyPane(detail, "", "");
      return;
    }
    if (held.length) {
      const hh = el("div", "palette-row-group");
      hh.textContent = `${held.length} draft(s) held`;
      list.appendChild(hh);
      held.forEach(hd => {
        const row = el("button", "job-row");
        row.type = "button";
        row.appendChild(monogram(hd.company || hd.title));
        const body = el("div", "job-row-body");
        const t = el("div", "job-row-title");
        const nm = el("span"); nm.textContent = hd.title || "(untitled)";
        t.appendChild(nm);
        if (hd.company) {
          const co = el("span", "job-row-co"); co.textContent = hd.company;
          t.appendChild(co);
        }
        const m = el("div", "job-row-meta");
        m.textContent = hd.needs_redraft
          ? (hd.redraft_reason || "needs rewriting")
          : (hd.problems || []).length
            ? `${hd.problems.length} unsourced claim(s)`
            : "held, but no specific claim was named";
        body.append(t, m);
        row.appendChild(body);
        row.addEventListener("click", () => {
          Array.from(list.children).forEach(c2 => c2.classList
            && c2.classList.remove("is-on"));
          row.classList.add("is-on");
          showHeldDraft(hd);
        });
        list.appendChild(row);
      });
    }
    if (d.claims.length) {
      const ch = el("div", "palette-row-group");
      ch.textContent = "Claims to decide";
      list.appendChild(ch);
    }
    d.claims.forEach(c => {
      const row = el("button", "job-row");
      row.type = "button";
      const t = el("div", "job-row-title"); t.textContent = c.term;
      const m = el("div", "job-row-meta");
      m.textContent = `claimed in ${c.roles.length} draft(s)`;
      row.append(t, m);
      row.addEventListener("click", () => {
        Array.from(list.children).forEach(x => x.classList
          && x.classList.remove("is-on"));
        row.classList.add("is-on");
        showClaim(c);
      });
      list.appendChild(row);
    });
    emptyPane(detail, "", "Pick a claim to confirm or dismiss it.");
  } catch (e) {
    emptyPane(list, "Couldn't load", "Try again.");
  }
}

function showHeldDraft(hd) {
  const detail = $("#jobsClaimDetail");
  detail.innerHTML = "";
  const head = el("div", "detail-head");
  head.textContent = hd.title || "";
  const sub = el("div", "detail-sub");
  sub.textContent = hd.company || "";
  detail.append(head, sub);

  const acts = el("div", "detail-actions");
  const rd = el("button", "btn primary btn-mini");
  rd.textContent = "Redraft this";
  rd.addEventListener("click", async () => {
    rd.disabled = true;
    const eng = (($("#jobsEngine") || {}).value || "");
    try {
      await fetch("/api/jobs/draft", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: hd.key, engine: eng }) });
      toast("Redrafted — the check runs again automatically.", "ok");
      loadClaims();
    } catch (e) { toast("Could not redraft that.", "bad"); }
    rd.disabled = false;
  });
  acts.appendChild(rd);
  detail.appendChild(acts);

  const blk = (title, text) => {
    if (!text) return;
    const b = el("div", "detail-block");
    const h = el("h4"); h.textContent = title;
    const pre = el("pre"); pre.textContent = text;
    b.append(h, pre);
    detail.appendChild(b);
  };
  if ((hd.problems || []).length) {
    blk("Why it's held", hd.problems.map(p => "\u2022 " + p).join("\n"));
  } else if (hd.redraft_reason) {
    blk("Why it's held", hd.redraft_reason);
  } else {
    // the case that produced an empty tab: held, but nothing nameable
    blk("Why it's held",
        "The check failed without naming a specific claim. Redrafting "
        + "usually clears it — the draft itself is below so you can see "
        + "what it says.");
  }
  blk("Subject", hd.subject);
  blk("The draft", hd.body);
}

function showClaim(c) {
  const detail = $("#jobsClaimDetail");
  detail.innerHTML = "";
  const head = el("div", "detail-head"); head.textContent = c.term;
  const sub = el("div", "detail-sub");
  sub.textContent = c.detail || "";
  detail.append(head, sub);

  const q = el("div", "detail-block");
  const qh = el("h4"); qh.textContent = "Is this true of you?";
  const qp = el("pre");
  qp.textContent = "If yes, say where it belongs and it will be added to "
    + "your profile — every held draft is then re-checked immediately, at no "
    + "cost. If no, dismiss it and drafts won't use it again.";
  q.append(qh, qp); detail.appendChild(q);

  const acts = el("div", "detail-actions");
  const where = el("select"); where.className = "model-select";
  [["technologies", "a tool I've used"],
   ["skills", "a skill I have"],
   ["achievements", "something I did"],
   ["employers", "somewhere I worked"]].forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = label; where.appendChild(o);
  });
  const yes = el("button", "btn primary btn-mini");
  yes.textContent = "Yes — add to my profile";
  yes.addEventListener("click", async () => {
    yes.disabled = true; yes.textContent = "Adding…";
    try {
      const r = await fetch("/api/jobs/claims/confirm", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term: c.term, where: where.value }) })
        .then(x => x.json());
      $("#jobsStatus").textContent = r.cleared
        ? `Added. ${r.cleared} draft(s) cleared and can now be sent.`
        : `Added to your profile. ${r.still_held || 0} draft(s) still held for other claims.`;
      loadClaims(); refreshDashboard();
    } catch (e) { $("#jobsStatus").textContent = "Could not add that."; }
    yes.disabled = false; yes.textContent = "Yes — add to my profile";
  });
  const no = el("button", "btn ghost btn-mini");
  no.textContent = "No — don't claim this";
  no.addEventListener("click", async () => {
    await fetch("/api/jobs/claims/dismiss", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ term: c.term }) });
    $("#jobsStatus").textContent =
      `Noted. Redraft those roles and “${c.term}” won't be used.`;
    loadClaims();
  });
  acts.append(where, yes, no);
  detail.appendChild(acts);

  if (c.context) {
    const b = el("div", "detail-block");
    const h = el("h4"); h.textContent = "Where it appears";
    const pre = el("pre"); pre.textContent = c.context;
    b.append(h, pre); detail.appendChild(b);
  }
  const rb = el("div", "detail-block");
  const rh = el("h4"); rh.textContent = "Drafts affected";
  const rp = el("pre"); rp.textContent = (c.roles || []).join("\n");
  rb.append(rh, rp); detail.appendChild(rb);
}

let _jobFilters = { email: false, strong: false, unscored: false,
                    openonly: false };
let _jobSort = "fit";
const _jobPicked = new Set();
let _jobSel = "";          // the key of the role you're looking at
let _jobRoles = [];        // last payload, so actions can update in place

async function loadPipeline() {
  const host = $("#jobsPipeline");
  if (!host) return;
  try {
    const p = await fetch("/api/jobs/pipeline").then(r => r.json());
    if (!p.total) { host.hidden = true; return; }
    host.hidden = false;
    host.innerHTML = "";
    const steps = [["found", "found"], ["scored", "scored"],
                   ["drafted", "drafted"], ["held", "held back"],
                   ["applied", "applied"], ["responded", "replied"]];
    steps.forEach(([key, label]) => {
      const n = p.stages[key] || 0;
      const s = el("div", "pipe-step"
        + (p.blocked_at === key ? " is-blocked" : "")
        + (n ? "" : " is-empty"));
      const v = el("div", "pipe-n"); v.textContent = n;
      const l = el("div", "pipe-l"); l.textContent = label;
      s.append(v, l);
      host.appendChild(s);
    });
    if (p.blocked_at) {
      const w = el("div", "pipe-why");
      w.textContent = p.why;
      host.appendChild(w);
    }
  } catch (e) { host.hidden = true; }
}

let _showArchive = false;

async function loadArchive() {
  const list = $("#jobsList"), detail = $("#jobsDetail");
  list.innerHTML = "";
  try {
    const d = await fetch("/api/jobs/archive").then(r => r.json());
    $("#jobsTrackedCount").textContent = `${d.count} archived`;
    if (!d.count) {
      emptyPane(list, "Nothing archived",
        "Closed and expired roles land here when you archive them — kept, "
        + "so you can see you'd already looked at a company.");
      emptyPane(detail, "", "");
      return;
    }
    d.roles.forEach(a => {
      const row = el("button", "job-row");
      row.type = "button";
      row.appendChild(monogram(a.company || a.title));
      const body = el("div", "job-row-body");
      const t = el("div", "job-row-title");
      const nm = el("span"); nm.textContent = a.title || "(untitled)";
      t.appendChild(nm);
      if (a.company) {
        const co = el("span", "job-row-co"); co.textContent = a.company;
        t.appendChild(co);
      }
      const m = el("div", "job-row-meta");
      m.textContent = `${a.why}${a.when ? " · " + a.when.slice(0, 10) : ""}`;
      body.append(t, m);
      row.appendChild(body);
      row.addEventListener("click", () => {
        detail.innerHTML = "";
        const h = el("div", "detail-head"); h.textContent = a.title;
        const s2 = el("div", "detail-sub");
        s2.textContent = [a.company, a.was_applied ? "you applied" : null,
                          a.when].filter(Boolean).join(" · ");
        detail.append(h, s2);
        const acts = el("div", "detail-actions");
        const back = el("button", "btn ghost btn-mini");
        back.textContent = "Put it back";
        back.addEventListener("click", async () => {
          const res = await fetch("/api/jobs/unarchive", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: a.key }) });
          const dd = await res.json().catch(() => ({}));
          toast(res.ok ? `${dd.title}: ${dd.note}` : errText(dd, "Could not."),
                res.ok ? "ok" : "warn");
          if (res.ok) loadArchive();
        });
        acts.appendChild(back);
        detail.appendChild(acts);
        const b = el("div", "detail-block");
        const bh = el("h4"); bh.textContent = "Why it was archived";
        const pre = el("pre"); pre.textContent = a.why;
        b.append(bh, pre); detail.appendChild(b);
      });
      list.appendChild(row);
    });
    emptyPane(detail, "", "Pick one to see it, or put it back.");
  } catch (e) {
    emptyPane(list, "Couldn't load the archive", "Try again.");
  }
}

async function loadJobs() {
  if (_showArchive) return loadArchive();
  const list = $("#jobsList"), detail = $("#jobsDetail");
  if (!list) return;
  try {
    const s = await fetch("/api/jobs").then(r => r.json());
    const a = s.auto || {};
    $("#jobsAutoOn").checked = !!a.enabled;
    $("#jobsDryRun").checked = a.dry_run !== false;
    $("#jobsMinScore").value = a.min_score ?? 75;
    $("#jobsCap").value = a.daily_cap ?? 5;
    $("#jobsCleanOnly").checked = a.require_clean_check !== false;
    const hint = $("#jobsProfileHint");
    if (hint) {
      hint.textContent = s.profile_ready ? ""
        : "Your profile is empty, so nothing can be drafted. Ask in chat: \u201cbuild my job profile from my CV\u201d.";
    }
    const eng0 = $("#jobsEngine");
    if (eng0 && !eng0.children.length) {
      const t = await fetch("/api/trends").then(r => r.json()).catch(() => ({}));
      (t.engines || ["Auto"]).forEach(n => {
        const o = document.createElement("option");
        o.value = n; o.textContent = n; eng0.appendChild(o);
      });
    }
    const want = ($("#jobsStageFilter") || {}).value || "";
    let roles = (s.roles || []).filter(r => !want || r.stage === want);
    // quick filters answer the questions you actually ask of a long list:
    // what can it send, what is worth my time, what hasn't been looked at
    if (_jobFilters.email) roles = roles.filter(r => r.apply_email);
    if (_jobFilters.strong) {
      roles = roles.filter(r => ((r.fit || {}).score || 0) >= 75);
    }
    if (_jobFilters.unscored) {
      roles = roles.filter(r => (r.fit || {}).score === undefined
                             || (r.fit || {}).score === null);
    }
    if (_jobFilters.openonly) {
      roles = roles.filter(r => !r.expired && r.stage !== "closed");
    }
    const bystage = ["found", "drafted", "applied", "responded",
                     "interview", "offer", "closed"];
    roles = roles.slice().sort((a, b) => {
      if (_jobSort === "new") return (b.added_at || "").localeCompare(a.added_at || "");
      if (_jobSort === "company") {
        return (a.company || "").localeCompare(b.company || "");
      }
      if (_jobSort === "stage") {
        return bystage.indexOf(a.stage) - bystage.indexOf(b.stage);
      }
      return ((b.fit || {}).score || -1) - ((a.fit || {}).score || -1);
    });
    $("#jobsTrackedCount").textContent =
      `${roles.length} of ${(s.roles || []).length} role(s)`;
    setTabBadge("jobsBadgeTracked", (s.roles || []).length);
    list.innerHTML = "";
    if (!roles.length) {
      emptyPane(list, "Nothing tracked yet",
        "Use the Find tab to search your sites, or paste any job page — then press Track.");
      emptyPane(detail, "", "Pick a role to work on it.");
      return;
    }
    _jobRoles = roles;
    loadPipeline();
    roles.forEach(r => {
      const row = jobRow(r, (role) => { _jobSel = role.key; showJobDetail(role); });
      row.dataset.key = r.key;
      list.appendChild(row);
    });
    // Every action used to reload the list, which threw away whatever you had
    // open — click Draft and the pane you were reading vanished. Put you back
    // where you were.
    const keep = roles.find(r => r.key === _jobSel);
    if (keep) {
      const row = Array.from(list.children)
        .find(c => c.dataset && c.dataset.key === keep.key);
      if (row) row.classList.add("is-on");
      showJobDetail(keep);
      if (row && row.scrollIntoView) {
        row.scrollIntoView({ block: "nearest" });
      }
    } else {
      emptyPane(detail, "", "Pick a role on the left to score, draft or apply.");
    }
  } catch (e) {
    emptyPane(list, "Couldn't load", "The app is still working; try again.");
  }
}

function markRowBusy(key, busy) {
  // show the work where it was asked for, not only at the foot of the panel
  const list = $("#jobsList");
  if (!list) return;
  const row = Array.from(list.children)
    .find(c => c.dataset && c.dataset.key === key);
  if (row) row.classList.toggle("is-busy", !!busy);
}

function showJobDetail(r) {
  _jobSel = r.key;
  const detail = $("#jobsDetail");
  detail.innerHTML = "";
  const head = el("div", "detail-head"); head.textContent = r.title || "";
  const sub = el("div", "detail-sub");
  sub.textContent = [r.company, r.location, r.stage,
                     r.apply_email || "portal application"]
    .filter(Boolean).join(" · ");
  if (r.closed_at) {
    const cl = el("div", "detail-sub");
    cl.textContent = `${r.expired ? "Expired" : "Closed"} ${r.closed_at}`
      + (r.closed_reason ? ` — ${r.closed_reason}` : "");
    detail.appendChild(cl);
  }
  detail.append(head, sub);

  const acts = el("div", "detail-actions");
  const mk = (label, cls, fn) => {
    const b = el("button", "btn " + cls + " btn-mini");
    b.textContent = label;
    b.addEventListener("click", async () => {
      b.disabled = true;
      const was = b.textContent; b.textContent = "Working…";
      markRowBusy(r.key, true);
      try { await fn(); } catch (e) { /* status line reports it */ }
      markRowBusy(r.key, false);
      b.textContent = was; b.disabled = false;
    });
    acts.appendChild(b);
    return b;
  };
  const engine = () => (($("#jobsEngine") || {}).value || "");
  mk("Score fit", "ghost", async () => {
    const res = await fetch("/api/jobs/score", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: r.key, engine: engine() }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(errText(d, "Could not score that."), "bad"); return; }
    loadJobs();
  });
  mk("Draft application", "primary", async () => {
    const res = await fetch("/api/jobs/draft", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: r.key, engine: engine() }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(errText(d, "Could not draft that."), "bad"); return; }
    loadJobs();
  });
  if (r.url && !r.apply_email) {
    mk("Apply on portal", "ghost", async () => {
      $("#jobsStatus").textContent = "Opening the form — the browser window is on this computer.";
      const d = await fetch("/api/jobs/portal", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: r.key, submit: false }) }).then(x => x.json());
      $("#jobsStatus").textContent = `[${d.ats || "unknown"}] ${d.state}: ${d.message || ""}`;
      loadJobs();
    });
  }
  mk("Match", "ghost", async () => {
    const d = await fetch("/api/jobs/ats", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: r.key }) }).then(x => x.json());
    const b = el("div", "detail-block");
    const h = el("h4");
    h.textContent = `Keyword match — ${d.score}% of ${d.total_terms} term(s)`;
    const pre = el("pre");
    pre.textContent = [
      d.matched.length ? "You can evidence: " + d.matched.join(", ") : "",
      d.missing.length ? "Not in your profile: " + d.missing.join(", ") : "",
      ...(d.advice || []), d.note].filter(Boolean).join("\n\n");
    b.append(h, pre);
    detail.appendChild(b);
    b.scrollIntoView({ block: "nearest" });
  });
  mk("Tailor CV", "ghost", async () => {
    const res = await fetch("/api/jobs/cv", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: r.key, engine: engine() }) });
    const d = await res.json();
    if (!res.ok) { $("#jobsStatus").textContent = errText(d, "Could not tailor it."); return; }
    $("#jobsStatus").textContent = `CV written to ${d.path}`;
    loadJobs();
  });
  mk("Interview prep", "ghost", async () => {
    const res = await fetch("/api/jobs/interview", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: r.key, engine: engine() }) });
    const d = await res.json();
    if (!res.ok) { $("#jobsStatus").textContent = errText(d, "Could not prepare."); return; }
    loadJobs();
  });
  if (r.url) {
    const a = el("a", "btn ghost btn-mini");
    a.href = r.url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "Open advert"; acts.appendChild(a);
  }
  // Most applications are still made by a person — a portal form, an email
  // you wrote, a referral. Without this the role sat looking untouched: no
  // follow-up clock, and a real chance of applying twice.
  if (!["applied", "waiting", "closed", "expired"].includes(r.bucket || "")) {
    mk("I applied", "primary", async () => {
      const res = await fetch("/api/jobs/applied", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: r.key,
          how: r.apply_email ? "by email" : "on their portal" }) });
      const d = await res.json().catch(() => ({}));
      toast(res.ok ? `${d.title}: ${d.note}` : errText(d, "Could not record that."),
            res.ok ? "ok" : "warn");
      if (res.ok) loadJobs();
    });
  }
  const rm = el("button", "btn ghost btn-mini");
  rm.textContent = "Not interested";
  rm.title = "Stop tracking this, and don't find it again";
  rm.addEventListener("click", async () => {
    rm.disabled = true;
    try {
      const d = await fetch("/api/jobs/remove", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: r.key, forget: true }) })
        .then(x => x.json());
      $("#jobsStatus").textContent = d.title
        ? `Removed “${d.title}”. It won't be found again.`
        : "Removed.";
      emptyPane($("#jobsDetail"), "", "Pick a role on the left.");
      loadJobs(); refreshDashboard();
    } catch (e) { $("#jobsStatus").textContent = "Could not remove that."; }
    rm.disabled = false;
  });
  acts.appendChild(rm);
  detail.appendChild(acts);

  const block = (title, text) => {
    if (!text) return;
    const b = el("div", "detail-block");
    const h = el("h4"); h.textContent = title;
    const pre = el("pre"); pre.textContent = text;
    b.append(h, pre); detail.appendChild(b);
  };
  if (r.fit) {
    const b = el("div", "detail-block");
    const h = el("h4"); h.textContent = "Fit";
    const pill = el("div", "score-pill");
    pill.textContent = `${r.fit.score}/100 — ${r.fit.verdict || ""}`;
    b.append(h, pill);
    if ((r.fit.against || []).length) {
      const pre = el("pre");
      pre.textContent = "Against: " + r.fit.against.join("; ");
      b.appendChild(pre);
    }
    if ((r.fit.missing || []).length) {
      const pre = el("pre");
      pre.textContent = "Missing: " + r.fit.missing.join("; ");
      b.appendChild(pre);
    }
    detail.appendChild(b);
  }
  if (r.draft) {
    block("Draft — subject", r.draft.subject);
    block("Draft — body", r.draft.body);
    const c = r.draft.check || {};
    if (c.ok === false) {
      block("Verify before sending",
        (c.problems || []).map(p => "• " + p.detail).join("\n"));
    }
  }
  if (r.tailored_cv) {
    const c = r.tailored_cv;
    block("Tailored CV — headline", c.headline);
    block("Tailored CV — summary", c.summary);
    block("Tailored CV — key skills", (c.key_skills || []).join(", "));
    if ((c.left_out || []).length) {
      block("Left out (the advert wanted these; your profile can't support them)",
            (c.left_out || []).join("\n"));
    }
    if (c.check && c.check.ok === false) {
      block("CV needs checking",
        (c.check.problems || []).map(p => "• " + p.detail).join("\n"));
    }
  }
  if (r.interview) {
    block("Likely questions",
      (r.interview.questions || []).map(q =>
        `Q: ${q.question}\n   ${q.answer_from_profile || "(nothing in your profile — think about this one)"}`
      ).join("\n\n"));
    if ((r.interview.ask_them || []).length) {
      block("Worth asking them", (r.interview.ask_them || []).join("\n"));
    }
  }
  block("From the advert", r.summary);
}

function showDraft(d) {
  const lines = [`Subject: ${d.subject}`, "", d.body];
  if ((d.gaps || []).length) lines.push("", "Asked for, not in your profile: " + d.gaps.join("; "));
  const c = d.check || {};
  if (c.ok === false) {
    lines.push("", "\u26a0 VERIFY BEFORE SENDING \u2014 claims not sourced from your profile:");
    (c.problems || []).forEach(p => lines.push("  - " + p.detail));
  } else {
    lines.push("", "\u2713 Every specific in this draft traces to your profile or the advert.");
  }
  lines.push("", "Copy it, check it, send it yourself.");
  $("#jobsDraft").textContent = lines.join("\n");
}

/* --------------------------------- theme -------------------------------- */
/* One control, five choices. "System" follows the OS the way a native app
   does — including live, if you flip Windows to dark while this is open.
   Instruments and Midnight are dark-only palettes, so they simply don't
   participate in the light/dark switch; pretending otherwise would produce
   an unreadable half-theme. */
const THEMES = {
  system: { label: "System (follow Windows)", dark: "fluent", light: "fluent-light" },
  "fluent-light": { label: "Light", fixed: "fluent-light" },
  fluent: { label: "Dark", fixed: "fluent" },
  instruments: { label: "Instruments (dark)", fixed: "instruments" },
  midnight: { label: "Midnight (dark)", fixed: "midnight" },
};
let _themeMedia = null;

function currentTheme() {
  try { return localStorage.getItem("agentjo-theme") || "system"; }
  catch (e) { return "system"; }
}
function resolveTheme(choice) {
  const t = THEMES[choice] || THEMES.system;
  if (t.fixed) return t.fixed;
  const prefersDark = window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return prefersDark ? t.dark : t.light;
}
function paintTheme(choice) {
  const resolved = resolveTheme(choice);
  // "instruments" is what :root already defines, so it needs no attribute
  if (resolved === "instruments") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", resolved);
  }
}
function applyTheme(choice) {
  const c = THEMES[choice] ? choice : "system";
  try { localStorage.setItem("agentjo-theme", c); } catch (e) {}
  paintTheme(c);
  // follow the OS live, but only while "System" is selected
  if (window.matchMedia) {
    if (!_themeMedia) {
      _themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = () => {
        if (currentTheme() === "system") paintTheme("system");
      };
      if (_themeMedia.addEventListener) _themeMedia.addEventListener("change", onChange);
      else if (_themeMedia.addListener) _themeMedia.addListener(onChange);
    }
  }
}

/* --------------------------------- crew --------------------------------- */
function openCrew() {
  const m = $("#crewModal");
  m.hidden = false; m.style.display = "flex";
  $("#crewReport").textContent = ""; $("#crewStatus").textContent = "";
  loadCrew();
}
function closeCrew() {
  const m = $("#crewModal");
  m.hidden = true; m.style.display = "none";
}
async function loadCrew() {
  const list = $("#crewList");
  try {
    const d = await fetch("/api/crew").then(r => r.json());
    const sel = $("#crewWho");
    sel.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = ""; auto.textContent = "Auto-route";
    sel.appendChild(auto);
    const eng = $("#crewEngine");
    if (eng && !eng.children.length) {
      const t = await fetch("/api/trends").then(r => r.json()).catch(() => ({}));
      const o0 = document.createElement("option");
      o0.value = ""; o0.textContent = "Member's engine";
      eng.appendChild(o0);
      (t.engines || []).forEach(n => {
        const o = document.createElement("option");
        o.value = n; o.textContent = n; eng.appendChild(o);
      });
    }
    list.innerHTML = "";
    (d.members || []).forEach(mem => {
      const o = document.createElement("option");
      o.value = mem.name; o.textContent = mem.name;
      sel.appendChild(o);
      const row = el("div", "log-row");
      row.style.flexDirection = "column"; row.style.alignItems = "stretch";
      // the rail states the real cost tier: local work is bronze, cloud is gold
      const eng = (mem.engine || "Auto");
      row.classList.add("tier",
        eng === "Auto" ? "tier-silver"
          : /claude|gpt|deepseek|sonnet|opus/i.test(eng) ? "tier-gold"
          : "tier-bronze");
      const head = el("div"); head.style.fontWeight = "600";
      head.textContent = `${mem.name} — ${mem.role || ""}`;
      const meta = el("div", "log-meta");
      meta.textContent = `engine ${mem.engine || "Auto"} · `
        + (mem.schedule ? `runs ${mem.schedule.kind} at ${mem.schedule.time}`
                        : "no schedule");
      const brief = el("div", "log-meta");
      brief.textContent = (mem.brief || "").slice(0, 180) + "…";
      const act = el("div", "mcp-actions"); act.style.marginTop = "6px";
      const sch = el("button", "btn ghost btn-mini");
      sch.textContent = mem.schedule ? "Clear schedule" : "Run weekly";
      sch.addEventListener("click", async () => {
        sch.disabled = true;
        try {
          await fetch("/api/crew/schedule", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(mem.schedule
              ? { member: mem.name, kind: "" }
              : { member: mem.name, kind: "weekly", time: "07:30", dow: 1,
                  task: `Run your standing brief for ${mem.name}.` }) });
          loadCrew();
        } catch (e) { $("#crewStatus").textContent = "Schedule change failed."; }
      });
      act.appendChild(sch);
      row.append(head, meta, brief, act);
      list.appendChild(row);
    });
    if (d.runs && d.runs.length) {
      const h = el("div", "log-meta");
      h.style.marginTop = "8px"; h.textContent = "Recent runs";
      list.appendChild(h);
      d.runs.slice(0, 8).forEach(r => {
        const row = el("div", "log-row");
        const meta = el("span", "log-meta");
        meta.textContent = `${r.iso} · ${r.member} · `
          + (r.ok ? "ok" : "FAILED") + ` · ${r.task}`;
        const b = el("button", "btn ghost btn-mini");
        b.textContent = "Report";
        b.addEventListener("click", () => {
          $("#crewReport").textContent = r.report || "(no report)";
        });
        row.append(meta, b);
        list.appendChild(row);
      });
    }
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
function crewEngine() {
  const s = $("#crewEngine");
  return s ? s.value : "";
}
async function runCrew() {
  const task = $("#crewTask").value.trim();
  if (!task) { $("#crewStatus").textContent = "Type a task first."; return; }
  const btn = $("#crewRunBtn");
  btn.disabled = true; btn.textContent = "Working…";
  $("#crewStatus").textContent = "The specialist is working — this can take a few minutes.";
  try {
    const chain = $("#crewChain") ? $("#crewChain").value : "";
    const r = chain
      ? await fetch("/api/crew/chain", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chain, task, engine: crewEngine() }) })
      : await fetch("/api/crew/run", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task, member: $("#crewWho").value,
                                 engine: crewEngine() }) });
    const d = await r.json();
    if (!r.ok) { $("#crewStatus").textContent = errText(d, "Run failed."); }
    else {
      if (d.steps) {
        $("#crewStatus").textContent = d.ok
          ? `Chain '${d.chain}' completed ${d.steps.length} handoff(s).`
          : `Chain stopped at step ${d.stopped_at}. ${d.why || ""}`;
        $("#crewReport").textContent = (d.steps || [])
          .map(s => `--- ${s.member} (step ${s.step}) ---\n${s.report}`)
          .join("\n\n");
      } else {
        $("#crewStatus").textContent =
          `${d.member} finished in ${d.seconds}s · workspace: ${d.workspace}`;
        $("#crewReport").textContent = d.report || "";
      }
      loadCrew();
    }
  } catch (e) { $("#crewStatus").textContent = "Run failed."; }
  btn.disabled = false; btn.textContent = "Run";
}
/* -------------------------------- trends -------------------------------- */
function openTrends() {
  const m = $("#trendsModal");
  m.hidden = false; m.style.display = "flex";
  $("#trendStatus").textContent = "";
  loadTrends();
}
function closeTrends() {
  const m = $("#trendsModal");
  m.hidden = true; m.style.display = "none";
}
function renderTrendReport(rep) {
  const list = $("#trendList"); list.innerHTML = "";
  const partial = rep.items_total && rep.items_done < rep.items_total;
  $("#trendsInfo").textContent = rep.at
    ? `Last scan ${rep.at} · ${(rep.trends || []).length} trend(s)`
      + (rep.items_total ? ` · ${rep.items_done}/${rep.items_total} items` : "")
    : "No scan yet — run one.";
  $("#trendResumeBtn").hidden = !partial;
  if (rep.error) {
    $("#trendStatus").textContent = rep.error
      + (partial ? " (progress saved — Resume continues from item "
                   + (rep.items_done + 1) + ")" : "");
  }
  if (!(rep.trends || []).length) {
    list.innerHTML = "<div class='out-empty'>Nothing here yet. Scan now, or enable the weekly auto-scan.</div>";
    return;
  }
  rep.trends.forEach((t, i) => {
    const card = el("div", "log-row");
    card.style.flexDirection = "column";
    card.style.alignItems = "stretch";
    const head = el("div"); head.style.fontWeight = "600";
    head.textContent = t.title;
    const why = el("div", "log-meta"); why.textContent = t.why || "";
    const srcs = el("div", "log-meta");
    (t.sources || []).slice(0, 3).forEach(u => {
      const a = el("a"); a.href = u; a.target = "_blank"; a.rel = "noopener";
      a.textContent = (() => { try { return new URL(u).hostname; } catch (e) { return u; } })();
      a.style.marginRight = "10px";
      srcs.appendChild(a);
    });
    const act = el("div", "mcp-actions"); act.style.marginTop = "6px";
    const ln = t.learnable || {};
    if (ln.kind === "skill") {
      const desc = el("div", "log-meta");
      desc.textContent = `Skill: ${ln.name} — ${ln.description || ""}`;
      card.appendChild(desc);
      const b = el("button", "btn primary btn-mini");
      b.textContent = t.adopted ? "Adopted ✓" : "Adopt skill";
      b.disabled = !!t.adopted;
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const r = await fetch("/api/trends/adopt", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ index: i }) }).then(r => r.json());
          $("#trendStatus").textContent = r.skill
            ? `Adopted skill “${r.skill}” — the agent can use it now.`
            : (r.detail || "Adopt failed.");
          if (r.report) renderTrendReport(r.report);
        } catch (e) { $("#trendStatus").textContent = "Adopt failed."; }
      });
      act.appendChild(b);
    } else if (ln.kind === "build") {
      const desc = el("div", "log-meta");
      desc.textContent = `Build request: ${ln.request || ""}`;
      card.appendChild(desc);
      const b = el("button", "btn ghost btn-mini");
      b.textContent = "Use in chat";
      b.addEventListener("click", () => {
        const inp = $("#input");
        if (inp) { inp.value = ln.request || ""; inp.focus(); }
        closeTrends();
      });
      act.appendChild(b);
    }
    card.append(head, why, srcs, act);
    list.appendChild(card);
  });
}
async function loadTrends() {
  try {
    const d = await fetch("/api/trends").then(r => r.json());
    $("#trendWeekly").checked = !!d.weekly;
    const sel = $("#trendEngine");
    if (sel) {
      sel.innerHTML = "";
      (d.engines || ["Auto"]).forEach(name => {
        const o = document.createElement("option");
        o.value = name; o.textContent = name;
        if (name === d.engine) o.selected = true;
        sel.appendChild(o);
      });
    }
    renderTrendReport(d.report || {});
  } catch (e) { $("#trendList").innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function scanTrends() {
  const b = $("#trendScanBtn");
  b.disabled = true; b.textContent = "Scanning…";
  $("#trendStatus").textContent = "Fetching sources and digesting — this can take a minute.";
  try {
    const sel = $("#trendEngine");
    const r = await fetch("/api/trends/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine: sel ? sel.value : "" }) });
    const d = await r.json();
    if (!r.ok) { $("#trendStatus").textContent = errText(d, "Scan failed."); }
    else { $("#trendStatus").textContent = ""; renderTrendReport(d.report || {}); }
  } catch (e) { $("#trendStatus").textContent = "Scan failed — is the server reachable?"; }
  b.disabled = false; b.textContent = "Scan now";
}
/* ----------------------------- self-improve ----------------------------- */
function openSelf() {
  const m = $("#selfModal");
  m.hidden = false; m.style.display = "flex";
  $("#selfDiff").textContent = ""; $("#selfNote").textContent = "";
  loadSelf();
}
function closeSelf() {
  const m = $("#selfModal");
  m.hidden = true; m.style.display = "none";
}
function selfTab(which) {
  const map = { proposal: "Proposal", ideas: "Ideas", history: "History" };
  Object.keys(map).forEach(k => {
    const b = $("#selfTab" + map[k]), p = $("#selfPane" + map[k]);
    if (b) b.classList.toggle("is-on", k === which);
    if (p) p.hidden = k !== which;
  });
  if (which === "ideas") loadSelfIdeas();
  if (which === "history") loadSelfHistory();
}

async function loadSelf() {
  try {
    // the route is /api/selfimprove — the panel rewrite renamed the caller
    // and not the endpoint, so the whole panel 404'd on open
    const p = await fetch("/api/selfimprove").then(r => r.json());
    const files = $("#selfFiles"), diff = $("#selfDiff");
    const state = p.state || "none";
    $("#selfStatusLine").textContent = {
      none: "Nothing proposed",
      building: "Building…",
      testing: "Running the test suite…",
      proposed: p.tests_ok ? "Tested and waiting for you" : "Tests failed",
      applied: "Applied — restart to activate",
    }[state] || state;
    const t = p.tests || {};
    $("#selfTests").textContent = t.passed
      ? `${t.passed} checks passed` + (t.failed ? ` · ${t.failed} failed` : "")
      : "";
    $("#selfApplyBtn").disabled = !(state === "proposed" && p.tests_ok);
    $("#selfDiscardBtn").disabled = state === "none";
    $("#selfNote").textContent = p.summary || "";
    files.innerHTML = ""; diff.textContent = "";
    const ds = p.diffs || [];
    if (!ds.length) {
      emptyPane(files, "Nothing proposed",
        "Ask in chat: “add X to yourself”. It works in a sandboxed copy and "
        + "must pass the whole test suite before you see a diff.");
      return;
    }
    ds.forEach((f, i) => {
      const row = el("button", "job-row");
      row.type = "button";
      const t2 = el("div", "job-row-title"); t2.textContent = f.path;
      const m2 = el("div", "job-row-meta");
      m2.textContent = f.status + (f.lines ? ` · ${f.lines} line(s)` : "");
      row.append(t2, m2);
      row.addEventListener("click", () => {
        Array.from(files.children).forEach(c => c.classList
          && c.classList.remove("is-on"));
        row.classList.add("is-on");
        diff.textContent = f.diff || "(no textual diff)";
      });
      files.appendChild(row);
      if (i === 0) { row.classList.add("is-on"); diff.textContent = f.diff || ""; }
    });
  } catch (e) { $("#selfStatusLine").textContent = "Could not load."; }
}

async function loadSelfIdeas() {
  const list = $("#selfIdeaList"), detail = $("#selfIdeaDetail");
  list.innerHTML = "";
  emptyPane(detail, "", "Pick something to see the evidence.");
  try {
    const d = await fetch("/api/self/suggestions").then(r => r.json());
    const s = d.suggestions || [];
    if (!s.length) {
      emptyPane(list, "Nothing obvious to fix",
        "No repeating errors, no failing health checks, no tripped breakers. "
        + "You can still ask for anything directly in chat.");
      return;
    }
    s.forEach(x => {
      const row = el("button", "job-row"); row.type = "button";
      const t = el("div", "job-row-title"); t.textContent = x.title;
      const m = el("div", "job-row-meta"); m.textContent = x.why;
      row.append(t, m);
      row.addEventListener("click", () => {
        Array.from(list.children).forEach(c => c.classList
          && c.classList.remove("is-on"));
        row.classList.add("is-on");
        detail.innerHTML = "";
        const h = el("div", "detail-head"); h.textContent = x.title;
        const sub = el("div", "detail-sub"); sub.textContent = x.why;
        detail.append(h, sub);
        const acts = el("div", "detail-actions");
        const go = el("button", "btn primary btn-mini");
        go.textContent = "Ask it to build this";
        go.addEventListener("click", () => {
          closeSelf();
          send("Build this into yourself: " + x.request);
        });
        acts.appendChild(go);
        detail.appendChild(acts);
        const b = el("div", "detail-block");
        const bh = el("h4"); bh.textContent = "What it will be asked";
        const pre = el("pre"); pre.textContent = x.request;
        b.append(bh, pre); detail.appendChild(b);
      });
      list.appendChild(row);
    });
  } catch (e) { emptyPane(list, "Could not load", "Try again."); }
}

async function loadSelfHistory() {
  const list = $("#selfHistList"), detail = $("#selfHistDetail");
  list.innerHTML = "";
  emptyPane(detail, "", "Pick an improvement to see what changed.");
  try {
    const d = await fetch("/api/self/history").then(r => r.json());
    const h = d.history || [];
    if (!h.length) {
      emptyPane(list, "Nothing applied yet",
        "Improvements you approve will be listed here, and can be rolled back.");
      return;
    }
    h.forEach(x => {
      const row = el("button", "job-row"); row.type = "button";
      const t = el("div", "job-row-title");
      t.textContent = x.summary || x.request || x.id;
      const m = el("div", "job-row-meta");
      m.textContent = [x.at, `${(x.files || []).length} file(s)`,
        x.reverted_at ? "reverted" : null].filter(Boolean).join(" · ");
      row.append(t, m);
      row.addEventListener("click", () => {
        Array.from(list.children).forEach(c => c.classList
          && c.classList.remove("is-on"));
        row.classList.add("is-on");
        detail.innerHTML = "";
        const hh = el("div", "detail-head");
        hh.textContent = x.summary || x.id;
        const sub = el("div", "detail-sub");
        sub.textContent = [x.at, x.reverted_at ? "rolled back " + x.reverted_at : null]
          .filter(Boolean).join(" · ");
        detail.append(hh, sub);
        if (!x.reverted_at) {
          const acts = el("div", "detail-actions");
          const rb = el("button", "btn ghost btn-mini");
          rb.textContent = "Roll this back";
          rb.addEventListener("click", async () => {
            rb.disabled = true;
            try {
              const r = await fetch("/api/self/revert", { method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: x.id }) });
              const dd = await r.json();
              $("#selfStatusLine").textContent = r.ok
                ? `Rolled back ${(dd.restored || []).length} file(s) — restart to activate.`
                : errText(dd, "Could not roll that back.");
              loadSelfHistory();
            } catch (e) { $("#selfStatusLine").textContent = "Could not roll that back."; }
            rb.disabled = false;
          });
          acts.appendChild(rb);
          detail.appendChild(acts);
        }
        const b = el("div", "detail-block");
        const bh = el("h4"); bh.textContent = "Files changed";
        const pre = el("pre"); pre.textContent = (x.files || []).join("\n");
        b.append(bh, pre); detail.appendChild(b);
        if (x.request) {
          const b2 = el("div", "detail-block");
          const bh2 = el("h4"); bh2.textContent = "What was asked";
          const p2 = el("pre"); p2.textContent = x.request;
          b2.append(bh2, p2); detail.appendChild(b2);
        }
      });
      list.appendChild(row);
    });
  } catch (e) { emptyPane(list, "Could not load", "Try again."); }
}
async function applySelf() {
  $("#selfApplyBtn").disabled = true;
  try {
    const r = await fetch("/api/selfimprove/apply", { method: "POST" }).then(r => r.json());
    $("#selfNote").textContent = r.note || r.detail || "";
    loadSelf();
  } catch (e) { $("#selfNote").textContent = "Apply failed."; }
}
async function discardSelf() {
  await fetch("/api/selfimprove/discard", { method: "POST" });
  $("#selfNote").textContent = "Discarded.";
  loadSelf();
}
/* ---------------------------- time machine ------------------------------ */
function openUndo() {
  const m = $("#undoModal");
  m.hidden = false; m.style.display = "flex";
  $("#undoDiff").textContent = ""; $("#undoStatus").textContent = "";
  loadUndo();
}
function closeUndo() {
  const m = $("#undoModal");
  m.hidden = true; m.style.display = "none";
}
async function loadUndo() {
  const list = $("#undoList"); list.innerHTML = "<div class='out-empty'>Loading…</div>";
  try {
    const d = await fetch("/api/timemachine").then(r => r.json());
    $("#undoCount").textContent = `${(d.entries || []).length} change(s) · store ${(d.store_bytes / 1048576).toFixed(1)} MB` + (d.enabled ? "" : " · OFF");
    list.innerHTML = "";
    if (!(d.entries || []).length) {
      list.innerHTML = "<div class='out-empty'>No file changes recorded yet. When the agent writes a file, the previous version lands here.</div>";
      return;
    }
    d.entries.forEach(e => {
      const row = el("div", "log-row");
      const meta = el("span", "log-meta");
      meta.textContent = `${e.iso} · ${e.action} · ${e.path}` + (e.note ? ` · ${e.note}` : "");
      const wrap = el("span", "mcp-actions");
      const df = el("button", "btn ghost btn-mini"); df.textContent = "Diff";
      df.addEventListener("click", async () => {
        const r = await fetch(`/api/timemachine/${e.id}/diff`).then(r => r.json());
        $("#undoDiff").textContent = r.ok ? r.text : ("Diff unavailable: " + (r.error || ""));
      });
      const rs = el("button", "btn ghost btn-mini"); rs.textContent = e.action === "create" ? "Undo create" : "Restore";
      rs.disabled = !e.restorable;
      rs.addEventListener("click", async () => {
        rs.disabled = true;
        try {
          const r = await fetch(`/api/timemachine/${e.id}/restore`, { method: "POST" }).then(r => r.json());
          $("#undoStatus").textContent = r.result || r.detail || "";
          loadUndo();
        } catch (err) { $("#undoStatus").textContent = "Restore failed."; }
      });
      wrap.append(df, rs); row.append(meta, wrap); list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
/* ------------------------------- audit trail ---------------------------- */
function openAudit() {
  const m = $("#auditModal");
  m.hidden = false; m.style.display = "flex";
  loadAudit();
}
function closeAudit() {
  const m = $("#auditModal");
  m.hidden = true; m.style.display = "none";
}
function _auditLine(e) {
  const bits = [e.iso, e.kind];
  ["engine", "name", "action", "status", "summary", "detail"].forEach(k => {
    if (e[k] !== undefined && e[k] !== "") bits.push(`${k}=${e[k]}`);
  });
  if (e.ok !== undefined) bits.push(e.ok ? "ok" : "FAILED");
  return bits.join(" · ");
}
async function loadAuditArchives() {
  const host = $("#auditArchives");
  if (!host) return;
  host.innerHTML = "";
  try {
    const d = await fetch("/api/audit/archives").then(r => r.json());
    const a = d.archives || [];
    if (!a.length) {
      const p = el("div", "log-meta");
      p.textContent = "No archived trails on disk.";
      host.appendChild(p);
      return;
    }
    const head = el("div", "log-meta");
    const kb = a.reduce((n, x) => n + x.bytes, 0) / 1024;
    head.textContent = `${a.length} archived trail(s), ${kb.toFixed(0)} KB — kept, not deleted.`;
    host.appendChild(head);
    a.forEach(x => {
      const row = el("div", "dash-edit-row");
      const nm = el("span");
      nm.textContent = `${x.name} · ${(x.bytes / 1024).toFixed(0)} KB · ${x.modified}`;
      row.appendChild(nm);
      host.appendChild(row);
    });
    const del = el("button", "btn ghost btn-mini");
    del.style.marginTop = "6px";
    del.textContent = `Delete all ${a.length} archive(s) permanently`;
    del.addEventListener("click", async () => {
      if (!confirm("Delete every archived trail permanently? The current "
                   + "trail is not affected, and the deletion is recorded.")) return;
      try {
        const r = await fetch("/api/audit/archives/delete", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ names: [] }) });
        const dd = await r.json();
        $("#auditStatus").textContent = r.ok
          ? `Deleted ${dd.deleted.length} archive(s), freed ${(dd.bytes_freed / 1024).toFixed(0)} KB.`
          : errText(dd, "Could not delete those.");
        loadAuditArchives(); loadAudit();
      } catch (e) { $("#auditStatus").textContent = "Could not delete those."; }
    });
    host.appendChild(del);
  } catch (e) { /* the panel still works without this */ }
}

async function auditClear(keepDays) {
  const what = keepDays > 0
    ? `Clear entries older than ${keepDays} day(s)?`
    : "Clear the entire audit trail?";
  if (!confirm(what + "\n\nThe old trail is archived, not destroyed, and "
               + "the clearing is recorded in the new one.")) return;
  try {
    const r = await fetch("/api/audit/clear", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_days: keepDays, archive: true }) });
    const d = await r.json();
    $("#auditStatus").textContent = r.ok
      ? `Cleared ${d.removed} entry(s)${d.kept ? `, kept ${d.kept}` : ""} — archived as ${d.archived_as}.`
      : errText(d, "Could not clear the trail.");
    loadAudit(); loadAuditArchives();
  } catch (e) { $("#auditStatus").textContent = "Could not clear the trail."; }
}

async function loadAudit() {
  const list = $("#auditList"); list.innerHTML = "<div class='out-empty'>Loading…</div>";
  try {
    const kind = $("#auditFilter").value;
    const d = await fetch("/api/audit?limit=150&kind=" + encodeURIComponent(kind)).then(r => r.json());
    $("#auditCount").textContent = `${d.count} entries · chain ${d.chain && d.chain.ok ? "✓ intact" : "⚠ BROKEN"}` + (d.enabled ? "" : " · auditing OFF");
    list.innerHTML = "";
    if (!(d.entries || []).length) {
      list.innerHTML = "<div class='out-empty'>Nothing recorded yet for this filter.</div>";
      return;
    }
    d.entries.forEach(e => {
      const row = el("div", "log-row");
      const meta = el("span", "log-meta");
      meta.textContent = _auditLine(e);
      row.appendChild(meta); list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function verifyAudit() {
  try {
    const v = await fetch("/api/audit/verify", { method: "POST" }).then(r => r.json());
    $("#auditStatus").textContent = v.ok
      ? `Chain verified: ${v.entries} entries, no tampering detected.`
      : `Chain BROKEN at line ${v.break_at} (${v.reason || "mismatch"}).`;
  } catch (e) { $("#auditStatus").textContent = "Verify failed."; }
}
async function copyAudit(fmt) {
  try {
    const d = await fetch("/api/audit/export?fmt=" + fmt).then(r => r.json());
    const ok = await copyToClipboard(d.text);
    $("#auditStatus").textContent = ok ? `Copied ${d.count} entries as ${fmt}.` : "Copy failed.";
  } catch (e) { $("#auditStatus").textContent = "Export failed."; }
}
/* ------------------------------- MCP servers ---------------------------- */
function openMcp() {
  const m = $("#mcpModal");
  m.hidden = false; m.style.display = "flex";
  loadMcp();
}
function closeMcp() {
  const m = $("#mcpModal");
  m.hidden = true; m.style.display = "none";
}
function mcpTransportSync() {
  const http = $("#mcpTransport").value === "http";
  $("#mcpCommand").hidden = http; $("#mcpEnv").hidden = http;
  $("#mcpUrl").hidden = !http; $("#mcpHeaders").hidden = !http;
}
function renderMcpServers(servers) {
  const list = $("#mcpList"); list.innerHTML = "";
  $("#mcpCount").textContent = servers.length
    ? servers.length + " server(s)"
    : "No servers yet. Try: npx -y @modelcontextprotocol/server-filesystem C:\\Users";
  servers.forEach(s => {
    const row = el("div", "log-row");
    const meta = el("span", "log-meta");
    const dot = s.connected ? "●" : (s.enabled ? "○" : "◌");
    meta.textContent = `${dot} ${s.name} · ${s.transport} · ` +
      (s.connected ? `${s.tools} tool(s)` : (s.enabled ? (s.error ? "error" : "not connected") : "disabled"));
    meta.title = s.target + (s.error ? "\n" + s.error : "");
    const wrap = el("span", "mcp-actions");
    if (s.enabled && !s.connected) {
      const retry = el("button", "btn ghost btn-mini"); retry.textContent = "Retry";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        const r = await fetch(`/api/mcp/${encodeURIComponent(s.name)}/connect`, { method: "POST" }).then(r => r.json());
        setOut("mcpStatus", r.ok ? `Connected — ${r.tools} tool(s).` : ("Failed: " + (r.error || "unknown")).slice(0, 160), r.ok ? "ok" : "err");
        renderMcpServers(r.servers || []);
      });
      wrap.appendChild(retry);
    }
    const tog = el("button", "btn ghost btn-mini"); tog.textContent = s.enabled ? "Disable" : "Enable";
    tog.addEventListener("click", async () => {
      const r = await fetch(`/api/mcp/${encodeURIComponent(s.name)}/toggle`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !s.enabled }),
      }).then(r => r.json());
      renderMcpServers(r.servers || []); refreshStats();
    });
    const rm = el("button", "btn ghost btn-mini"); rm.textContent = "Remove";
    rm.addEventListener("click", async () => {
      const r = await fetch(`/api/mcp/${encodeURIComponent(s.name)}`, { method: "DELETE" }).then(r => r.json());
      renderMcpServers(r.servers || []); refreshStats();
    });
    wrap.append(tog, rm);
    row.append(meta, wrap); list.appendChild(row);
  });
}
async function loadMcp() {
  try {
    const d = await fetch("/api/mcp").then(r => r.json());
    renderMcpServers(d.servers || []);
  } catch (e) { $("#mcpList").innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function addMcpServer() {
  const body = {
    name: $("#mcpName").value.trim(),
    transport: $("#mcpTransport").value,
    command: $("#mcpCommand").value.trim(),
    url: $("#mcpUrl").value.trim(),
    env: $("#mcpEnv").value,
    headers: $("#mcpHeaders").value,
    enabled: true,
  };
  if (!body.name) { setOut("mcpStatus", "Give the server a name.", "err"); return; }
  setOut("mcpStatus", "Adding and connecting…", "");
  try {
    const r = await fetch("/api/mcp", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (r.detail) { setOut("mcpStatus", r.detail, "err"); return; }
    setOut("mcpStatus", r.connected
      ? `Connected — ${r.tools} tool(s) available.`
      : ("Saved, but not connected: " + (r.error || "unknown")).slice(0, 180),
      r.connected ? "ok" : "err");
    if (r.connected) { $("#mcpName").value = ""; $("#mcpCommand").value = ""; $("#mcpUrl").value = ""; }
    renderMcpServers(r.servers || []); refreshStats();
  } catch (e) { setOut("mcpStatus", "Could not add the server.", "err"); }
}
function openIssues() {
  const m = $("#issuesModal");
  m.hidden = false; m.style.display = "flex";
  loadIssues();
}
function closeIssues() {
  const m = $("#issuesModal");
  m.hidden = true; m.style.display = "none";
}
async function loadIssues() {
  const list = $("#issuesList"); list.innerHTML = "<div class='out-empty'>Loading…</div>";
  const errList = $("#autoErrList"); errList.innerHTML = "";
  try {
    const d = await fetch("/api/issues").then(r => r.json());
    // auto-captured errors (newest first) with one-click promotion to a report
    if (!(d.errors || []).length) {
      errList.innerHTML = "<div class='out-empty'>No errors picked up. If a button misbehaves, it should appear here on its own.</div>";
    }
    (d.errors || []).forEach(e => {
      const row = el("div", "log-row");
      const meta = el("span", "log-meta");
      meta.textContent = `${e.iso} · ${e.source} · ${e.message}`;
      const rp = el("button", "btn ghost btn-mini"); rp.textContent = "Report";
      rp.title = "File a full report for this error (bundles conversation + context)";
      rp.addEventListener("click", async () => {
        rp.disabled = true;
        await captureIssueWithNote("Auto-captured error: " + e.message);
        rp.textContent = "Reported ✓";
      });
      row.append(meta, rp); errList.appendChild(row);
    });
    $("#issuesCount").textContent = d.count ? d.count + " report(s)" : "No reports yet.";
    list.innerHTML = "";
    if (!(d.issues || []).length) {
      list.innerHTML = "<div class='out-empty'>Nothing recorded. When something misbehaves, describe it above and capture it.</div>";
      return;
    }
    d.issues.forEach(e => {
      const row = el("div", "log-row");
      const meta = el("span", "log-meta");
      meta.textContent = `${e.iso} · ${e.engine || "?"} · ${e.note}`;
      const cp = el("button", "btn ghost btn-mini"); cp.textContent = "Copy";
      cp.addEventListener("click", async () => {
        cp.textContent = (await copyToClipboard(e.text)) ? "Copied ✓" : "Copy failed";
        setTimeout(() => cp.textContent = "Copy", 1500);
      });
      row.append(meta, cp); list.appendChild(row);
    });
  } catch (e) { list.innerHTML = "<div class='out-empty'>Could not load.</div>"; }
}
async function captureIssueWithNote(note) {
  try {
    const r = await fetch("/api/issues", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note, conversation_id: state.activeId || "", engine: state.engine || "" }),
    }).then(r => r.json());
    if (r.ok) {
      setOut("issueStatus", "Captured — report #" + r.count + " saved locally.", "ok");
      showToast("Issue report captured");
      loadIssues();
    } else { setOut("issueStatus", r.detail || "Capture failed.", "err"); }
  } catch (e) { setOut("issueStatus", "Capture failed.", "err"); }
}
async function captureIssue() {
  const note = $("#issueNote").value.trim();
  if (!note) { setOut("issueStatus", "Describe what went wrong first.", "err"); return; }
  setOut("issueStatus", "Capturing…", "");
  $("#issueNote").value = "";
  await captureIssueWithNote(note);
}
async function clearAutoErrors() {
  try {
    await fetch("/api/issues/clear-errors", { method: "POST" });
    loadIssues();
  } catch (e) {}
}
async function copyAllIssues() {
  try {
    const d = await fetch("/api/issues/export").then(r => r.json());
    if (!d.count) { setOut("issueStatus", "Nothing to copy yet.", ""); return; }
    const ok = await copyToClipboard(d.text);
    setOut("issueStatus", ok ? "All " + d.count + " report(s) copied — paste to the developer." : "Copy failed.", ok ? "ok" : "err");
  } catch (e) { setOut("issueStatus", "Export failed.", "err"); }
}
async function clearIssues() {
  try {
    const r = await fetch("/api/issues/clear", { method: "POST" }).then(r => r.json());
    setOut("issueStatus", "Cleared " + (r.cleared || 0) + " report(s).", "");
    loadIssues();
  } catch (e) { setOut("issueStatus", "Clear failed.", "err"); }
}
async function pauseAllAutonomy() {
  setOut("autoMasterStatus", "Pausing everything…", "");
  try {
    const r = await fetch("/api/autonomy/pause-all", { method: "POST" }).then(r => r.json());
    const p = r.paused || {};
    setOut("autoMasterStatus",
      `Paused: auto-pilot ${p.autopilot ? "✓" : "–"}, auto-resume ${p.autoresume ? "✓" : "–"}, ${p.schedules || 0} schedule(s) disabled.`, "ok");
    showToast("All autonomy paused");
    await loadAutonomy();
  } catch (e) { setOut("autoMasterStatus", "Pause failed.", "err"); }
}
async function loadEmailLog() {
  const box = $("#emLog"); box.innerHTML = "<div class='out-empty'>Loading…</div>";
  try {
    const r = await fetch("/api/email/log").then(r => r.json());
    box.innerHTML = "";
    if (!(r.log || []).length) { box.innerHTML = "<div class='out-empty'>No activity yet.</div>"; return; }
    r.log.forEach(e => {
      const row = el("div", "log-row");
      const when = new Date((e.ts || 0) * 1000).toLocaleString();
      const st = el("span", "log-status s-" + (e.status || "")); st.textContent = e.status || "?";
      const meta = el("span", "log-meta");
      meta.textContent = `${when} · ${e.to || ""} · ${e.subject || ""}`;
      row.append(st, meta); box.appendChild(row);
    });
  } catch (e) { box.innerHTML = "<div class='out-empty'>Could not load activity.</div>"; }
}
function _modelControl(key, label, desc, choices, current, freeText) {
  const row = el("div", "set-row");
  const info = el("div", "set-info");
  const nm = el("div", "set-name"); nm.textContent = label;
  const ds = el("div", "set-desc"); ds.textContent = desc;
  info.append(nm, ds);
  const ctrl = el("div", "set-control");
  if (choices && choices.length && !freeText) {
    const sel = el("select"); sel.id = "model_" + key; sel.className = "model-select";
    const opts = choices.slice();
    if (current && !opts.includes(current)) opts.unshift(current);
    opts.forEach(o => { const op = el("option"); op.value = o; op.textContent = o; sel.appendChild(op); });
    sel.value = current || opts[0] || "";
    ctrl.appendChild(sel);
  } else {
    const inp = el("input"); inp.type = "text"; inp.id = "model_" + key;
    inp.className = "model-input"; inp.value = current || "";
    inp.placeholder = key.startsWith("OLLAMA") ? "e.g. qwen3:8b" : "e.g. claude-sonnet-4-6";
    ctrl.appendChild(inp);
  }
  row.append(info, ctrl);
  return row;
}
async function loadModelPickers() {
  const box = $("#modelsSection");
  box.innerHTML = "<div class='settings-group-label first'>Engine &amp; models</div>";
  const engRow = el("div", "set-row");
  const engInfo = el("div", "set-info");
  const engNm = el("div", "set-name"); engNm.textContent = "Default engine";
  const engDs = el("div", "set-desc");
  engDs.textContent = "The engine the app starts on and remembers. Auto routes across engines; pick a specific one to always start there.";
  engInfo.append(engNm, engDs);
  const engCtrl = el("div", "set-control");
  const engSel = el("select"); engSel.id = "model_DEFAULT_ENGINE"; engSel.className = "model-select";
  (state.engines || []).forEach(e => {
    const op = el("option"); op.value = e.id; op.textContent = e.label; engSel.appendChild(op);
  });
  engSel.value = state.defaultEngine || "Auto";
  engCtrl.appendChild(engSel);
  engRow.append(engInfo, engCtrl);
  box.appendChild(engRow);
  const pvRow = el("div", "set-row");
  const pvInfo = el("div", "set-info");
  const pvNm = el("div", "set-name"); pvNm.textContent = "Privacy shield";
  const pvDs = el("div", "set-desc");
  pvDs.textContent = "off: unchanged. mask: IDs, cards, emails, phones and secrets are replaced with placeholders before any engine sees them, and restored in replies and tools. local: turns containing sensitive data run entirely on the local model (masks if Ollama is off).";
  pvInfo.append(pvNm, pvDs);
  const pvCtrl = el("div", "set-control");
  const pvSel = el("select"); pvSel.id = "model_PRIVACY_MODE"; pvSel.className = "model-select";
  ["off","mask","local"].forEach(o => { const op = el("option"); op.value = o; op.textContent = o; pvSel.appendChild(op); });
  pvCtrl.appendChild(pvSel);
  pvRow.append(pvInfo, pvCtrl);
  box.appendChild(pvRow);
  const tbRow = el("div", "set-row");
  const tbInfo = el("div", "set-info");
  const tbNm = el("div", "set-name"); tbNm.textContent = "Turbo (local first)";
  const tbDs = el("div", "set-desc");
  tbDs.textContent = "Let the local model answer first; only escalate to the cloud engine when the answer fails automatic checks (empty, refused, truncated, repeating, bad JSON). Needs both a local and a cloud engine. Watch the hit rate on the dashboard — if most turns escalate, this costs more than it saves.";
  tbInfo.append(tbNm, tbDs);
  const tbCtrl = el("div", "set-control");
  const tbSel = el("select"); tbSel.id = "model_TURBO"; tbSel.className = "model-select";
  [["false", "off"], ["true", "on"]].forEach(([v, l]) => {
    const o = el("option"); o.value = v; o.textContent = l; tbSel.appendChild(o);
  });
  tbCtrl.appendChild(tbSel);
  tbRow.append(tbInfo, tbCtrl);
  box.appendChild(tbRow);
  fetch("/api/settings").then(r => r.json())
    .then(d => { tbSel.value = String(!!(d.settings || {}).TURBO); })
    .catch(() => {});

  const thRow = el("div", "set-row");
  const thInfo = el("div", "set-info");
  const thNm = el("div", "set-name"); thNm.textContent = "Theme";
  const thDs = el("div", "set-desc");
  thDs.textContent = "System follows Windows and switches with it. Light and Dark pin it. Instruments and Midnight are dark-only palettes.";
  thInfo.append(thNm, thDs);
  const thCtrl = el("div", "set-control");
  const thSel = el("select"); thSel.id = "model_THEME"; thSel.className = "model-select";
  Object.entries(THEMES).forEach(([value, t]) => {
    const op = el("option"); op.value = value; op.textContent = t.label;
    if (value === currentTheme()) op.selected = true;
    thSel.appendChild(op);
  });
  thSel.addEventListener("change", (e) => applyTheme(e.target.value));
  thCtrl.appendChild(thSel);
  thRow.append(thInfo, thCtrl);
  box.appendChild(thRow);
  const blRow = el("div", "set-row");
  const blInfo = el("div", "set-info");
  const blNm = el("div", "set-name"); blNm.textContent = "Blender path";
  const blDs = el("div", "set-desc"); blDs.textContent = "Full path to blender.exe for the 3D design tool (blank = auto-detect from PATH or standard install locations).";
  blInfo.append(blNm, blDs);
  const blCtrl = el("div", "set-control");
  const blIn = el("input"); blIn.type = "text"; blIn.id = "model_BLENDER_PATH"; blIn.className = "model-input"; blIn.placeholder = "auto-detect";
  blCtrl.appendChild(blIn);
  blRow.append(blInfo, blCtrl);
  box.appendChild(blRow);
  fetch("/api/settings").then(r=>r.json()).then(d => { blIn.value = (d.settings||{}).BLENDER_PATH || ""; }).catch(()=>{});
  const n3Row = el("div", "set-row");
  const n3Info = el("div", "set-info");
  const n3Nm = el("div", "set-name"); n3Nm.textContent = "Neural 3D command";
  const n3Ds = el("div", "set-desc"); n3Ds.textContent = "Command template for a locally-installed image-to-3D tool (TripoSR-class), with {image} and {out} placeholders. Blank = feature off; see README for an install recipe.";
  n3Info.append(n3Nm, n3Ds);
  const n3Ctrl = el("div", "set-control");
  const n3In = el("input"); n3In.type = "text"; n3In.id = "model_NEURAL3D_CMD"; n3In.className = "model-input"; n3In.placeholder = "not configured — click Detect";
  const n3Btns = el("div", "mcp-actions"); n3Btns.style.marginTop = "6px";
  const n3Det = el("button", "btn ghost btn-mini"); n3Det.textContent = "Detect";
  const n3Chk = el("button", "btn ghost btn-mini"); n3Chk.textContent = "Check";
  const n3Msg = el("div", "set-desc"); n3Msg.style.marginTop = "5px";
  n3Det.addEventListener("click", async () => {
    n3Det.disabled = true; n3Msg.textContent = "Looking for an installed tool…";
    try {
      const d = await fetch("/api/neural3d/detect").then(r => r.json());
      if (!(d.found || []).length) {
        n3Msg.textContent = "No image-to-3D tool found in the usual places. Install one (see README), or paste its command here.";
      } else {
        n3In.value = d.found[0].command;
        n3Msg.textContent = `Found ${d.found[0].tool} at ${d.found[0].path}`
          + (d.found[0].venv ? " (using its own venv)" : "")
          + ". Save settings to apply.";
      }
    } catch (e) { n3Msg.textContent = "Detect failed."; }
    n3Det.disabled = false;
  });
  n3Chk.addEventListener("click", async () => {
    n3Chk.disabled = true; n3Msg.textContent = "Checking…";
    try {
      const d = await fetch("/api/neural3d/check", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: n3In.value }) }).then(r => r.json());
      n3Msg.textContent = (d.ok ? "✓ " : "✕ ") + d.detail;
    } catch (e) { n3Msg.textContent = "Check failed."; }
    n3Chk.disabled = false;
  });
  n3Btns.append(n3Det, n3Chk);
  n3Ctrl.append(n3In, n3Btns, n3Msg);
  n3Row.append(n3Info, n3Ctrl);
  box.appendChild(n3Row);
  fetch("/api/settings").then(r=>r.json()).then(d => { n3In.value = (d.settings||{}).NEURAL3D_CMD || ""; }).catch(()=>{});
  fetch("/api/settings").then(r=>r.json()).then(d => { pvSel.value = (d.settings||{}).PRIVACY_MODE || "off"; }).catch(()=>{});
  let d;
  try { d = await fetch("/api/models").then(r => r.json()); }
  catch (e) { box.innerHTML += "<div class='set-desc'>Could not load model options.</div>"; return; }
  const cur = d.current || {};
  box.appendChild(_modelControl("MODEL", "Cloud model", "Main cloud model (Claude). Used for hard turns and as reviewer when a local model answers.", d.cloud, cur.MODEL, true));
  box.appendChild(_modelControl("FAST_MODEL", "Cloud fast model", "Cheaper cloud model for quick/triage calls.", d.cloud, cur.FAST_MODEL, true));
  const localNote = d.local_available
    ? "Chosen from models installed in your Ollama."
    : "Ollama isn't reachable — type the model id manually (start Ollama to get a dropdown).";
  box.appendChild(_modelControl("OLLAMA_MODEL", "Local model", "Main local model (Ollama). Used for cheap work and as reviewer when the cloud answers. " + localNote, d.local, cur.OLLAMA_MODEL, !d.local_available));
  box.appendChild(_modelControl("OLLAMA_FAST_MODEL", "Local fast model", "Optional smaller local model for triage; blank = same as local model.", d.local, cur.OLLAMA_FAST_MODEL, !d.local_available));
  if (!d.local_available) {
    const hint = el("div", "set-desc"); hint.style.marginTop = "2px";
    hint.textContent = "Tip: run `ollama list` to see your installed models.";
    box.appendChild(hint);
  }
}
function collectModelSettings() {
  const updates = {};
  ["MODEL", "FAST_MODEL", "OLLAMA_MODEL", "OLLAMA_FAST_MODEL"].forEach(k => {
    const inp = $("#model_" + k);
    if (inp) updates[k] = (inp.value || "").trim();
  });
  const eng = $("#model_DEFAULT_ENGINE");
  if (eng) updates.DEFAULT_ENGINE = eng.value;
  const pv = $("#model_PRIVACY_MODE");
  if (pv) updates.PRIVACY_MODE = pv.value;
  const bl = $("#model_BLENDER_PATH");
  if (bl) updates.BLENDER_PATH = bl.value.trim();
  const tb = $("#model_TURBO");
  if (tb) updates.TURBO = tb.value === "true";
  const n3 = $("#model_NEURAL3D_CMD");
  if (n3) updates.NEURAL3D_CMD = n3.value.trim();
  return updates;
}
function renderSettings(values) {
  const body = $("#settingsBody");
  body.innerHTML = "";
  SETTINGS_SCHEMA.forEach((group, gi) => {
    const gl = el("div", "settings-group-label" + (gi === 0 ? " first" : ""));
    gl.textContent = group.group; body.appendChild(gl);
    group.items.forEach(item => {
      const row = el("div", "set-row");
      const info = el("div", "set-info");
      const nm = el("div", "set-name"); nm.textContent = item.name;
      const ds = el("div", "set-desc"); ds.textContent = item.desc;
      info.append(nm, ds);
      const ctrl = el("div", "set-control");
      if (item.type === "bool") {
        const lab = el("label", "switch");
        const inp = el("input"); inp.type = "checkbox"; inp.id = "set_" + item.key;
        inp.checked = !!values[item.key];
        const track = el("span", "switch-track"); const dot = el("span", "switch-dot");
        track.appendChild(dot); lab.append(inp, track); ctrl.appendChild(lab);
      } else {
        const inp = el("input"); inp.type = "number"; inp.id = "set_" + item.key;
        inp.value = values[item.key]; inp.min = "1";
        ctrl.appendChild(inp);
      }
      row.append(info, ctrl); body.appendChild(row);
    });
  });
}
function collectSettings() {
  const updates = {};
  SETTINGS_SCHEMA.forEach(g => g.items.forEach(item => {
    const inp = $("#set_" + item.key);
    if (!inp) return;
    updates[item.key] = item.type === "bool" ? inp.checked : Number(inp.value);
  }));
  return updates;
}
async function saveSettings() {
  setSettingsStatus("Saving…", "");
  try {
    const updates = Object.assign(collectSettings(), collectModelSettings());
    const data = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    }).then(r => r.json());
    // reflect a new default engine on the live pill straight away
    if (updates.DEFAULT_ENGINE && updates.DEFAULT_ENGINE !== state.defaultEngine) {
      state.defaultEngine = updates.DEFAULT_ENGINE;
      const eng = (state.engines || []).find(e => e.id === updates.DEFAULT_ENGINE);
      if (eng) applyEngine(eng);
    }
    renderSettings(data.settings || {});
    loadModelPickers();
    setSettingsStatus(data.engine_reloaded ? "Saved — model switched." : "Saved.", "ok");
    showToast(data.engine_reloaded ? "Model switched" : "Settings saved.");
  } catch (e) { setSettingsStatus("Could not save settings.", "err"); }
}
async function resetSettings() {
  setSettingsStatus("Resetting…", "");
  try {
    const data = await fetch("/api/settings/reset", { method: "POST" }).then(r => r.json());
    renderSettings(data.settings || {});
    setSettingsStatus("Reset to defaults.", "ok"); showToast("Settings reset to defaults.");
  } catch (e) { setSettingsStatus("Could not reset settings.", "err"); }
}
function setSettingsStatus(msg, kind) {
  const s = $("#settingsStatus");
  s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}

/* ----------------------------- documents (RAG) ----------------------------- */
/* --------------------------- watched folders ---------------------------- */
function renderWatchedFolders(st) {
  const list = $("#wfList"); list.innerHTML = "";
  const last = (st.log || [])[0];
  $("#wfStatus").textContent = last
    ? `Last sweep: ${last.summary || ""}` : "";
  if (!(st.folders || []).length) {
    list.innerHTML = "<div class='out-empty'>No folders watched yet. Add one and new files flow into the knowledge base by themselves.</div>";
    return;
  }
  st.folders.forEach(f => {
    const row = el("div", "log-row");
    const meta = el("span", "log-meta");
    meta.textContent = `${f.enabled ? "●" : "◌"} ${f.path}`;
    const wrap = el("span", "mcp-actions");
    const tog = el("button", "btn ghost btn-mini"); tog.textContent = f.enabled ? "Pause" : "Resume";
    tog.addEventListener("click", async () => {
      const r = await fetch("/api/folders/toggle", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: f.path, enabled: !f.enabled }),
      }).then(r => r.json());
      renderWatchedFolders(r);
    });
    const rm = el("button", "btn ghost btn-mini"); rm.textContent = "Remove";
    rm.addEventListener("click", async () => {
      const r = await fetch("/api/folders?path=" + encodeURIComponent(f.path), { method: "DELETE" }).then(r => r.json());
      renderWatchedFolders(r);
    });
    wrap.append(tog, rm); row.append(meta, wrap); list.appendChild(row);
  });
}
async function loadWatchedFolders() {
  try { renderWatchedFolders(await fetch("/api/folders").then(r => r.json())); }
  catch (e) {}
}
async function addWatchedFolder() {
  const path = $("#wfPath").value.trim();
  if (!path) return;
  $("#wfStatus").textContent = "Adding and indexing…";
  try {
    const r = await fetch("/api/folders", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }).then(r => r.json());
    if (r.detail) { $("#wfStatus").textContent = r.detail; return; }
    $("#wfPath").value = "";
    $("#wfStatus").textContent = r.first_sweep
      ? `Indexed: ${r.first_sweep.added} added, ${r.first_sweep.updated} updated.` : "";
    renderWatchedFolders(r); loadDocuments && loadDocuments();
  } catch (e) { $("#wfStatus").textContent = "Could not add that folder."; }
}
async function scanWatchedFolders() {
  $("#wfStatus").textContent = "Scanning…";
  try {
    const r = await fetch("/api/folders/scan", { method: "POST" }).then(r => r.json());
    $("#wfStatus").textContent = `Scan: ${r.summary.added} added, ${r.summary.updated} updated, ${r.summary.skipped} unchanged.`;
    renderWatchedFolders(r);
  } catch (e) { $("#wfStatus").textContent = "Scan failed."; }
}
async function openDocuments() {
  loadWatchedFolders();
  const m = $("#documentsModal");
  m.hidden = false; m.style.display = "flex";
  $("#docNote").hidden = true; $("#docResults").innerHTML = ""; setDocStatus("", "");
  await loadDocuments();
}
function closeDocuments() {
  const m = $("#documentsModal");
  m.hidden = true; m.style.display = "none";
  refreshStats();
}
async function loadDocuments() {
  try {
    const d = await fetch("/api/documents").then(r => r.json());
    renderDocuments(d);
  } catch (e) { setDocStatus("Could not load documents.", "err"); }
}
function renderDocuments(d) {
  $("#docStats").textContent =
    `${d.doc_count} document${d.doc_count === 1 ? "" : "s"} · ${d.chunk_count} chunks · embeddings: ${d.embed_model}`;
  const list = $("#docList");
  list.innerHTML = "";
  const docs = d.documents || [];
  if (!docs.length) {
    const e = el("div", "doc-empty"); e.textContent = "Nothing indexed yet.";
    list.appendChild(e); return;
  }
  docs.forEach(doc => {
    const row = el("div", "doc-row");
    const info = el("div", "doc-info");
    const nm = el("div", "doc-name"); nm.textContent = doc.name;
    const meta = el("div", "doc-meta"); meta.textContent = `${doc.chunks} chunks`;
    info.append(nm, meta);
    const badge = el("span", "doc-badge " + (doc.embedded ? "semantic" : "keyword"));
    badge.textContent = doc.embedded ? "semantic" : "keyword";
    const rm = el("button", "doc-remove"); rm.textContent = "Remove";
    rm.addEventListener("click", () => removeDocument(doc.id));
    row.append(info, badge, rm);
    list.appendChild(row);
  });
}
async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  let added = 0, chunks = 0, lastNote = "";
  let truncated = 0;
  for (let i = 0; i < files.length; i++) {
    setDocStatus(`Indexing ${files[i].name} (${i + 1}/${files.length})…`, "");
    const fd = new FormData(); fd.append("file", files[i]);
    try {
      const r = await fetch("/api/documents", { method: "POST", body: fd });
      const data = await r.json();
      const res = data.result || {};
      if (res.error) { lastNote = res.error; }
      else {
        added += (res.added || 0) + (res.updated || 0);
        chunks += res.chunks || 0;
        if (res.note) lastNote = res.note;
        // a truncated index answers from part of your material without
        // saying so, which is worse than refusing
        if (res.warning) lastNote = res.warning;
        if (res.truncated) truncated += res.truncated;
      }
      renderDocuments(data);
    } catch (e) { lastNote = "Upload failed for " + files[i].name; }
  }
  showDocNote(lastNote);
  setDocStatus(
    `Indexed ${added} file${added === 1 ? "" : "s"} (${chunks} chunks).`
    + (truncated ? ` ${truncated} left out — see the note above.` : ""),
    truncated ? "warn" : "ok");
  refreshEnginesMetaQuietly();
}
async function ingestPath() {
  const p = $("#docPath").value.trim();
  if (!p) { setDocStatus("Enter a file or folder path.", "err"); return; }
  const btn = $("#docPathBtn");
  if (btn) btn.disabled = true;
  // Indexing runs in bounded passes: each call does what it can in about a
  // minute, commits, and says how much is left. That keeps every run inside
  // a browser request and means stopping never loses work — so the loop
  // below just keeps calling until there is nothing left.
  let totalFiles = 0, totalChunks = 0, pass = 0, stop = false;
  const stopBtn = $("#docStopBtn");
  if (stopBtn) {
    stopBtn.hidden = false;
    stopBtn.onclick = () => { stop = true;
      setDocStatus("Stopping after this pass — work so far is saved.", ""); };
  }
  try {
    while (!stop) {
      pass += 1;
      setDocStatus(`Indexing… pass ${pass}, ${totalFiles} file(s) so far.`, "");
      const r = await fetch("/api/documents/path", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: p }),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        setDocStatus(errText(d, "Could not index that."), "err");
        break;
      }
      totalFiles += (d.added || 0) + (d.updated || 0);
      totalChunks += d.chunks || 0;
      renderDocuments(await fetch("/api/documents").then(x => x.json()));
      if (!d.stopped_early && !d.truncated) {
        setDocStatus(
          `Done. ${totalFiles} file(s), ${totalChunks} chunks.`
          + (d.skipped_binary
             ? ` ${d.skipped_binary} image/binary file(s) skipped.` : ""),
          "ok");
        if (d.note) showDocNote(d.note);
        break;
      }
      const left = d.remaining_here ?? d.truncated ?? "some";
      setDocStatus(`${totalFiles} file(s) saved, ${left} to go — continuing…`,
                   "");
    }
    if (stop) {
      setDocStatus(`Stopped. ${totalFiles} file(s) indexed and saved — `
                   + `press again to carry on where it left off.`, "ok");
    }
  } catch (e) {
    setDocStatus(`Interrupted after ${totalFiles} file(s) — those are saved. `
                 + `Press again to continue.`, "warn");
  }
  if (btn) btn.disabled = false;
  if (stopBtn) stopBtn.hidden = true;
  refreshEnginesMetaQuietly();
}
async function removeDocument(id) {
  try {
    const d = await fetch("/api/documents/" + id, { method: "DELETE" }).then(r => r.json());
    renderDocuments(d);
  } catch (e) { setDocStatus("Could not remove.", "err"); }
}
async function clearDocuments() {
  if (!confirm("Remove all indexed documents? This cannot be undone.")) return;
  try {
    const d = await fetch("/api/documents", { method: "DELETE" }).then(r => r.json());
    renderDocuments(d); showToast(`Removed ${d.removed} document(s).`);
  } catch (e) { setDocStatus("Could not clear.", "err"); }
}
async function searchDocuments() {
  const q = $("#docSearch").value.trim();
  const out = $("#docResults");
  if (!q) { out.innerHTML = ""; return; }
  out.innerHTML = `<div class="doc-empty">Searching…</div>`;
  try {
    const d = await fetch("/api/documents/search?q=" + encodeURIComponent(q)).then(r => r.json());
    out.innerHTML = "";
    const results = d.results || [];
    if (!results.length) { out.innerHTML = `<div class="doc-empty">No matches.</div>`; return; }
    results.forEach(r => {
      const card = el("div", "doc-result");
      const head = el("div", "doc-result-head");
      const src = el("span"); src.textContent = r.source || "?";
      const sc = el("span", "score"); sc.textContent = (r.score != null ? "score " + r.score : "keyword");
      head.append(src, sc);
      const txt = el("div", "doc-result-text"); txt.textContent = r.text || "";
      card.append(head, txt); out.appendChild(card);
    });
  } catch (e) { out.innerHTML = `<div class="doc-empty">Search failed.</div>`; }
}
function showDocNote(note) {
  const n = $("#docNote");
  if (note) { n.textContent = note; n.hidden = false; } else { n.hidden = true; }
}
function setDocStatus(msg, kind) {
  const s = $("#docStatus");
  s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}
// the system prompt counts indexed docs; nothing else needs refreshing client-side
function refreshEnginesMetaQuietly() {}

/* ----------------------------- memory & skills ----------------------------- */
async function openMemory() {
  const m = $("#memoryModal");
  m.hidden = false; m.style.display = "flex";
  setMemStatus("", ""); switchMemTab("memories");
  await loadMemory();
}
function closeMemory() {
  const m = $("#memoryModal");
  m.hidden = true; m.style.display = "none";
  refreshStats();
}
function switchMemTab(tab) {
  $("#memTabMemories").classList.toggle("active", tab === "memories");
  $("#memTabSkills").classList.toggle("active", tab === "skills");
  $("#memPaneMemories").hidden = tab !== "memories";
  $("#memPaneSkills").hidden = tab !== "skills";
}
async function loadMemory() {
  try { renderMemory(await fetch("/api/memory").then(r => r.json())); }
  catch (e) { setMemStatus("Could not load memory.", "err"); }
}
function renderMemory(d) {
  $("#memStats").textContent =
    `${d.memory_count} memor${d.memory_count === 1 ? "y" : "ies"} · ${d.skill_count} skill${d.skill_count === 1 ? "" : "s"}`;
  renderMemories(d.memories || []);
  renderSkills(d.skills || []);
}
function renderMemories(list) {
  const wrap = $("#memList"); wrap.innerHTML = "";
  if (!list.length) { const e = el("div", "mem-empty"); e.textContent = "No memories yet."; wrap.appendChild(e); return; }
  list.forEach(mem => {
    const row = el("div", "mem-row");
    const cat = el("span", "mem-cat"); cat.textContent = mem.category || "general";
    const c = el("div", "mem-content"); c.textContent = mem.content;
    const del = el("button", "mem-del"); del.textContent = "Delete";
    del.addEventListener("click", () => deleteMemory(mem.id));
    row.append(cat, c, del); wrap.appendChild(row);
  });
}
function renderSkills(list) {
  const wrap = $("#skillList"); wrap.innerHTML = "";
  if (!list.length) { const e = el("div", "mem-empty"); e.textContent = "No skills taught yet."; wrap.appendChild(e); return; }
  list.forEach(sk => {
    const row = el("div", "mem-row");
    const info = el("div", "mem-content");
    const nm = el("div", "skill-name"); nm.textContent = sk.name;
    const ds = el("div", "skill-desc"); ds.textContent = sk.description || "";
    info.append(nm, ds);
    const del = el("button", "mem-del"); del.textContent = "Delete";
    del.addEventListener("click", () => deleteSkill(sk.name));
    row.append(info, del); wrap.appendChild(row);
  });
}
async function searchMemories() {
  const q = $("#memSearch").value.trim();
  if (!q) { loadMemory(); return; }
  try {
    const d = await fetch("/api/memory/search?q=" + encodeURIComponent(q)).then(r => r.json());
    renderMemories(d.memories || []);
  } catch (e) { setMemStatus("Search failed.", "err"); }
}
async function addMemory() {
  const content = $("#memContent").value.trim();
  if (!content) return;
  const category = $("#memCategory").value.trim() || "general";
  try {
    const d = await fetch("/api/memory", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, category }),
    }).then(r => r.json());
    renderMemory(d);
    if (d.ok) { $("#memContent").value = ""; $("#memCategory").value = ""; showToast("Memory added."); }
    else showToast(d.message || "Not added.", true);
  } catch (e) { setMemStatus("Could not add memory.", "err"); }
}
async function deleteMemory(id) {
  try { renderMemory(await fetch("/api/memory/" + id, { method: "DELETE" }).then(r => r.json())); }
  catch (e) { setMemStatus("Could not delete.", "err"); }
}
async function addSkill() {
  const name = $("#skName").value.trim();
  const description = $("#skDesc").value.trim();
  const instructions = $("#skInstr").value.trim();
  if (!name || !instructions) { setMemStatus("Skill name and instructions are required.", "err"); return; }
  try {
    const d = await fetch("/api/skills", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description, instructions }),
    }).then(r => r.json());
    renderMemory(d);
    $("#skName").value = ""; $("#skDesc").value = ""; $("#skInstr").value = "";
    setMemStatus(d.replaced ? "Skill updated." : "Skill saved.", "ok"); showToast("Skill saved.");
  } catch (e) { setMemStatus("Could not save skill.", "err"); }
}
async function deleteSkill(name) {
  try { renderMemory(await fetch("/api/skills/" + encodeURIComponent(name), { method: "DELETE" }).then(r => r.json())); }
  catch (e) { setMemStatus("Could not delete skill.", "err"); }
}
function setMemStatus(msg, kind) {
  const s = $("#memStatus"); s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}

/* ----------------------------- permissions ----------------------------- */
async function openPerms() {
  const m = $("#permsModal");
  m.hidden = false; m.style.display = "flex";
  setPermStatus("", ""); await loadPerms();
}
function closePerms() {
  const m = $("#permsModal");
  m.hidden = true; m.style.display = "none";
}
async function loadPerms() {
  try { renderPerms(await fetch("/api/permissions").then(r => r.json())); }
  catch (e) { setPermStatus("Could not load permissions.", "err"); }
}
function renderPerms(d) {
  $("#permStats").textContent = `${d.count} rule${d.count === 1 ? "" : "s"}`;
  const wrap = $("#permList"); wrap.innerHTML = "";
  const perms = d.permissions || [];
  if (!perms.length) {
    const e = el("div", "mem-empty");
    e.textContent = "No standing permissions - Agent Jo asks before running commands or writing files.";
    wrap.appendChild(e); return;
  }
  perms.forEach(p => {
    const row = el("div", "mem-row");
    const badge = el("span", "perm-kind " + (p.kind === "command" ? "command" : "write_dir"));
    badge.textContent = p.kind === "command" ? "command" : "write dir";
    const c = el("div", "mem-content perm-pattern"); c.textContent = p.pattern;
    const del = el("button", "mem-del"); del.textContent = "Revoke";
    del.addEventListener("click", () => deletePerm(p.id));
    row.append(badge, c, del); wrap.appendChild(row);
  });
}
async function addPerm() {
  const kind = $("#permKind").value;
  const pattern = $("#permPattern").value.trim();
  if (!pattern) { setPermStatus("Enter a command prefix or directory path.", "err"); return; }
  try {
    const d = await fetch("/api/permissions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, pattern }),
    }).then(r => r.json());
    renderPerms(d);
    $("#permPattern").value = "";
    setPermStatus(d.added ? "Rule added." : "That rule already exists.", d.added ? "ok" : "");
  } catch (e) { setPermStatus("Could not add rule.", "err"); }
}
async function deletePerm(id) {
  try { renderPerms(await fetch("/api/permissions/" + id, { method: "DELETE" }).then(r => r.json())); }
  catch (e) { setPermStatus("Could not revoke.", "err"); }
}
function setPermStatus(msg, kind) {
  const s = $("#permStatus"); s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}

/* ----------------------------- scheduler & tasks ----------------------------- */
function fmtTs(ts) {
  if (!ts) return "—";
  try { return new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
  catch (e) { return "—"; }
}
async function openSched() {
  const m = $("#schedModal");
  m.hidden = false; m.style.display = "flex";
  setSchedStatus("", ""); switchSchedTab("schedules");
  populateSchedEngines(); updateSchedFields();
  await loadSchedules(); await loadTasks();
}
function closeSched() { const m = $("#schedModal"); m.hidden = true; m.style.display = "none"; refreshStats(); }
function switchSchedTab(tab) {
  $("#schedTabSchedules").classList.toggle("active", tab === "schedules");
  $("#schedTabTasks").classList.toggle("active", tab === "tasks");
  $("#schedPaneSchedules").hidden = tab !== "schedules";
  $("#schedPaneTasks").hidden = tab !== "tasks";
}
function populateSchedEngines() {
  const sel = $("#schEngine"); sel.innerHTML = "";
  (state.engines || []).forEach(e => {
    const o = document.createElement("option"); o.value = e.id; o.textContent = e.id; sel.appendChild(o);
  });
}
function updateSchedFields() {
  const k = $("#schKind").value;
  $("#schTimeWrap").hidden = !(k === "daily" || k === "weekdays" || k === "weekly");
  $("#schMinWrap").hidden = k !== "minutes";
  $("#schDowWrap").hidden = k !== "weekly";
}
async function loadSchedules() {
  try { renderSchedules((await fetch("/api/schedules").then(r => r.json())).schedules || []); }
  catch (e) { setSchedStatus("Could not load schedules.", "err"); }
}
function renderSchedules(list) {
  const wrap = $("#schedList"); wrap.innerHTML = "";
  if (!list.length) { const e = el("div", "mem-empty"); e.textContent = "No schedules yet."; wrap.appendChild(e); return; }
  list.forEach(s => {
    const row = el("div", "sched-row");
    const head = el("div", "sched-head");
    const nm = el("span", "sched-name");
    const _ico = s.action === "watch" ? "👁 " : s.action === "autopilot" ? "✉ " : "";
    nm.textContent = _ico + s.name;
    const dsc = el("span", "sched-desc"); dsc.textContent = s.describe || "";
    const pill = el("span", "status-pill " + (s.enabled ? "active" : "disabled"));
    pill.textContent = s.enabled ? "enabled" : "paused";
    head.append(nm, dsc, pill);
    const pr = el("div", "sched-prompt"); pr.textContent = s.prompt;
    const meta = el("div", "sched-meta");
    const next = el("span"); next.innerHTML = `<b>Next:</b> ${s.enabled ? fmtTs(s.next_run) : "paused"}`;
    meta.appendChild(next);
    if (s.last_run) {
      const last = el("span");
      last.innerHTML = `<b>Last:</b> ${fmtTs(s.last_run)} · ${escapeHtml(s.last_status || "")}`;
      meta.appendChild(last);
    }
    if (s.engine && s.engine !== "Auto") { const en = el("span"); en.innerHTML = `<b>Engine:</b> ${escapeHtml(s.engine)}`; meta.appendChild(en); }
    if (s.full_access) { const fa = el("span"); fa.innerHTML = "<b>Full access</b>"; meta.appendChild(fa); }
    row.append(head, pr, meta);
    if (s.last_summary) { const sm = el("div", "task-summary"); sm.textContent = s.last_summary; row.appendChild(sm); }
    const acts = el("div", "sched-actions");
    const tog = el("button", "mini-btn" + (s.enabled ? " on" : "")); tog.textContent = s.enabled ? "Pause" : "Enable";
    tog.addEventListener("click", () => toggleSchedule(s.id));
    const run = el("button", "mini-btn"); run.textContent = "Run now";
    run.addEventListener("click", () => runSchedule(s.id, s.name));
    const prev = el("div", "sched-preview"); prev.hidden = true;
    if (s.action === "autopilot" || s.action === "watch") {
      const pvb = el("button", "mini-btn"); pvb.textContent = "Preview next";
      pvb.addEventListener("click", () => previewSchedule(s.id, prev));
      acts.append(tog, run, pvb);
    } else {
      acts.append(tog, run);
    }
    const del = el("button", "mini-btn danger"); del.textContent = "Delete";
    del.addEventListener("click", () => deleteSchedule(s.id));
    acts.append(del); row.appendChild(acts); row.appendChild(prev);
    wrap.appendChild(row);
  });
}
async function previewSchedule(sid, box) {
  box.hidden = false; box.textContent = "Previewing (no send)…";
  try {
    const r = await fetch(`/api/schedules/${sid}/preview`, { method: "POST" }).then(r => r.json());
    if (r.ok === false) { box.textContent = "Preview: " + (r.error || "failed"); return; }
    if (r.action === "autopilot") {
      box.textContent = `Would send ${r.would_send} of ${r.total}` +
        (r.blocked ? `, ${r.blocked} blocked (not approved)` : "") +
        (r.skipped ? `, ${r.skipped} skipped` : "") + " — nothing delivered (dry run).";
    } else if (r.action === "watch") {
      box.textContent = (r.changed ? "Change detected. " : "No change right now. ") +
        (r.note || "") + (r.summary && r.changed ? " · " + r.summary.split("\n")[0] : "");
    } else {
      box.textContent = r.note || "Preview ready.";
    }
  } catch (e) { box.textContent = "Preview failed."; }
}
async function createSchedule() {
  const name = $("#schName").value.trim();
  const prompt = $("#schPrompt").value.trim();
  if (!name || !prompt) { setSchedStatus("A name and an instruction are both required.", "err"); return; }
  const body = {
    name, prompt, kind: $("#schKind").value,
    time: $("#schTime").value.trim() || "07:00",
    n: parseInt($("#schN").value, 10) || 60,
    dow: parseInt($("#schDow").value, 10) || 0,
    engine: $("#schEngine").value || "Auto",
    full_access: $("#schFull").checked,
  };
  try {
    const d = await fetch("/api/schedules", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (d.schedules) {
      renderSchedules(d.schedules);
      $("#schName").value = ""; $("#schPrompt").value = "";
      setSchedStatus(`Scheduled - first run ${fmtTs(d.next_run)}.`, "ok");
      showToast("Schedule created.");
    } else {
      setSchedStatus(errText(d, "Could not create schedule."), "err");
    }
  } catch (e) { setSchedStatus("Could not create schedule.", "err"); }
}
async function toggleSchedule(id) {
  try { renderSchedules((await fetch(`/api/schedules/${id}/toggle`, { method: "POST" }).then(r => r.json())).schedules || []); }
  catch (e) { setSchedStatus("Could not toggle.", "err"); }
}
async function runSchedule(id, name) {
  try {
    await fetch(`/api/schedules/${id}/run`, { method: "POST" });
    showToast(`Running '${name}' now…`);
    setTimeout(loadSchedules, 1500);
  } catch (e) { setSchedStatus("Could not run.", "err"); }
}
async function deleteSchedule(id) {
  try { renderSchedules((await fetch(`/api/schedules/${id}`, { method: "DELETE" }).then(r => r.json())).schedules || []); }
  catch (e) { setSchedStatus("Could not delete.", "err"); }
}
async function loadTasks() {
  try { renderTasks((await fetch("/api/tasks").then(r => r.json())).tasks || []); }
  catch (e) {}
  loadAutoResume();
}
async function loadAutoResume() {
  try {
    const s = await fetch("/api/autoresume").then(r => r.json());
    $("#arEnabled").checked = !!s.enabled;
    $("#arFull").checked = !!s.full_access;
    $("#arCadence").value = s.cadence || "hourly";
    $("#arIdle").value = s.idle_minutes || 30;
    $("#arAttempts").value = s.max_attempts || 3;
    $("#arPerSweep").value = s.max_per_sweep || 3;
    const st = $("#arState");
    st.textContent = "Auto-resume: " + (s.enabled ? "ARMED" : "off");
    st.className = "ap-state" + (s.enabled ? " armed" : "");
    $("#arArmRow").classList.toggle("armed", !!s.enabled);
  } catch (e) {}
}
async function saveAutoResume() {
  setOut("arStatus", "Saving…", "");
  try {
    const s = await fetch("/api/autoresume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("#arEnabled").checked, full_access: $("#arFull").checked,
        cadence: $("#arCadence").value,
        idle_minutes: parseInt($("#arIdle").value, 10) || 30,
        max_attempts: parseInt($("#arAttempts").value, 10) || 3,
        max_per_sweep: parseInt($("#arPerSweep").value, 10) || 3,
      }),
    }).then(r => r.json());
    loadAutoResume();
    setOut("arStatus", s.enabled ? "Saved · ARMED" : "Saved · off", s.enabled ? "ok" : "");
    showToast(s.enabled ? "Auto-resume armed" : "Auto-resume saved");
  } catch (e) { setOut("arStatus", "Save failed.", "err"); }
}
async function previewAutoResume() {
  const box = $("#arPreview"); box.hidden = false; box.textContent = "Checking…";
  try {
    const r = await fetch("/api/autoresume/candidates").then(r => r.json());
    if (!r.count) { box.textContent = "No stalled tasks are eligible right now."; return; }
    box.innerHTML = "Would resume " + r.count + ": " +
      r.tasks.map(t => `#${t.id} ${t.title} (→ ${t.next_step})`).join("; ");
  } catch (e) { box.textContent = "Preview failed."; }
}
async function runAutoResume() {
  setOut("arStatus", "Sweeping…", "");
  try {
    const r = await fetch("/api/autoresume/run", { method: "POST" }).then(r => r.json());
    setOut("arStatus", r.ok ? "Sweep started — check the tasks below shortly." : (r.detail || "Could not run."), r.ok ? "ok" : "err");
    if (r.ok) setTimeout(loadTasks, 2500);
  } catch (e) { setOut("arStatus", "Run failed.", "err"); }
}
async function pauseAutoResume() {
  try {
    await fetch("/api/autoresume/pause", { method: "POST" });
    loadAutoResume();
    setOut("arStatus", "Paused — auto-resume disarmed.", "");
    showToast("Auto-resume paused");
  } catch (e) { setOut("arStatus", "Pause failed.", "err"); }
}
function renderTasks(list) {
  const wrap = $("#taskList"); wrap.innerHTML = "";
  if (!list.length) {
    const e = el("div", "mem-empty");
    e.textContent = "No tasks yet - Agent Jo creates these while working through bigger jobs.";
    wrap.appendChild(e); return;
  }
  list.forEach(t => {
    const row = el("div", "task-row");
    const head = el("div", "sched-head");
    const title = el("span", "task-title"); title.textContent = t.title;
    const pill = el("span", "status-pill " + (t.status || "active")); pill.textContent = t.status || "active";
    head.append(title, pill); row.appendChild(head);
    if (t.steps && t.steps.length) {
      const steps = el("div", "task-steps");
      t.steps.forEach(st => {
        const s = el("div", "task-step");
        const seq = el("span", "seq"); seq.textContent = st.seq + ".";
        const desc = el("span", "task-step-desc");
        desc.textContent = st.description;
        if (st.note) {
          const note = el("span", "task-step-note" + (st.status === "blocked" ? " needs" : ""));
          note.textContent = " — " + st.note;
          desc.appendChild(note);
        }
        const stt = el("span", "stp stp-" + (st.status || "pending")); stt.textContent = st.status || "pending";
        s.append(seq, desc, stt); steps.appendChild(s);
      });
      row.appendChild(steps);
    }
    if (t.summary) { const sm = el("div", "task-summary"); sm.textContent = t.summary; row.appendChild(sm); }
    if (t.status === "active") {
      const acts = el("div", "sched-actions");
      const rb = el("button", "mini-btn"); rb.textContent = "Restart in place";
      rb.title = "Reset all steps to pending, keep this task id and history";
      rb.addEventListener("click", async () => {
        rb.disabled = true; rb.textContent = "Restarting…";
        try { renderTasks((await fetch(`/api/tasks/${t.id}/reset`, { method: "POST" }).then(r => r.json())).tasks || []); }
        catch (e) { rb.textContent = "Restart in place"; rb.disabled = false; }
      });
      acts.append(rb); row.appendChild(acts);
    }
    wrap.appendChild(row);
  });
}
function setSchedStatus(msg, kind) {
  const s = $("#schStatus"); s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}

/* ----------------------------- voice ----------------------------- */
function speakText(text) {
  if (!("speechSynthesis" in window)) { showToast("This browser can't read text aloud.", true); return; }
  const clean = (text || "")
    .replace(/```[\s\S]*?```/g, ". code block omitted. ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[(.*?)\]\((.*?)\)/g, "$1")
    .replace(/[*_#>|~]/g, "");
  window.speechSynthesis.cancel();
  if (!clean.trim()) return;
  const u = new SpeechSynthesisUtterance(clean);
  u.rate = 1.0; u.pitch = 1.0;
  window.speechSynthesis.speak(u);
}
function stopSpeaking() { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); }

async function toggleMic() {
  if (state.recording) { stopRecording(); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showToast("Microphone isn't available in this browser.", true); return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.audioChunks = [];
    const mr = new MediaRecorder(stream);
    state.mediaRecorder = mr;
    mr.addEventListener("dataavailable", (e) => { if (e.data && e.data.size) state.audioChunks.push(e.data); });
    mr.addEventListener("stop", () => {
      stream.getTracks().forEach(t => t.stop());
      transcribeBlob(new Blob(state.audioChunks, { type: mr.mimeType || "audio/webm" }));
    });
    mr.start();
    state.recording = true;
    $("#micBtn").classList.add("recording");
    $("#micBtn").title = "Recording - click to stop";
  } catch (e) {
    showToast("Couldn't access the microphone - check the browser's mic permission.", true);
  }
}
function stopRecording() {
  state.recording = false;
  const b = $("#micBtn"); b.classList.remove("recording"); b.title = "Click to dictate";
  if (state.mediaRecorder && state.mediaRecorder.state !== "inactive") state.mediaRecorder.stop();
}
async function transcribeBlob(blob) {
  const ext = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mp4") ? "mp4" : "webm";
  const b = $("#micBtn"); b.classList.add("busy");
  try {
    const fd = new FormData();
    fd.append("audio", blob, "rec." + ext);
    const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(errText(d, "Transcription failed."), true); return; }
    const text = (d.text || "").trim();
    if (!text) { showToast("Didn't catch that - try again.", true); return; }
    const input = $("#input");
    input.value = (input.value ? input.value.trim() + " " : "") + text;
    input.dispatchEvent(new Event("input"));   // trigger autosize
    input.focus();
  } catch (e) { showToast("Transcription failed.", true); }
  finally { b.classList.remove("busy"); }
}

/* ----------------------------- auth ----------------------------- */
function showAuthOverlay() {
  const ov = $("#authOverlay"); ov.hidden = false;
  const err = $("#authError");
  const submit = async () => {
    const pw = $("#authPassword").value;
    if (!pw) return;
    err.textContent = "";
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); err.textContent = errText(d, "Incorrect password."); return; }
      location.reload();
    } catch (e) { err.textContent = "Could not reach the server."; }
  };
  $("#authLoginBtn").addEventListener("click", submit);
  $("#authPassword").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  $("#authPassword").focus();
}

async function renderSecurity() {
  const body = $("#securityBody");
  setSecurityStatus("", "");
  let st = { enabled: false };
  try { st = await fetch("/api/auth/status").then(r => r.json()); } catch (e) {}
  body.innerHTML = "";
  const state_line = el("div", "sec-state");
  state_line.innerHTML = st.enabled
    ? 'Status: <span class="on">protected</span>'
    : 'Status: <span class="off">open (no password)</span>';
  body.appendChild(state_line);

  if (!st.enabled) {
    const row = el("div", "sec-row");
    const inp = el("input"); inp.type = "password"; inp.id = "secNewPw"; inp.placeholder = "New password (min 4 chars)"; inp.autocomplete = "new-password";
    const btn = el("button", "btn primary"); btn.textContent = "Enable protection";
    btn.addEventListener("click", () => enableProtection());
    row.append(inp, btn); body.appendChild(row);
  } else {
    const row1 = el("div", "sec-row");
    const cur = el("input"); cur.type = "password"; cur.id = "secCurPw"; cur.placeholder = "Current password"; cur.autocomplete = "current-password";
    const nw = el("input"); nw.type = "password"; nw.id = "secNewPw"; nw.placeholder = "New password"; nw.autocomplete = "new-password";
    row1.append(cur, nw); body.appendChild(row1);
    const row2 = el("div", "sec-row");
    const chg = el("button", "btn primary"); chg.textContent = "Change password";
    chg.addEventListener("click", () => changePassword());
    const off = el("button", "btn ghost"); off.textContent = "Disable protection";
    off.addEventListener("click", () => disableProtection());
    const out = el("button", "btn ghost"); out.textContent = "Log out";
    out.addEventListener("click", () => logout());
    row2.append(chg, off, out); body.appendChild(row2);
  }
}
async function enableProtection() {
  const pw = $("#secNewPw").value;
  if (!pw || pw.length < 4) { setSecurityStatus("Password must be at least 4 characters.", "err"); return; }
  try {
    const r = await fetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_password: pw }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { setSecurityStatus(errText(d, "Could not set password."), "err"); return; }
    setSecurityStatus("Protection enabled.", "ok");
    showToast("Password protection enabled.");
    renderSecurity();
  } catch (e) { setSecurityStatus("Could not set password.", "err"); }
}
async function changePassword() {
  const current_password = $("#secCurPw").value;
  const new_password = $("#secNewPw").value;
  if (!new_password || new_password.length < 4) { setSecurityStatus("New password must be at least 4 characters.", "err"); return; }
  try {
    const r = await fetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_password, current_password }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { setSecurityStatus(errText(d, "Could not change password."), "err"); return; }
    setSecurityStatus("Password changed.", "ok");
    showToast("Password changed.");
    renderSecurity();
  } catch (e) { setSecurityStatus("Could not change password.", "err"); }
}
async function disableProtection() {
  const password = window.prompt("Enter the current password to disable protection:") || "";
  if (!password) return;
  try {
    const r = await fetch("/api/auth/disable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { setSecurityStatus(errText(d, "Could not disable."), "err"); return; }
    setSecurityStatus("Protection disabled.", "ok");
    showToast("Password protection disabled.");
    renderSecurity();
  } catch (e) { setSecurityStatus("Could not disable.", "err"); }
}
async function logout() {
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
  location.reload();
}
function setSecurityStatus(msg, kind) {
  const s = $("#securityStatus"); s.textContent = msg; s.className = "engine-status" + (kind ? " " + kind : "");
}

/* ----------------------------- dashboard KPIs ----------------------------- */
async function refreshStats() {
  try {
    const s = await fetch("/api/stats").then(r => r.json());
    renderStats(s);
    const wt = $("#webToggle");
    if (wt && document.activeElement !== wt) wt.checked = !!s.web;
    const tw = $("#teamworkToggle");
    if (tw && document.activeElement !== tw) tw.checked = !!s.teamwork;
  } catch (e) {}
  refreshDashboard();
}
async function setTeamwork(on) {
  try {
    const r = await fetch("/api/teamwork", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: on }),
    }).then(r => r.json());
    $("#teamworkToggle").checked = !!r.teamwork;
    showToast(r.teamwork ? "Teamwork on — local worker handles grunt work" : "Teamwork off");
    refreshStats();
  } catch (e) {}
}
async function setWebAccess(on) {
  try {
    const r = await fetch("/api/web", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: on }),
    }).then(r => r.json());
    $("#webToggle").checked = !!r.web;
    showToast(r.web ? "Web access on" : "Web access off");
    refreshStats();
  } catch (e) {}
}
/* Balance the tile grid.
   `auto-fit` fills each row then leaves whatever is left over stranded on the
   last one: eleven tiles at five across gives 5 / 5 / 1, and that lone tile
   sits beside a stretch of dead space. Choosing the column count from the tile
   COUNT as well as the width gives 4 / 4 / 3 — every row full, every tile the
   same size. Recomputed on resize, because the right answer changes with the
   window. */
const TILE_MIN = 150, TILE_GAP = 8, TILE_MAX_COLS = 6;
function layoutTiles() {
  const grid = $("#dashTiles");
  if (!grid) return;
  const kids = Array.from(grid.children);
  const n = kids.length;
  if (!n) return;
  const width = grid.clientWidth || (grid.parentElement && grid.parentElement.clientWidth) || 0;
  if (!width) return;                       // hidden, or not laid out yet
  let fit = Math.max(1, Math.floor((width + TILE_GAP) / (TILE_MIN + TILE_GAP)));
  fit = Math.min(fit, TILE_MAX_COLS, n);
  const rows = Math.ceil(n / fit);
  const cols = Math.ceil(n / rows);
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;

  // A grid fills row by row, so whatever doesn't divide evenly is stranded on
  // the last row beside empty space — and some counts (13 tiles) can never
  // divide evenly at any sensible width. Widening the last row's tiles to take
  // up the leftover columns fills the pane properly instead.
  kids.forEach(k => { k.style.gridColumn = ""; });
  const rem = n % cols;
  if (rem) {
    const base = Math.floor(cols / rem), extra = cols % rem;
    kids.slice(n - rem).forEach((k, i) => {
      k.style.gridColumn = `span ${base + (i < extra ? 1 : 0)}`;
    });
  }
}

let _sparkSeq = 0;
function sparkline(values) {
  // The signature graphic. A bare polyline tells you the shape; this adds
  // the two things a reading actually needs — a filled body so the trend
  // has weight at 22px tall, and a marker on the latest point so "where am
  // I now" doesn't require squinting at the right-hand edge.
  const w = 96, h = 24;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = (max - min) || 1;
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const y = (v) => (h - 2.5) - ((v - min) / span) * (h - 5);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("class", "dash-spark");
  svg.setAttribute("preserveAspectRatio", "none");

  // a gradient per instance, so two tiles can't share one id
  const gid = "spk" + (++_sparkSeq);
  const defs = document.createElementNS(ns, "defs");
  const grad = document.createElementNS(ns, "linearGradient");
  grad.setAttribute("id", gid);
  grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
  grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
  const s1 = document.createElementNS(ns, "stop");
  s1.setAttribute("offset", "0%"); s1.setAttribute("stop-color", "currentColor");
  s1.setAttribute("stop-opacity", "0.28");
  const s2 = document.createElementNS(ns, "stop");
  s2.setAttribute("offset", "100%"); s2.setAttribute("stop-color", "currentColor");
  s2.setAttribute("stop-opacity", "0");
  grad.append(s1, s2); defs.appendChild(grad); svg.appendChild(defs);

  const area = document.createElementNS(ns, "path");
  area.setAttribute("d", `M0,${h} L${pts.join(" L")} L${w},${h} Z`);
  area.setAttribute("class", "dash-spark-area");
  area.setAttribute("fill", `url(#${gid})`);
  const line = document.createElementNS(ns, "path");
  line.setAttribute("d", `M${pts.join(" L")}`);
  line.setAttribute("class", "dash-spark-line");
  svg.append(area, line);

  // the latest reading, marked
  const last = values[values.length - 1];
  if (values.length > 1) {
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", w.toFixed(1));
    dot.setAttribute("cy", y(last).toFixed(1));
    dot.setAttribute("r", "2");
    dot.setAttribute("class", "dash-spark-dot");
    svg.appendChild(dot);
  }
  return svg;
}

async function refreshDashboard() {
  const dash = $("#dash");
  if (!dash) return;
  dash.hidden = dashHidden();
  const att0 = $("#dashAttention");
  if (att0 && !att0.children.length) {
    const wait = el("div", "dash-clear");
    wait.textContent = "Checking\u2026";
    att0.appendChild(wait);
  }
  try {
    const d = await fetch("/api/dashboard").then(r => r.json());
    paintDashboard(d);
  } catch (e) {
    const att = $("#dashAttention");
    if (att) {
      att.innerHTML = "";
      const row = el("div", "dash-clear");
      row.textContent = "Couldn't load the dashboard \u2014 the app is still "
        + "working. It retries automatically.";
      att.appendChild(row);
    }
    dash.hidden = dashHidden();
  }
}

let _dashAll = [], _dashPrefs = { hidden: [], order: [] };
let _dashSig = "", _tileNodes = {}, _dashPainted = false;
let _attSig = "";

let _runSig = "";
function paintRunning(d) {
  const run = $("#dashRunning");
  // the status chips are part of the board too — rebuilding them on every
  // poll flickered exactly like the tiles did
  const sig = JSON.stringify(d.running || {});
  if (sig === _runSig && run.children.length) return;
  _runSig = sig;
  run.innerHTML = "";
  const r = d.running || {};
  const chip = (label, value, cls) => {
    const c = el("span", "dash-chip" + (cls ? " " + cls : ""));
    const b = el("b"); b.textContent = value;
    const l = el("span"); l.textContent = label;
    c.append(l, b); run.appendChild(c);
  };
  if (r.engine) chip("Engine", r.engine);
  const sp = r.spend || {};
  if (sp.cap > 0) {
    chip("Spend", `$${Number(sp.usd || 0).toFixed(2)} / $${Number(sp.cap).toFixed(2)}`,
         sp.blocked ? "over" : "");
  } else if (sp.usd > 0) {
    chip("Spend this month", "$" + Number(sp.usd).toFixed(2));
  }
  if (r.top_spender) chip("Biggest", r.top_spender);
  if (r.next_job) {
    const m = r.next_job.in_minutes;
    chip("Next job", r.next_job.overdue ? "overdue"
      : m < 1 ? "any moment" : m < 60 ? `in ${m} min` : `in ${Math.round(m / 60)} h`);
  } else if (r.active_schedules === 0) {
    chip("Scheduled jobs", "none");
  }
  if (r.privacy && r.privacy !== "off") chip("Privacy", r.privacy);
}

function updateTile(node, t) {
  // Only touch what actually changed. Writing an identical string still
  // dirties the node and can re-trigger layout, which is what made the
  // board twitch every time the poll came back with the same numbers.
  if (node.val.textContent !== t.value) node.val.textContent = t.value;
  if (node.lbl.textContent !== t.label) node.lbl.textContent = t.label;
  const sub = t.sub || "";
  if (node.sub.textContent !== sub) node.sub.textContent = sub;
  node.sub.hidden = !sub;
  const cls = "dash-tile" + (t.tone ? " " + t.tone : "")
              + (t.panel ? " clickable" : "");
  if (node.card.className !== cls) node.card.className = cls;
  const hasBar = typeof t.progress === "number";
  node.track.hidden = !hasBar;
  if (hasBar) {
    const w = Math.max(2, t.progress) + "%";
    if (node.fill.style.width !== w) node.fill.style.width = w;  // animates
  }
  const sig = JSON.stringify(t.spark || []);
  if (sig !== node.spark) {
    node.spark = sig;
    node.sparkHost.innerHTML = "";
    if (t.spark && t.spark.length) {
      node.sparkHost.appendChild(sparkline(t.spark));
    }
  }
}

function paintDashboard(d) {
  const dash = $("#dash");
  if (!dash) return;
  try {
    // The board repaints every 45 seconds. Rebuilding the DOM each time
    // replayed the entrance animation and re-ran the grid layout, so tiles
    // visibly jumped even when not a single number had changed. Nothing to
    // report means nothing to redraw.
    const sig = JSON.stringify([d.needs_you, d.running, d.tiles,
                                d.all_clear]);
    if (sig === _dashSig) {
      dash.hidden = dashHidden();
      return;
    }
    _dashSig = sig;
    _dashAll = d.all_tiles || _dashAll;
    _dashPrefs = d.prefs || _dashPrefs;
    const att = $("#dashAttention");
    const items = d.needs_you || [];
    // the "needs you" list is the third piece of the board — guard it too,
    // so a token count ticking over doesn't redraw rows that haven't moved
    const attSig = JSON.stringify([items, d.all_clear]);
    if (attSig !== _attSig || !att.children.length) {
      _attSig = attSig;
      att.innerHTML = "";
    if (d.all_clear) {
      const row = el("div", "dash-clear");
      const b = el("b"); b.textContent = "All clear.";
      const t = el("span");
      t.textContent = items.length
        ? "Nothing waiting on you. " + items[0].title + "."
        : "Nothing waiting on you.";
      row.append(b, t);
      att.appendChild(row);
    } else {
      items.filter(i => i.severity !== "note").forEach(i => {
        const row = el("button", "dash-item " + i.severity);
        row.type = "button";
        const body = el("div", "dash-item-body");
        const t = el("div", "dash-item-title"); t.textContent = i.title;
        const dt = el("div", "dash-item-detail"); dt.textContent = i.detail;
        body.append(t, dt);
        if (i.why) {
          const w = el("div", "dash-item-why"); w.textContent = i.why;
          body.appendChild(w);
        }
        const go = el("span", "dash-item-go"); go.textContent = "\u203a";
        row.append(body, go);
        // every item is a doorway to the panel that resolves it
        row.addEventListener("click", () => {
          const btn = $("#" + i.panel);
          if (btn) btn.click();
        });
        att.appendChild(row);
      });
    }
    }
    paintRunning(d);
    // --- tiles -----------------------------------------------------------
    const tiles = $("#dashTiles");
    const incoming = d.tiles || [];
    const sameSet =
      Object.keys(_tileNodes).length === incoming.length &&
      incoming.every(t => _tileNodes[t.key]);
    if (sameSet) {
      // same tiles, new readings: write the values into the existing nodes
      // so nothing is created, destroyed, re-animated or re-laid-out
      incoming.forEach(t => updateTile(_tileNodes[t.key], t));
      paintRunning(d);
      dash.hidden = dashHidden();
      return;
    }
    tiles.innerHTML = "";
    _tileNodes = {};
    (d.tiles || []).forEach(t => {
      const card = el(t.panel ? "button" : "div",
                      "dash-tile" + (t.tone ? " " + t.tone : "")
                      + (t.panel ? " clickable" : ""));
      if (t.panel) card.type = "button";
      if (t.title) card.title = t.title;
      const v = el("div", "dash-tile-val"); v.textContent = t.value;
      const l = el("div", "dash-tile-lbl"); l.textContent = t.label;
      card.append(v, l);
      const sub = el("div", "dash-tile-sub");
      sub.textContent = t.sub || "";
      sub.hidden = !t.sub;
      card.appendChild(sub);
      // a fixed host for the sparkline, so swapping the drawing later
      // never adds or removes a child and never shifts the layout
      const sparkHost = el("div", "dash-spark-host");
      if (t.spark && t.spark.length) sparkHost.appendChild(sparkline(t.spark));
      card.appendChild(sparkHost);
      const track = el("div", "dash-bar");
      const fill = el("div", "dash-bar-fill");
      const hasBar = typeof t.progress === "number";
      fill.style.width = hasBar ? Math.max(2, t.progress) + "%" : "0%";
      track.hidden = !hasBar;
      track.appendChild(fill);
      card.appendChild(track);
      if (t.panel) {
        card.addEventListener("click", () => {
          const b = $("#" + t.panel);
          if (b) b.click();
        });
      }
      _tileNodes[t.key] = { card, val: v, lbl: l, sub, sparkHost, track,
                            fill, spark: JSON.stringify(t.spark || []) };
      tiles.appendChild(card);
    });
    tiles.classList.toggle("first-paint", !_dashPainted);
    _dashPainted = true;
    layoutTiles();
    // the toggle owns visibility; this only owns content
    dash.hidden = dashHidden();
  } catch (e) { /* a bad payload must not take the chat down */ }
}

/* ---- the tile picker: what shows, and in what order -------------------- */
async function saveDashPrefs(patch) {
  try {
    const d = await fetch("/api/dashboard/prefs", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) }).then(r => r.json());
    paintDashboard(d);
    renderDashEdit();
  } catch (e) { /* leave the board as it was */ }
}
function renderDashEdit() {
  const list = $("#dashEditList");
  if (!list) return;
  list.innerHTML = "";
  const hidden = new Set(_dashPrefs.hidden || []);
  const order = (_dashPrefs.order || []).slice();
  const ranked = _dashAll.slice().sort((a, b) => {
    const ia = order.indexOf(a.key), ib = order.indexOf(b.key);
    return (ia < 0 ? 9999 : ia) - (ib < 0 ? 9999 : ib);
  });
  ranked.forEach((t, i) => {
    const row = el("div", "dash-edit-row");
    const lab = el("label");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = !hidden.has(t.key);
    cb.addEventListener("change", () => {
      const next = new Set(_dashPrefs.hidden || []);
      cb.checked ? next.delete(t.key) : next.add(t.key);
      saveDashPrefs({ hidden: Array.from(next) });
    });
    const nm = el("span"); nm.textContent = t.label;
    lab.append(cb, nm);
    const up = el("button", "btn ghost btn-mini"); up.textContent = "\u2191";
    up.disabled = i === 0;
    up.addEventListener("click", () => {
      const keys = ranked.map(x => x.key);
      [keys[i - 1], keys[i]] = [keys[i], keys[i - 1]];
      saveDashPrefs({ order: keys });
    });
    const dn = el("button", "btn ghost btn-mini"); dn.textContent = "\u2193";
    dn.disabled = i === ranked.length - 1;
    dn.addEventListener("click", () => {
      const keys = ranked.map(x => x.key);
      [keys[i], keys[i + 1]] = [keys[i + 1], keys[i]];
      saveDashPrefs({ order: keys });
    });
    row.append(lab, up, dn);
    list.appendChild(row);
  });
}

function renderStats(s) {
  // The tile grid supersedes this strip — it carried the same counts with
  // none of the context, and two rows of numbers competing for the same
  // glance is worse than one that means something.
  const strip = $("#kpiStrip");
  if (strip) { strip.hidden = true; strip.innerHTML = ""; }
  return;
}
function renderStatsLegacy(s) {
  const strip = $("#kpiStrip");
  const breakdown = s.cost_by_engine || [];
  let costTitle = "Estimated spend this session, per engine.";
  if (breakdown.length) {
    costTitle = breakdown.map(r =>
      `${r.engine}: $${Number(r.cost).toFixed(4)}` +
      (r.free ? " (free)" : r.priced ? "" : " — no price set")
    ).join("\n");
  }
  const learned = s.learned || {};
  const learnedTotal = (learned.playbooks || 0) + (learned.lessons || 0);
  const cards = [
    ["Memories", s.memories], ["Skills", s.skills],
    ["Learned", learnedTotal, "",
     `Distilled from the agent's own work:\n${learned.playbooks || 0} playbook(s) from completed tasks\n${learned.lessons || 0} lesson(s) from blocked/failed work`],
    ["Tasks", s.tasks],
    ["Documents", s.documents], ["Chats", s.conversations], ["Schedules", s.schedules],
    ["Est. cost", "$" + Number(s.cost || 0).toFixed(2),
     s.budget && s.budget.exceeded ? "over" : s.budget && s.budget.warn ? "warn" : "",
     costTitle + (s.budget && s.budget.enabled
       ? `\n— cap $${Number(s.budget.cap).toFixed(2)}` +
         (s.budget.exceeded ? " (reached — autonomy paused)" : s.budget.warn ? " (nearing)" : "")
       : "")],
  ];
  strip.innerHTML = "";
  cards.forEach(([lbl, val, cls, title]) => {
    const c = el("div", "kpi-card" + (cls ? " " + cls : ""));
    if (title) c.title = title;
    const v = el("div", "kpi-val"); v.textContent = val;
    const l = el("div", "kpi-lbl"); l.textContent = lbl;
    c.append(v, l); strip.appendChild(c);
  });
  strip.hidden = false;
}

function dashHidden() {
  try { return localStorage.getItem("aj_dash_hidden") === "1"; }
  catch (e) { return false; }          // default: visible
}
function applyKpiCollapsed(collapsed) {
  $("#kpiStrip").classList.toggle("kpi-collapsed", collapsed);
  const _dash = $("#dash");
  if (_dash) _dash.hidden = collapsed;
  const btn = $("#kpiToggle");
  btn.classList.toggle("off", collapsed);
  btn.setAttribute("aria-pressed", String(!collapsed));
}
function initKpiToggle() {
  const collapsed = dashHidden();
  applyKpiCollapsed(collapsed);
  $("#kpiToggle").addEventListener("click", () => {
    const now = !dashHidden();
    applyKpiCollapsed(now);
    try { localStorage.setItem("aj_dash_hidden", now ? "1" : "0"); } catch (e) {}
    if (!now) refreshDashboard();   // don't make them wait for the next poll
  });
  $("#webToggle").addEventListener("change", (e) => setWebAccess(e.target.checked));
  $("#teamworkToggle").addEventListener("change", (e) => setTeamwork(e.target.checked));
}

/* ----------------------------- events ----------------------------- */
function wireEvents() {
  const input = $("#input");
  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
  input.addEventListener("input", autosize);

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    const c = activeConv();
    if ((!text && !state.pendingFiles.length) || (c && c.streaming)) return;
    input.value = ""; autosize();
    send(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });

  $("#attachBtn").addEventListener("click", () => $("#chatFile").click());
  $("#chatFile").addEventListener("change", (e) => {
    state.pendingFiles.push(...Array.from(e.target.files || []));
    e.target.value = ""; renderChips();
  });
  initChatDragDrop();

  $("#micBtn").addEventListener("click", toggleMic);
  $("#autoSpeakBtn").addEventListener("click", () => {
    state.autoSpeak = !state.autoSpeak;
    $("#autoSpeakBtn").classList.toggle("on", state.autoSpeak);
    $("#autoSpeakBtn").setAttribute("aria-pressed", state.autoSpeak ? "true" : "false");
    if (!state.autoSpeak) stopSpeaking();
  });

  $("#newChatBtn").addEventListener("click", () => { newConversation(); closeSidebar(); $("#input").focus(); });
    const csBox = $("#convSearch");
  if (csBox) {
    csBox.addEventListener("input", () => {
      // debounced: a query per keystroke across every message is wasteful
      clearTimeout(_convSearchTimer);
      _convSearchTimer = setTimeout(
        () => searchConversations(csBox.value), 220);
    });
    csBox.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { csBox.value = ""; searchConversations(""); }
    });
  }
$("#projectFilter").addEventListener("change", async (e) => {
    state.projectFilter = e.target.value;
    await loadConversations();
    newConversation();
  });
  $("#newProjectBtn").addEventListener("click", createProject);

  document.querySelectorAll(".suggest-card").forEach(card => {
    card.addEventListener("click", () => { const q = card.getAttribute("data-q"); $("#input").value = q; send(q); });
  });

  $("#enginePill").addEventListener("click", (e) => { e.stopPropagation(); toggleEngineMenu(); });
  document.addEventListener("click", (e) => {
    const menu = $("#engineMenu");
    const pill = $("#enginePill");
    if (!menu.hidden && !menu.contains(e.target) && !pill.contains(e.target)) menu.hidden = true;
  });
  window.addEventListener("resize", () => { $("#engineMenu").hidden = true; });

  // print the build the browser is ACTUALLY running — if this doesn't match
  // the build you just deployed, you're looking at a cached script
  fetch("/api/version").then(r => r.json())
    .then(d => console.log("%cAgent Jo build " + d.build,
                           "color:#D4A24C;font-weight:600"))
    .catch(() => {});
  let _tileResize = null;
  window.addEventListener("resize", () => {
    clearTimeout(_tileResize);
    _tileResize = setTimeout(layoutTiles, 120);
  });
  applyTheme(currentTheme());
  loadSetup(true);   // a fresh install has no engine — say so immediately
  // paint the last known board first: a restart shouldn't show an empty pane
  // while the live one is being assembled
  fetch("/api/dashboard?cached=1").then(r => r.json())
    .then(d => { if (d && (d.tiles || []).length) paintDashboard(d); })
    .catch(() => {});
  initFootGroups();
  $("#enginesBtn").addEventListener("click", openEngineModal);
  $("#enableLocalBtn").addEventListener("click", enableLocalEngines);
  $("#closeEngineModal").addEventListener("click", closeEngineModal);
  $("#engineModal").addEventListener("click", (e) => { if (e.target === $("#engineModal")) closeEngineModal(); });
  $("#saveEngineBtn").addEventListener("click", saveEngine);
  $("#testEngineBtn").addEventListener("click", testEngine);

  $("#settingsBtn").addEventListener("click", openSettings);
  $("#closeSettingsModal").addEventListener("click", closeSettings);
  $("#settingsModal").addEventListener("click", (e) => { if (e.target === $("#settingsModal")) closeSettings(); });
  $("#saveSettingsBtn").addEventListener("click", saveSettings);
  $("#resetSettingsBtn").addEventListener("click", resetSettings);

  $("#documentsBtn").addEventListener("click", openDocuments);
  $("#closeDocumentsModal").addEventListener("click", closeDocuments);
  $("#documentsModal").addEventListener("click", (e) => { if (e.target === $("#documentsModal")) closeDocuments(); });
  $("#docDrop").addEventListener("click", () => $("#docFile").click());
  $("#docFile").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
  ["dragenter", "dragover"].forEach(ev => $("#docDrop").addEventListener(ev, (e) => { e.preventDefault(); $("#docDrop").classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => $("#docDrop").addEventListener(ev, (e) => { e.preventDefault(); $("#docDrop").classList.remove("drag"); }));
  $("#docDrop").addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files); });
  const dsB = $("#docSurveyBtn");
  if (dsB) dsB.addEventListener("click", async () => {
    const p = ($("#docPath") || {}).value.trim();
    if (!p) { setDocStatus("Enter a folder path.", "err"); return; }
    dsB.disabled = true;
    setDocStatus("Counting…", "");
    try {
      const r = await fetch("/api/documents/survey", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: p }) });
      const d = await r.json();
      if (!r.ok) { setDocStatus(errText(d, "Could not read that."), "err"); }
      else {
        // the count before the commitment: a folder that looks like it hangs
        // is usually one nobody looked at first
        setDocStatus(d.verdict.replace(/\*\*/g, ""),
                     d.estimated_chunks > 40000 ? "warn" : "ok");
        showDocNote(
          (d.by_extension || []).map(e => `${e.ext} ${e.files}`).join(" · ")
          + (d.note ? "  —  " + d.note : ""));
      }
    } catch (e) { setDocStatus("Could not read that.", "err"); }
    dsB.disabled = false;
  });
  $("#docPathBtn").addEventListener("click", ingestPath);
  $("#wfAddBtn").addEventListener("click", addWatchedFolder);
  $("#wfScanBtn").addEventListener("click", scanWatchedFolders);
  $("#docClearBtn").addEventListener("click", clearDocuments);
  $("#docSearchBtn").addEventListener("click", searchDocuments);
  $("#docSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchDocuments(); } });
  $("#docPath").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ingestPath(); } });

  $("#memoryBtn").addEventListener("click", openMemory);
  $("#closeMemoryModal").addEventListener("click", closeMemory);
  $("#memoryModal").addEventListener("click", (e) => { if (e.target === $("#memoryModal")) closeMemory(); });
  $("#memTabMemories").addEventListener("click", () => switchMemTab("memories"));
  $("#memTabSkills").addEventListener("click", () => switchMemTab("skills"));
  $("#memSearchBtn").addEventListener("click", searchMemories);
  $("#memClearSearch").addEventListener("click", () => { $("#memSearch").value = ""; loadMemory(); });
  $("#memSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchMemories(); } });
  $("#memAddBtn").addEventListener("click", addMemory);
  $("#memContent").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addMemory(); } });
  $("#skAddBtn").addEventListener("click", addSkill);

  $("#permsBtn").addEventListener("click", openPerms);
  $("#closePermsModal").addEventListener("click", closePerms);
  $("#permsModal").addEventListener("click", (e) => { if (e.target === $("#permsModal")) closePerms(); });
  $("#permAddBtn").addEventListener("click", addPerm);
  $("#permPattern").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addPerm(); } });

  $("#schedBtn").addEventListener("click", openSched);
  $("#closeSchedModal").addEventListener("click", closeSched);
  $("#schedModal").addEventListener("click", (e) => { if (e.target === $("#schedModal")) closeSched(); });
  $("#schedTabSchedules").addEventListener("click", () => switchSchedTab("schedules"));
  $("#schedTabTasks").addEventListener("click", () => { switchSchedTab("tasks"); loadTasks(); });
  $("#arSaveBtn").addEventListener("click", saveAutoResume);
  $("#arPreviewBtn").addEventListener("click", previewAutoResume);
  $("#arRunBtn").addEventListener("click", runAutoResume);
  $("#arPauseBtn").addEventListener("click", pauseAutoResume);
  $("#schKind").addEventListener("change", updateSchedFields);
  $("#schCreateBtn").addEventListener("click", createSchedule);

  $("#outreachBtn").addEventListener("click", openOutreach);
  $("#closeOutreachModal").addEventListener("click", closeOutreach);
  $("#autonomyBtn").addEventListener("click", openAutonomy);
  $("#closeAutonomyModal").addEventListener("click", closeAutonomy);
  $("#autonomyModal").addEventListener("click", (e) => { if (e.target === $("#autonomyModal")) closeAutonomy(); });
  $("#issuesBtn").addEventListener("click", openIssues);
  const mcpFind = $("#mcpDiscoverBtn");
  if (mcpFind) mcpFind.addEventListener("click", async () => {
    mcpFind.disabled = true;
    const was = mcpFind.textContent;
    mcpFind.textContent = "Searching\u2026";
    const host = $("#mcpDiscoverList");
    try {
      const stale = ($("#mcpStale") || {}).checked ? "?include_stale=true" : "";
      const d = await fetch("/api/mcp/discover" + stale).then(r => r.json());
      host.hidden = false;
      host.innerHTML = "";
      $("#mcpDiscoverCount").textContent =
        `${d.total} server(s) · ${d.installed.count} already configured`;
      const already = new Set(d.installed.packages || []);
      (d.found || []).forEach(s => {
        const row = el("div", "auto-row");
        const t = el("div", "auto-row-title");
        t.textContent = s.title || s.name;
        if (s.official) {
          const b = el("span", "job-tag good");
          b.textContent = "official";
          b.style.marginLeft = "7px";
          t.appendChild(b);
        }
        const m2 = el("div", "log-meta");
        m2.textContent = `${s.description || s.name} · v${s.version} · `
          + `updated ${s.updated}`;
        const g = el("div", "log-meta");
        g.textContent = `It would be able to ${s.grants}. Needs: `
          + (s.needs || []).join(", ");
        row.append(t, m2, g);
        const acts = el("div", "mcp-actions");
        acts.style.marginTop = "6px";
        if (already.has(s.name)) {
          const done = el("span", "log-meta");
          done.textContent = "already configured";
          acts.appendChild(done);
        } else {
          const add = el("button", "btn ghost btn-mini");
          add.textContent = "Add (stays off)";
          add.addEventListener("click", async () => {
            add.disabled = true;
            const r = await fetch("/api/mcp/install", { method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ package: s }) });
            const dd = await r.json();
            if (!r.ok) { toast(derrText(d, "Could not add it."), "warn"); }
            else {
              toast(dd.note, "ok");
              add.textContent = "added — off";
              loadMcp();
            }
            add.disabled = false;
          });
          acts.appendChild(add);
        }
        if (s.npm) {
          const a = el("a", "btn ghost btn-mini");
          a.href = s.npm; a.target = "_blank"; a.rel = "noopener";
          a.textContent = "Read its page";
          acts.appendChild(a);
        }
        row.appendChild(acts);
        host.appendChild(row);
      });
      if (!(d.found || []).length) {
        emptyPane(host, "Nothing came back",
          (d.errors || []).join("; ") || "The registry returned no servers.");
      } else {
        const note = el("div", "log-meta");
        note.style.marginTop = "8px";
        note.textContent = d.note;
        host.appendChild(note);
      }
    } catch (e) { toast("Could not reach the registry.", "bad"); }
    mcpFind.textContent = was; mcpFind.disabled = false;
  });
  $("#mcpBtn").addEventListener("click", openMcp);
  const saveAuto = async () => {
    const body = {
      enabled: $("#jobsAutoOn").checked,
      dry_run: $("#jobsDryRun").checked,
      min_score: parseInt($("#jobsMinScore").value || "75", 10),
      daily_cap: parseInt($("#jobsCap").value || "5", 10),
      require_clean_check: $("#jobsCleanOnly").checked };
    try {
      await fetch("/api/jobs/auto", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      $("#jobsStatus").textContent = body.enabled
        ? (body.dry_run
            ? `Auto-apply ON in rehearsal — it will prepare applications and send nothing. Min fit ${body.min_score}, cap ${body.daily_cap}/day.`
            : `Auto-apply ON and LIVE — it will send applications for you. Min fit ${body.min_score}, cap ${body.daily_cap}/day, clean drafts only: ${body.require_clean_check}.`)
        : "Auto-apply off.";
    } catch (e) { $("#jobsStatus").textContent = "Could not save."; }
  };
  ["#jobsAutoOn", "#jobsDryRun", "#jobsMinScore", "#jobsCap", "#jobsCleanOnly"]
    .forEach(sel => $(sel).addEventListener("change", saveAuto));
  $("#jobsAutoRunBtn").addEventListener("click", async () => {
    const b = $("#jobsAutoRunBtn");
    b.disabled = true; b.textContent = "Running…";
    $("#jobsStatus").textContent = "Scoring, drafting and applying where the gates allow…";
    try {
      const eng = $("#jobsEngine");
      const r = await fetch("/api/jobs/auto/run", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine: eng ? eng.value : "" }) });
      const d = await r.json();
      if (!r.ok) { $("#jobsStatus").textContent = errText(d, "Run failed."); }
      else {
        const lines = [`${d.sent.length} application(s) ${d.dry_run ? "REHEARSED (nothing sent)" : "sent"}, ${d.held.length} held for you.`];
        d.sent.forEach(s => lines.push(`  → ${s.title} @ ${s.company} (${s.to})`));
        if (d.held.length) lines.push("", "Waiting on you:");
        d.held.forEach(h => lines.push(`  • ${h.title} @ ${h.company} — ${h.reason}`));
        if (d.common_error) {
          lines.push("", `All ${(d.errors || []).length} failed the same way: ${d.common_error}`);
        } else {
          (d.errors || []).forEach(e => lines.push(`  ! ${e}`));
        }
        $("#jobsDraft").textContent = lines.join("\n");
        $("#jobsStatus").textContent = "";
        loadJobs();
      }
    } catch (e) { $("#jobsStatus").textContent = "Run failed."; }
    b.disabled = false; b.textContent = "Run auto-apply";
  });
  [["Find", "find"], ["Tracked", "tracked"], ["Claims", "claims"],
    ["Results", "results"], ["Auto", "auto"],
    ["Where", "where"]].forEach(([id, key]) => {
    const b = $("#jobsTab" + id);
    if (b) b.addEventListener("click", () => jobsTab(key));
  });
  const autoP = $("#jobsAutopilotBtn");
  if (autoP) autoP.addEventListener("click", async () => {
    autoP.disabled = true;
    try {
      const d = await fetch("/api/jobs/autopilot", { method: "POST" })
        .then(r => r.json());
      toast(d.note || "Daily run set up.", "ok");
      loadJobs();
    } catch (e) { toast("Could not set that up.", "bad"); }
    autoP.disabled = false;
  });
  $("#jobsUrlBtn").addEventListener("click", () => searchThisPage(false));
  $("#jobsUrlBrowserBtn").addEventListener("click", () => {
    $("#jobsUrl").value = ($("#jobsQuery") || {}).value || "";
    searchThisPage(true);
  });
  const alertOut = () => $("#jobsAlertOut");
  const pasteB = $("#jobsAlertPasteBtn");
  if (pasteB) pasteB.addEventListener("click", async () => {
    const raw = ($("#jobsAlertPaste") || {}).value || "";
    if (!raw.trim()) { toast("Paste an alert email first.", "warn"); return; }
    pasteB.disabled = true;
    try {
      const r = await fetch("/api/jobs/alerts/paste", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw }) });
      const d = await r.json();
      if (!r.ok) { toast(errText(d, "Couldn't read that."), "bad"); }
      else {
        toast(`${d.board}: found ${d.count}, tracked ${d.added} new.`, "ok");
        const box = alertOut();
        if (box) {
          box.innerHTML = "";
          d.roles.forEach(x => {
            const line = el("div", "log-meta");
            line.textContent = x.title + (x.company ? " — " + x.company : "");
            box.appendChild(line);
          });
        }
        $("#jobsAlertPaste").value = "";
        loadJobs();
      }
    } catch (e) { toast("Couldn't read that.", "bad"); }
    pasteB.disabled = false;
  });
  const folderB = $("#jobsAlertFolderBtn");
  if (folderB) folderB.addEventListener("click", async () => {
    folderB.disabled = true;
    try {
      const r = await fetch("/api/jobs/alerts/folder", { method: "POST" });
      const d = await r.json();
      toast(r.ok
        ? `Read ${d.files} file(s): tracked ${d.added} new role(s).`
        : errText(d, "Nothing to read."), r.ok ? "ok" : "warn");
      if (r.ok) loadJobs();
    } catch (e) { toast("Could not read the folder.", "bad"); }
    folderB.disabled = false;
  });
  const guideB = $("#jobsAlertGuideBtn");
  if (guideB) guideB.addEventListener("click", async () => {
    try {
      const d = await fetch("/api/jobs/alerts/guide").then(r => r.json());
      const box = alertOut();
      if (!box) return;
      box.innerHTML = "";
      const why = el("div", "log-meta");
      why.textContent = d.why;
      box.appendChild(why);
      (d.guide || []).forEach(g => {
        const line = el("div", "log-meta");
        line.textContent = `${g.board}: ${g.how}`;
        box.appendChild(line);
      });
    } catch (e) { toast("Could not load that.", "bad"); }
  });
  const findB = $("#jobsFindBoardsBtn");
  if (findB) findB.addEventListener("click", async () => {
    findB.disabled = true;
    const was = findB.textContent;
    findB.textContent = "Checking boards…";
    toast("Checking each board actually returns jobs — this takes a moment.");
    try {
      const r = await fetch("/api/jobs/boards/auto", { method: "POST" });
      const d = await r.json();
      if (!r.ok) { toast(errText(d, "Could not do that."), "bad"); }
      else {
        const names = (d.added || []).map(a => a.name).join(", ");
        toast(d.added.length
          ? `Added ${d.added.length}: ${names}. ${d.rejected.length} didn't return anything.`
          : `None of the ${d.tried} boards tried returned jobs — see the list for why.`,
          d.added.length ? "ok" : "warn");
        const host = $("#jobsSourceList");
        if (host && (d.rejected || []).length) {
          const box = el("div", "log-meta");
          box.style.marginTop = "8px";
          box.textContent = "Not added: " + d.rejected
            .map(x => `${x.name} (${x.why})`).join("; ");
          host.appendChild(box);
        }
        loadJobSearchConfig();
      }
    } catch (e) { toast("Could not do that.", "bad"); }
    findB.textContent = was; findB.disabled = false;
  });
  const pruneBtn = $("#jobsPruneBtn");
  if (pruneBtn) pruneBtn.addEventListener("click", async () => {
    pruneBtn.disabled = true;
    try {
      const r = await fetch("/api/jobs/prune", { method: "POST" });
      const d = await r.json();
      $("#jobsStatus").textContent = r.ok
        ? `Removed ${d.removed} entr(ies) that were never vacancies: ${(d.titles || []).slice(0, 4).join(", ")}${d.removed > 4 ? "…" : ""}`
        : errText(d, "Nothing to remove.");
      loadJobs();
    } catch (e) { $("#jobsStatus").textContent = "Could not clean those up."; }
    pruneBtn.disabled = false;
  });
  const archB = $("#jobsArchiveBtn");
  if (archB) archB.addEventListener("click", async () => {
    archB.disabled = true;
    try {
      const r = await fetch("/api/jobs/archive", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ applied_before_days: 0 }) });
      const d = await r.json();
      toast(r.ok
        ? `Archived ${d.moved}: ${(d.titles || []).slice(0, 3).join(", ")}${d.moved > 3 ? "…" : ""}. Kept, not deleted.`
        : errText(d, "Nothing to archive."), r.ok ? "ok" : "");
      if (r.ok) loadJobs();
    } catch (e) { toast("Could not archive those.", "bad"); }
    archB.disabled = false;
  });
  const sweepB = $("#jobsSweepBtn");
  if (sweepB) sweepB.addEventListener("click", async () => {
    sweepB.disabled = true;
    const was = sweepB.textContent;
    sweepB.textContent = "Checking…";
    try {
      const d = await fetch("/api/jobs/sweep", { method: "POST" })
        .then(r => r.json());
      toast(d.closed.length
        ? `Closed ${d.closed.length}: ${d.closed.map(c => c.title).join(", ")}. `
          + `${d.still_open} still open.`
        : `None have closed. ${d.still_open} still open`
          + (d.unknown.length ? `, ${d.unknown.length} couldn't be checked.` : "."),
        d.closed.length ? "ok" : "");
      loadJobs();
    } catch (e) { toast("Could not check those.", "bad"); }
    sweepB.textContent = was; sweepB.disabled = false;
  });
  const archChip = $("#jobsChipArchive");
  if (archChip) archChip.addEventListener("click", async () => {
    _showArchive = !_showArchive;
    archChip.classList.toggle("is-on", _showArchive);
    if (_showArchive) loadArchive(); else loadJobs();
  });
  [["jobsChipEmail", "email"], ["jobsChipStrong", "strong"],
    ["jobsChipUnscored", "unscored"], ["jobsChipOpen", "openonly"]]
    .forEach(([id, key]) => {
    const c = $("#" + id);
    if (!c) return;
    c.addEventListener("click", () => {
      _jobFilters[key] = !_jobFilters[key];
      c.classList.toggle("is-on", _jobFilters[key]);
      loadJobs();
    });
  });
  const sortSel = $("#jobsSort");
  if (sortSel) sortSel.addEventListener("change", () => {
    _jobSort = sortSel.value;
    loadJobs();
  });
  const bs = $("#jobsBulkScore");
  if (bs) bs.addEventListener("click", () => bulkDo("score"));
  const br = $("#jobsBulkRemove");
  if (br) br.addEventListener("click", () => bulkDo("remove"));
  const bc = $("#jobsBulkClear");
  if (bc) bc.addEventListener("click", () => {
    _jobPicked.clear(); renderBulkBar(); loadJobs();
  });
  const clearBtn = $("#jobsClearBtn");
  if (clearBtn) clearBtn.addEventListener("click", async () => {
    const stage = ($("#jobsStageFilter") || {}).value || "";
    if (!stage) {
      $("#jobsStatus").textContent =
        "Choose a stage on the left first — this won't clear everything at once.";
      return;
    }
    clearBtn.disabled = true;
    try {
      const r = await fetch("/api/jobs/clear", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage, forget: false }) });
      const d = await r.json();
      $("#jobsStatus").textContent = r.ok
        ? `Removed ${d.removed} role(s) at stage “${stage}”. These can be found again.`
        : errText(d, "Nothing to clear.");
      loadJobs();
    } catch (e) { $("#jobsStatus").textContent = "Could not clear those."; }
    clearBtn.disabled = false;
  });
  // moving through a list of forty roles with the mouse is the slow part
  const jobsKeys = (e) => {
    const m = $("#jobsModal");
    if (!m || m.hidden) return;
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const pane = $("#jobsPaneTracked").hidden ? $("#jobsResults") : $("#jobsList");
    const rows = Array.from(pane.children).filter(c => c.classList
      && c.classList.contains("job-row"));
    if (!rows.length) return;
    e.preventDefault();
    const at = rows.findIndex(r => r.classList.contains("is-on"));
    const next = e.key === "ArrowDown"
      ? Math.min(rows.length - 1, at + 1)
      : Math.max(0, at < 0 ? 0 : at - 1);
    rows[next].click();
    if (rows[next].scrollIntoView) {
      rows[next].scrollIntoView({ block: "nearest" });
    }
  };
  document.addEventListener("keydown", jobsKeys);

  const stageF = $("#jobsStageFilter");
  if (stageF) stageF.addEventListener("change", loadJobs);
  $("#jobsQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#jobsSearchBtn").click();
  });
  const looksLikeUrl = (s) => /^(https?:\/\/|www\.)/i.test((s || "").trim())
    || /^[\w-]+(\.[\w-]+){1,}\/\S*$/.test((s || "").trim());
  const qBox = $("#jobsQuery");
  const syncSearchMode = () => {
    const isUrl = looksLikeUrl(qBox.value);
    $("#jobsSearchBtn").textContent = isUrl ? "Read this page" : "Search";
    const br = $("#jobsUrlBrowserBtn");
    if (br) br.hidden = !isUrl;
  };
  if (qBox) qBox.addEventListener("input", syncSearchMode);
  $("#jobsSearchBtn").addEventListener("click", () => {
    // Two boxes asked you to know which kind of thing you had. One box that
    // notices is the same feature with less to read.
    if (looksLikeUrl(qBox.value)) {
      $("#jobsUrl").value = qBox.value.trim();
      searchThisPage(false);
    } else {
      searchJobs();
    }
  });
  $("#jobsQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") searchJobs(); });
  ["#jobsExclude", "#jobsLocations", "#jobsRemoteOnly", "#jobsNeedEmail"]
    .forEach(sel => $(sel).addEventListener("change", saveJobSearchConfig));
  $("#jobsSrcAdd").addEventListener("click", async () => {
    const name = $("#jobsSrcName").value.trim(), url = $("#jobsSrcUrl").value.trim();
    if (!name || !url) { $("#jobsStatus").textContent = "A board needs a name and a URL."; return; }
    try {
      const r = await fetch("/api/jobs/sources", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, url, kind: $("#jobsSrcKind").value }) });
      const d = await r.json();
      $("#jobsStatus").textContent = r.ok ? `Added ${name}.` : errText(d, "Could not add that.");
      if (r.ok) { $("#jobsSrcName").value = ""; $("#jobsSrcUrl").value = ""; loadJobSearchConfig(); }
    } catch (e) { $("#jobsStatus").textContent = "Could not add that."; }
  });
  $("#jobsBtn").addEventListener("click", openJobs);
  $("#closeJobsModal").addEventListener("click", closeJobs);
  $("#jobsModal").addEventListener("click", (e) => { if (e.target === $("#jobsModal")) closeJobs(); });
  $("#jobsDaily").addEventListener("change", async (e) => {
    try {
      const d = await fetch("/api/jobs/schedule", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: e.target.checked }) }).then(r => r.json());
      e.target.checked = !!d.daily;
      $("#jobsStatus").textContent = d.daily
        ? "Daily scan on — 07:00. It finds, scores and drafts. It never sends."
        : "Daily scan off.";
    } catch (err) { e.target.checked = !e.target.checked; }
  });
  $("#closeSetupModal").addEventListener("click", closeSetup);
  $("#setupModal").addEventListener("click", (e) => { if (e.target === $("#setupModal")) closeSetup(); });
  $("#setupSaveBtn").addEventListener("click", saveSetupKey);
  $("#setupKey").addEventListener("keydown", (e) => { if (e.key === "Enter") saveSetupKey(); });
  $("#setupRecheckBtn").addEventListener("click", async () => {
    $("#setupStatus").textContent = "Checking\u2026";
    await fetch("/api/setup/recheck", { method: "POST" });
    const s = await loadSetup();
    if (s && s.configured) { $("#setupStatus").textContent = "Ready."; setTimeout(closeSetup, 900); }
  });
  $("#dashCustomise").addEventListener("click", () => {
    const box = $("#dashEdit");
    box.hidden = !box.hidden;
    if (!box.hidden) renderDashEdit();
  });
  $("#dashEditDone").addEventListener("click", () => { $("#dashEdit").hidden = true; });
  // phone drawer: opens the sidebar, and closes on any navigation so the
  // panel you tapped isn't hidden behind it
  const navT = $("#navToggle");
  if (navT) {
    navT.addEventListener("click", () => {
      const open = document.body.classList.toggle("nav-open");
      navT.setAttribute("aria-expanded", String(open));
    });
    document.querySelectorAll(".foot-btn").forEach((b) => {
      b.addEventListener("click", () => {
        document.body.classList.remove("nav-open");
        navT.setAttribute("aria-expanded", "false");
      });
    });
  }
  // Ctrl+K / Cmd+K from anywhere
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      const bd = $("#paletteBackdrop");
      if (bd && bd.hidden) openPalette(); else closePalette();
      return;
    }
    const bd = $("#paletteBackdrop");
    if (!bd || bd.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); closePalette(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = _palHits.length;
      if (!n) return;
      _palAt = e.key === "ArrowDown"
        ? Math.min(n - 1, _palAt + 1) : Math.max(0, _palAt - 1);
      renderPalette($("#paletteInput").value);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = _palHits[_palAt];
      if (hit) { closePalette(); hit.run(); }
    }
  });
  $("#paletteInput").addEventListener("input", () => {
    _palAt = 0;
    renderPalette($("#paletteInput").value);
  });
  $("#paletteBackdrop").addEventListener("click", (e) => {
    if (e.target === $("#paletteBackdrop")) closePalette();
  });
  const palHint = $("#paletteHint");
  if (palHint) palHint.addEventListener("click", openPalette);
  const mlBtn = $("#modelLabBtn");
  if (mlBtn) mlBtn.addEventListener("click", () => {
    const box = $("#modelLab");
    box.hidden = !box.hidden;
    if (!box.hidden) modelFit();
  });
  const mlClose = $("#modelLabClose");
  if (mlClose) mlClose.addEventListener("click", () => {
    $("#modelLab").hidden = true;
  });
  const fitBtn = $("#modelFitBtn");
  if (fitBtn) fitBtn.addEventListener("click", modelFit);
  const derBtn = $("#modelDeriveBtn");
  if (derBtn) derBtn.addEventListener("click", async () => {
    const name = ($("#modelNewName") || {}).value || "";
    const base = ($("#modelBase") || {}).value || "";
    if (!name.trim() || !base.trim()) {
      toast("Give it a name and say which model to build on.", "warn");
      return;
    }
    derBtn.disabled = true;
    const was = derBtn.textContent;
    derBtn.textContent = "Creating…";
    try {
      const r = await fetch("/api/models/derive", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, base,
          system: ($("#modelSystem") || {}).value || "",
          temperature: parseFloat(($("#modelTemp") || {}).value) || null,
          context: parseInt(($("#modelCtx") || {}).value, 10) || null })});
      const d = await r.json();
      if (!r.ok) { toast(errText(d, "Could not create it."), "bad"); }
      else {
        toast(d.note, "ok");
        const out = $("#modelOut");
        if (out) {
          out.innerHTML = "";
          const line = el("div", "log-meta");
          line.textContent = `${d.name} built on ${d.base} — recipe at ${d.modelfile}`;
          out.appendChild(line);
        }
      }
    } catch (e) { toast("Could not create it.", "bad"); }
    derBtn.textContent = was; derBtn.disabled = false;
  });
  const trBtn = $("#modelTrainBtn");
  if (trBtn) trBtn.addEventListener("click", async () => {
    const base = ($("#modelBase") || {}).value || "qwen3:8b";
    const vram = parseFloat(($("#modelVram") || {}).value) || 24;
    const m = /(\d+(?:\.\d+)?)\s*b/i.exec(base);
    try {
      const d = await fetch("/api/models/training-plan", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base, vram_gb: vram,
          params_b: m ? parseFloat(m[1]) : 8,
          out_name: ($("#modelNewName") || {}).value || "my-model" })})
        .then(r => r.json());
      const out = $("#modelOut");
      out.innerHTML = "";
      const add = (txt, strong) => {
        const line = el("div", strong ? "" : "log-meta");
        line.textContent = txt;
        out.appendChild(line);
      };
      add(d.verdict, true);
      add(`Roughly ${d.rough_hours} hour(s) of GPU time.`);
      add(d.dataset.how_many);
      add(d.dataset.warning);
      add(d.honest_note);
      d.steps.forEach((s, i) => add(`${i + 1}. ${s}`));
      const pre = el("pre");
      pre.style.cssText = "white-space:pre-wrap;font-size:11.5px;margin-top:8px";
      pre.textContent = d.script;
      out.appendChild(pre);
    } catch (e) { toast("Could not work that out.", "bad"); }
  });
  const mlB = $("#mlBtn");
  if (mlB) mlB.addEventListener("click", openMl);
  $("#closeMlModal").addEventListener("click", closeMl);
  [["Data", "data"], ["Build", "build"], ["Engineer", "engineer"],
   ["Features", "features"], ["Test", "test"], ["Predict", "predict"],
   ["Saved", "saved"]].forEach(([id, key]) => {
    const b = $("#mlTab" + id);
    if (b) b.addEventListener("click", () => mlTab(key));
  });
  const mlLookB = $("#mlLookBtn");
  if (mlLookB) mlLookB.addEventListener("click", mlLook);
  const mlTrainB = $("#mlTrainBtn");
  if (mlTrainB) mlTrainB.addEventListener("click", mlTrain);
  const mlColsB = $("#mlColsBtn");
  if (mlColsB) mlColsB.addEventListener("click", mlColumns);
  const mlFeatB = $("#mlFeatBtn");
  if (mlFeatB) mlFeatB.addEventListener("click", mlFeatures);
  const mlTestB = $("#mlTestBtn");
  if (mlTestB) mlTestB.addEventListener("click", mlScoreHidden);
  const mlPredB = $("#mlPredictBtn");
  if (mlPredB) mlPredB.addEventListener("click", mlPredict);
  const cmBtn = $("#codemapBtn");
  if (cmBtn) cmBtn.addEventListener("click", openCodemap);
  $("#closeCodemapModal").addEventListener("click", closeCodemap);
  [["Findings", "findings"], ["Diagram", "diagram"]].forEach(([id, key]) => {
    const b = $("#cmTab" + id);
    if (b) b.addEventListener("click", () => cmTab(key));
  });
  const zoomBy = (f) => {
    if (!_cmSvg) return;
    _cmSvg._zoom(f, 0, 0);
  };
  const zi = $("#cmZoomIn");
  if (zi) zi.addEventListener("click", () => zoomBy(0.8));
  const zo = $("#cmZoomOut");
  if (zo) zo.addEventListener("click", () => zoomBy(1.25));
  const zr = $("#cmZoomReset");
  if (zr) zr.addEventListener("click", () => { if (_cmSvg) _cmSvg._reset(); });
  const cmEx = $("#cmExport");
  if (cmEx) cmEx.addEventListener("change", async () => {
    const fmt = cmEx.value;
    if (!fmt) return;
    cmEx.value = "";
    try {
      const d = await fetch("/api/codemap/export", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: ($("#cmFolder") || {}).value || ".",
                               format: fmt, module: _cmFocus }) })
        .then(r => r.json());
      toast(`Saved to ${d.path} — ${d.how}`, "ok");
      const box = $("#cmDetail");
      if (box) {
        box.innerHTML = "";
        const h = el("div", "detail-head"); h.textContent = `${fmt} export`;
        const s = el("div", "detail-sub"); s.textContent = d.how;
        const pre = el("pre");
        pre.style.cssText = "white-space:pre-wrap;font-size:11.5px";
        pre.textContent = d.text;
        box.append(h, s, pre);
        cmTab("findings");
      }
    } catch (e) { toast("Could not export that.", "bad"); }
  });
  const cmScan = $("#cmScanBtn");
  if (cmScan) cmScan.addEventListener("click", runCodemap);
  const cmIn = $("#cmFolder");
  if (cmIn) cmIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runCodemap();
  });
  $("#phoneBtn").addEventListener("click", openPhone);
  $("#closePhoneModal").addEventListener("click", closePhone);
  $("#phoneModal").addEventListener("click", (e) => { if (e.target === $("#phoneModal")) closePhone(); });
  $("#phoneCopy").addEventListener("click", () => {
    if (!_phoneUrl) return;
    navigator.clipboard.writeText(_phoneUrl);
    $("#phoneUrl").textContent = _phoneUrl + " — copied";
  });
  $("#presenterBtn").addEventListener("click", startPresenter);
  $("#presNext").addEventListener("click", () => presenterGo(1));
  $("#presPrev").addEventListener("click", () => presenterGo(-1));
  $("#presExit").addEventListener("click", exitPresenter);
  $("#presPlay").addEventListener("click", () => {
    _pres.playing = !_pres.playing;
    $("#presPlay").textContent = _pres.playing ? "❚❚ Pause" : "▶ Auto";
    if (_pres.playing) {
      queueAuto((_pres.scenes[_pres.i] || {}).seconds || 14);
    } else { stopAuto(); }
  });
  $("#presNotes").addEventListener("click", () => {
    _pres.notes = !_pres.notes;
    showScene(_pres.i);
  });
  $("#tourBtn").addEventListener("click", openTour);
  $("#closeTourModal").addEventListener("click", closeTour);
  $("#tourModal").addEventListener("click", (e) => { if (e.target === $("#tourModal")) closeTour(); });
  $("#challengesBtn").addEventListener("click", openChallenges);
  $("#closeChallengesModal").addEventListener("click", closeChallenges);
  $("#challengesModal").addEventListener("click", (e) => { if (e.target === $("#challengesModal")) closeChallenges(); });
  $("#chScanBtn").addEventListener("click", () => scanChallenges(false));
  $("#chDeepBtn").addEventListener("click", () => scanChallenges(true));
  $("#skillsBtn").addEventListener("click", openSkills);
  $("#closeSkillsModal").addEventListener("click", closeSkills);
  $("#skillsModal").addEventListener("click", (e) => { if (e.target === $("#skillsModal")) closeSkills(); });
  $("#capsBtn").addEventListener("click", openCaps);
  $("#closeCapsModal").addEventListener("click", closeCaps);
  $("#capsModal").addEventListener("click", (e) => { if (e.target === $("#capsModal")) closeCaps(); });
  $("#healthBtn").addEventListener("click", openHealth);
  $("#closeHealthModal").addEventListener("click", closeHealth);
  $("#healthModal").addEventListener("click", (e) => { if (e.target === $("#healthModal")) closeHealth(); });
  $("#healthRefreshBtn").addEventListener("click", loadHealth);
  $("#healthCopyBtn").addEventListener("click", () => {
    if (!_healthLast) return;
    const d = _healthLast;
    const lines = [`${(window.__brand || 'Symbolic Synapse')} — Agent Jo health — ${d.at} — build ${d.build}`, ""];
    (d.checks || []).forEach(c => {
      lines.push(`[${c.state.toUpperCase()}] ${c.group} / ${c.name}: ${c.detail}`);
      if (c.fix) lines.push(`    fix: ${c.fix}`);
    });
    navigator.clipboard.writeText(lines.join("\n"));
    $("#healthInfo").textContent = "Report copied — paste it into Claude if something needs diagnosing.";
  });
  $("#backupBtn").addEventListener("click", openBackup);
  $("#closeBackupModal").addEventListener("click", closeBackup);
  $("#backupModal").addEventListener("click", (e) => { if (e.target === $("#backupModal")) closeBackup(); });
  $("#backupNowBtn").addEventListener("click", makeBackup);
  $("#backupNightly").addEventListener("change", async (e) => {
    try {
      const d = await fetch("/api/backups/schedule", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: e.target.checked }) }).then(r => r.json());
      e.target.checked = !!d.nightly;
      $("#backupStatus").textContent = d.nightly
        ? "Nightly backup on — runs at 23:30, keeps the 7 most recent."
        : "Nightly backup off.";
    } catch (err) { e.target.checked = !e.target.checked; }
  });
  $("#crewBtn").addEventListener("click", openCrew);
  $("#closeCrewModal").addEventListener("click", closeCrew);
  $("#crewModal").addEventListener("click", (e) => { if (e.target === $("#crewModal")) closeCrew(); });
  $("#crewRunBtn").addEventListener("click", runCrew);
  $("#crewTask").addEventListener("keydown", (e) => { if (e.key === "Enter") runCrew(); });
  $("#trendsBtn").addEventListener("click", openTrends);
  $("#closeTrendsModal").addEventListener("click", closeTrends);
  $("#trendsModal").addEventListener("click", (e) => { if (e.target === $("#trendsModal")) closeTrends(); });
  $("#trendScanBtn").addEventListener("click", scanTrends);
  $("#trendResumeBtn").addEventListener("click", async () => {
    const b = $("#trendResumeBtn"), sel = $("#trendEngine");
    b.disabled = true; b.textContent = "Resuming…";
    $("#trendStatus").textContent = "Continuing from where it stopped…";
    try {
      const r = await fetch("/api/trends/resume", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine: sel ? sel.value : "" }) });
      const d = await r.json();
      if (!r.ok) $("#trendStatus").textContent = errText(d, "Resume failed.");
      else renderTrendReport(d.report || {});
    } catch (e) { $("#trendStatus").textContent = "Resume failed."; }
    b.disabled = false; b.textContent = "Resume";
  });
  $("#trendEngine").addEventListener("change", async (e) => {
    try {
      await fetch("/api/trends/engine", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine: e.target.value }) });
      $("#trendStatus").textContent =
        `Digest engine set to ${e.target.value} — used for manual and weekly scans.`;
    } catch (err) { /* non-fatal */ }
  });
  $("#trendWeekly").addEventListener("change", async (e) => {
    try {
      const d = await fetch("/api/trends/schedule", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: e.target.checked }) }).then(r => r.json());
      e.target.checked = !!d.weekly;
      $("#trendStatus").textContent = d.weekly
        ? "Weekly auto-scan on — runs Mondays 07:30; findings wait here for your review."
        : "Weekly auto-scan off.";
    } catch (err) { e.target.checked = !e.target.checked; }
  });
  [["Proposal", "proposal"], ["Ideas", "ideas"],
    ["History", "history"]].forEach(([id, key]) => {
    const b = $("#selfTab" + id);
    if (b) b.addEventListener("click", () => selfTab(key));
  });
  $("#selfBtn").addEventListener("click", openSelf);
  $("#closeSelfModal").addEventListener("click", closeSelf);
  $("#selfModal").addEventListener("click", (e) => { if (e.target === $("#selfModal")) closeSelf(); });
  $("#selfApplyBtn").addEventListener("click", applySelf);
  $("#selfDiscardBtn").addEventListener("click", discardSelf);
  $("#undoBtn").addEventListener("click", openUndo);
  $("#closeUndoModal").addEventListener("click", closeUndo);
  $("#undoModal").addEventListener("click", (e) => { if (e.target === $("#undoModal")) closeUndo(); });
  $("#auditBtn").addEventListener("click", openAudit);
  $("#closeAuditModal").addEventListener("click", closeAudit);
  $("#auditModal").addEventListener("click", (e) => { if (e.target === $("#auditModal")) closeAudit(); });
  $("#auditFilter").addEventListener("change", loadAudit);
  $("#auditTidyBtn").addEventListener("click", () => {
    const box = $("#auditTidy");
    box.hidden = !box.hidden;
    if (!box.hidden) loadAuditArchives();
  });
  $("#auditTidyDone").addEventListener("click", () => { $("#auditTidy").hidden = true; });
  $("#auditClearOldBtn").addEventListener("click", () => {
    const n = parseInt($("#auditKeepDays").value, 10);
    auditClear(Number.isFinite(n) && n > 0 ? n : 30);
  });
  $("#auditClearAllBtn").addEventListener("click", () => auditClear(0));
  $("#auditVerifyBtn").addEventListener("click", verifyAudit);
  $("#auditCsvBtn").addEventListener("click", () => copyAudit("csv"));
  $("#auditTextBtn").addEventListener("click", () => copyAudit("text"));
  $("#closeMcpModal").addEventListener("click", closeMcp);
  $("#mcpModal").addEventListener("click", (e) => { if (e.target === $("#mcpModal")) closeMcp(); });
  $("#mcpTransport").addEventListener("change", mcpTransportSync);
  $("#mcpAddBtn").addEventListener("click", addMcpServer);
  $("#closeIssuesModal").addEventListener("click", closeIssues);
  $("#issuesModal").addEventListener("click", (e) => { if (e.target === $("#issuesModal")) closeIssues(); });
  $("#issueCaptureBtn").addEventListener("click", captureIssue);
  $("#issuesCopyAllBtn").addEventListener("click", copyAllIssues);
  $("#issuesClearBtn").addEventListener("click", clearIssues);
  $("#errorsClearBtn").addEventListener("click", clearAutoErrors);
  $("#autoPauseAllBtn").addEventListener("click", pauseAllAutonomy);
  $("#autoRefreshBtn").addEventListener("click", loadAutonomy);
  $("#budgetSaveBtn").addEventListener("click", saveBudget);
  $("#outreachModal").addEventListener("click", (e) => { if (e.target === $("#outreachModal")) closeOutreach(); });
  document.querySelectorAll(".out-tab").forEach(t =>
    t.addEventListener("click", () => switchOutTab(t.getAttribute("data-tab"))));
  $("#emSaveBtn").addEventListener("click", saveEmailConfig);
  $("#emTestBtn").addEventListener("click", sendEmailTest);
  $("#qsPreviewBtn").addEventListener("click", () => quickSend(true));
  $("#qsSendBtn").addEventListener("click", () => quickSend(false));
  $("#cpDraftBtn").addEventListener("click", renderCampaignDrafts);
  $("#cpSendBtn").addEventListener("click", sendCampaign);
  $("#apSaveBtn").addEventListener("click", saveAutopilot);
  $("#apPauseBtn").addEventListener("click", pauseAutopilot);
  $("#apDryRunBtn").addEventListener("click", () => runAutopilot(true));
  $("#apRunBtn").addEventListener("click", () => runAutopilot(false));
  $("#apScheduleBtn").addEventListener("click", scheduleAutopilot);
  $("#wCreateBtn").addEventListener("click", createWatcher);
  $("#emLogBtn").addEventListener("click", loadEmailLog);

  $("#menuBtn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAllModals(); }
  });
}

document.addEventListener("DOMContentLoaded", boot);
