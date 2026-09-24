/* Agent Jo Jobs.
 *
 * Every endpoint the job search has is reachable from here. The first cut of
 * this window wired twelve of forty-seven and quietly dropped the rest —
 * scoring, drafting, CV tailoring, interview prep, portal applications,
 * claims, auto-apply controls, alerts, the archive. A redesign that removes
 * what people used is a regression wearing new clothes.
 *
 * Nothing here decides anything. The fabrication check, the fit score and the
 * auto-apply rules live on the server; this renders what they conclude.
 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = {
  // one definition of "profile ready", the server's. The window had three —
  // onboarding, the rail and the server — and they disagreed on a fresh
  // install: step one said done while the rail said empty.
  profileReady: false,
  view: "overview", roles: [], selected: null, ticked: new Set(),
  filter: "", chip: "", sort: "fit", data: null, engine: "",
};

/* --- plumbing ---------------------------------------------------------- */
async function api(path, body, method) {
  // A GET must not carry a body — the browser rejects the request outright,
  // before it's sent. The first version of this attached one to every call
  // that named a method, so every read silently failed: the pipeline showed
  // zeros under a headline counting eight roles, and the engine list was
  // empty. Reads and writes are different shapes, so build them separately.
  const verb = (method || (body === undefined ? "GET" : "POST")).toUpperCase();
  const opts = verb === "GET" ? { method: "GET" } : {
    method: verb,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  };
  const r = await fetch(path, opts);
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) {
    let m = d && (d.detail || d.error);
    if (Array.isArray(m)) m = m.map((x) => x.msg || x).join("; ");
    throw new Error(m || `${r.status}`);
  }
  return d || {};
}
const withEngine = (b) => Object.assign({ engine: S.engine }, b || {});

function toast(msg, kind) {
  const t = el("div", "toast" + (kind ? " " + kind : ""), msg);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 5600);
}

function empty(host, title, detail) {
  host.innerHTML = "";
  const e = el("div", "empty");
  e.appendChild(el("b", "", title));
  if (detail) e.appendChild(document.createTextNode(detail));
  host.appendChild(e);
}

async function busy(btn, label, fn) {
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = label;
  btn.classList.add("is-busy");          // the chrome sweep, while it works
  try { return await fn(); }
  catch (e) { toast(e.message, "bad"); }
  finally {
    btn.disabled = false; btn.textContent = was;
    btn.classList.remove("is-busy");
  }
}

// restart a pane's entrance when what it shows changes
function enterView(node) {
  if (!node || !node.classList) return;
  node.classList.remove("view-enter");
  void node.offsetWidth;
  node.classList.add("view-enter");
}

const initial = (s) => ((s || "?").trim()[0] || "?").toUpperCase();
const fitOf = (r) => (r.fit || {}).score;

/* --- navigation -------------------------------------------------------- */
const LOADERS = {};
function show(view) {
  S.view = view;
  $$(".nav").forEach((b) => b.classList.toggle("is-on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-on", v.id === "v-" + view));
  if (LOADERS[view]) LOADERS[view]();
}

/* --- the shared payload: one call feeds the rail and most views -------- */
async function refresh() {
  try {
    S.data = await api("/api/jobs");
    S.roles = S.data.roles || [];
  } catch (e) { S.data = { roles: [] }; S.roles = []; }
  railCounts();
  return S.data;
}

// What a draft leans on. Completeness is the share of these that are filled
// in — a number that means something, and a tooltip saying what's missing.
const PROFILE_PARTS = [
  ["target_roles", "roles you're after"], ["skills", "skills"],
  ["technologies", "tools"], ["employers", "where you've worked"],
  ["locations_ok", "where you'd work"], ["summary", "a summary"],
  ["achievements", "achievements"], ["full_name", "your name"],
];
function profileCompleteness(p) {
  p = p || {};
  const has = (v) => Array.isArray(v) ? v.length > 0 : !!String(v || "").trim();
  const missing = PROFILE_PARTS.filter(([k]) => !has(p[k])).map(([, label]) => label);
  return { pct: Math.round(100 * (PROFILE_PARTS.length - missing.length) / PROFILE_PARTS.length),
           missing };
}

function railCounts() {
  const set = (id, n) => { const e = $(id); if (e) e.textContent = n ? n : ""; };
  set("#nRoles", S.roles.length);
  set("#nDrafts", S.roles.filter((r) => r.stage === "held"
                                   || (r.bucket === "held")).length);
  const auto = (S.data || {}).auto || {};
  const pill = $("#railAuto");
  if (pill) {
    pill.classList.toggle("on", !!auto.enabled);
    pill.setAttribute("aria-checked", auto.enabled ? "true" : "false");
    // railCounts runs on nearly every view; a missing piece of the switch
    // must not take all of them down with it
    const txt = pill.querySelector(".pt-txt");
    if (txt) txt.textContent = auto.enabled ? (auto.dry_run ? "Test" : "On") : "Off";
    pill.title = auto.enabled
      ? (auto.dry_run ? "Auto-apply is rehearsing — nothing is sent. Click to turn off."
                      : "Auto-apply is on. Click to turn off.")
      : "Auto-apply is off. Click to turn on (rules in the Auto-apply view).";
  }
  const c = profileCompleteness((S.data || {}).profile);
  const b = $("#nProfile");
  if (b) { b.textContent = c.pct + "%"; b.classList.toggle("warn", c.pct < 60); }
  const f = $("#profileFill");
  if (f) f.style.width = c.pct + "%";
  const m = $("#profileMeter");
  if (m) m.title = c.missing.length ? "Missing: " + c.missing.join(", ") : "Complete";
}

/* --- overview ---------------------------------------------------------- */
const STEP_ART = [
  '<svg viewBox="0 0 48 48"><circle cx="17" cy="20" r="11" class="f"/><circle cx="17" cy="17" r="4"/><path d="M9.5 27a8 8 0 0 1 15 0"/><path d="M33 14h9M33 20h7M33 26h9"/><circle cx="30" cy="14" r="1.2"/><circle cx="30" cy="20" r="1.2"/><circle cx="30" cy="26" r="1.2"/></svg>',
  '<svg viewBox="0 0 48 48"><circle cx="20" cy="20" r="12" class="f"/><path d="M8 20h24M20 8c4 3.5 4 20.5 0 24M20 8c-4 3.5-4 20.5 0 24"/><ellipse cx="36" cy="28" rx="7" ry="2.5" class="f"/><path d="M29 28v8c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-8M29 32c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/></svg>',
  '<svg viewBox="0 0 48 48"><rect x="5" y="10" width="26" height="18" rx="3" class="f"/><path d="M10 16h10M10 21h14"/><rect x="21" y="18" width="22" height="16" rx="3" class="f"/><circle cx="30" cy="27" r="5"/><path d="m34 31 5 5"/></svg>',
];

const STAGES = [
  ["found", "Found"], ["scored", "Scored"], ["drafted", "Drafted"],
  ["held", "Held"], ["applied", "Sent"], ["responded", "Replied"],
];

LOADERS.overview = async function () {
  try { S.profileReady = !!(await api("/api/meta")).profile_ready; }
  catch (e) { /* keep the last answer */ }
  const now = new Date();
  $("#ovDate").textContent = now.toLocaleDateString(undefined,
    { weekday: "long", day: "numeric", month: "long" });
  await refresh();
  const p = (S.data || {}).profile || {};
  const hasProfile = S.profileReady;
  let sources = [];
  try { sources = (await api("/api/jobs/search/config", undefined, "GET")).sources || []; }
  catch (e) { /* the steps still render */ }
  $("#nSources").textContent = sources.length || "";

  // a first run is a checklist, not a row of zeros
  const steps = [
    ["Tell it who you are", "What you've done is the only thing a draft is allowed to claim.",
     !!hasProfile, "Fill in your profile", () => show("profile")],
    ["Tell it where to look", "Boards, careers pages, RSS — or let it find some.",
     sources.length > 0, "Add sources", () => show("sources")],
    ["Find roles", "It searches, scores against your profile, and drafts.",
     S.roles.length > 0, "Search now", () => show("search")],
  ];
  const ob = $("#onboard");
  ob.innerHTML = "";
  if (steps.some((s) => !s[2])) {
    const wrap = el("div", "steps");
    let nextMarked = false;
    steps.forEach(([title, why, done, cta, go], i) => {
      const isNext = !done && !nextMarked;
      if (isNext) nextMarked = true;
      const s = el("div", "step" + (done ? " done" : "") + (isNext ? " next" : ""));
      const art = el("div", "step-art");
      art.innerHTML = STEP_ART[i];
      s.appendChild(art);
      s.appendChild(el("div", "step-num", done ? "✓ done" : `step ${i + 1}`));
      s.appendChild(el("h4", "", title));
      s.appendChild(el("p", "", why));
      if (!done) {
        const b = el("button", "btn sm" + (isNext ? " primary" : ""), cta);
        b.addEventListener("click", go);
        s.appendChild(b);
      }
      wrap.appendChild(s);
    });
    ob.appendChild(wrap);
  }

  let pipe = {};
  try { pipe = await api("/api/jobs/pipeline", undefined, "GET"); } catch (e) { }
  const counts = pipe.stages || {};
  const total = pipe.total ?? S.roles.length;
  $("#ovHead").textContent = total
    ? `${total} role${total === 1 ? "" : "s"} in play.`
    : hasProfile ? "Nothing found yet." : "Let's get you set up.";
  $("#ovLede").textContent = pipe.why
    || (total ? "Here is where each one stands, and what's holding the rest up."
              : "Three steps and it can start looking on its own.");

  const track = $("#track");
  track.innerHTML = "";
  STAGES.forEach(([key, label]) => {
    const n = counts[key] || 0;
    const stop = el("div", "stop" + (n ? " lit" : "")
      + (pipe.blocked_at === key ? " blocked" : ""));
    const ring = el("div", "ring");
    ring.appendChild(el("div", "stop-n" + (n ? "" : " zero"), String(n)));
    stop.appendChild(ring);
    stop.appendChild(el("div", "stop-label", label));
    // No stage-to-stage percentage: "held" is a detour, not a step, so
    // "sent — 100% of held" was computed against the wrong thing. A number
    // that looks like analysis and isn't is worse than none.
    track.appendChild(stop);
  });

  const blk = $("#ovBlocked");
  blk.innerHTML = "";
  if (pipe.blocked_at) {
    blk.appendChild(el("p", "", pipe.why || `Stuck at ${pipe.blocked_at}.`));
  } else {
    blk.appendChild(el("p", "muted", total ? "Nothing is stuck."
                                             : "Nothing to do until roles come in."));
  }
  const extra = pipe.counts || {};
  [["screened", "screened out before any engine was called"],
   ["no_address", "portal-only — nothing can be sent automatically"],
   ["needs_redraft", "held drafts waiting on a rewrite"]]
    .filter(([k]) => extra[k])
    .forEach(([k, why]) => blk.appendChild(el("p", "muted", `${extra[k]} ${why}`)));

  const prevBox = $("#ovPreview");
  prevBox.innerHTML = "";
  try {
    const pv = await api("/api/jobs/auto/preview", undefined, "GET");
    prevBox.appendChild(el("p", "", pv.sentence || "Auto-apply is off."));
    if (pv.dry_run) prevBox.appendChild(el("p", "muted", "Rehearsal is on, so nothing leaves."));
    const go = el("button", "btn sm", "Open auto-apply");
    go.addEventListener("click", () => show("auto"));
    prevBox.appendChild(go);
  } catch (e) { prevBox.appendChild(el("p", "muted", "Couldn't read that.")); }

  drawNeeds(counts, pipe);
  drawWhereFrom();
  drawActivity();

  const fu = (S.data || {}).follow_ups || [];
  $("#ovFollowCard").hidden = !fu.length;
  const fh = $("#ovFollow");
  fh.innerHTML = "";
  fu.slice(0, 6).forEach((f) => {
    const row = el("div", "item static");
    row.append(el("div", "mono", initial(f.company)),
      Object.assign(el("div"), {}),
      el("span", "tag", `${f.days || "?"}d`));
    row.children[1].append(el("div", "item-t", f.title || ""),
                           el("div", "item-m", f.company || ""));
    fh.appendChild(row);
  });
};

// The overview used a third of the screen and left the rest empty. These three
// are built only from what the app already knows — no invented activity.
function needRow(host, label, n, where, why) {
  if (!n) return;
  const b = el("button", "need");
  b.type = "button";
  b.append(el("span", "need-n", String(n)),
           el("span", "need-t", label));
  if (why) b.appendChild(el("span", "need-why", why));
  b.addEventListener("click", () => show(where));
  host.appendChild(b);
}

function drawNeeds(counts, pipe) {
  const host = $("#ovNeeds");
  if (!host) return;
  host.innerHTML = "";
  const extra = (pipe || {}).counts || {};
  const scored = (counts || {}).scored || 0;
  needRow(host, "held draft(s) claiming too much", extra.held
    || (counts || {}).held || 0, "drafts", "read and redraft");
  needRow(host, "need rewriting since your profile changed",
          extra.needs_redraft || 0, "drafts", "");
  needRow(host, "scored, not drafted yet", scored, "roles", "draft them");
  needRow(host, "not scored yet", (counts || {}).found || 0, "roles",
          "score to rank them");
  needRow(host, "portal-only — no address to email", extra.no_address || 0,
          "roles", "apply through the site");
  if (!S.profileReady) {
    needRow(host, "your profile is too thin to draft from", 1, "profile",
            "fill it in");
  }
  if (!host.children.length) {
    host.appendChild(el("p", "muted", "Nothing waiting on you."));
  }
}

async function drawWhereFrom() {
  const host = $("#ovSources");
  if (!host) return;
  host.innerHTML = "";
  const bySource = {};
  S.roles.forEach((r) => {
    const s = r.source || "added by hand";
    bySource[s] = (bySource[s] || 0) + 1;
  });
  const rows = Object.entries(bySource).sort((a, b) => b[1] - a[1]).slice(0, 6);
  if (!rows.length) {
    host.appendChild(el("p", "muted", "No roles yet."));
    return;
  }
  const top = rows[0][1];
  rows.forEach(([name, n]) => {
    const row = el("div", "bar-row");
    row.appendChild(el("span", "bar-name", name));
    const track = el("span", "bar-track");
    const fill = el("span", "bar-fill");
    fill.style.width = Math.max(6, Math.round((n / top) * 100)) + "%";
    track.appendChild(fill);
    row.append(track, el("span", "bar-n", String(n)));
    host.appendChild(row);
  });
  // a source that returned nothing last time is worth knowing about here
  try {
    const cfg = await api("/api/jobs/search/config");
    const bad = (cfg.sources || []).filter((s) => s.status === "failing");
    if (bad.length) {
      const p = el("p", "muted");
      p.textContent = `${bad.length} source(s) returned nothing last check: `
        + bad.map((s) => s.name).join(", ");
      host.appendChild(p);
    }
  } catch (e) { /* the counts still stand */ }
}

function drawActivity() {
  const host = $("#ovActivity");
  if (!host) return;
  host.innerHTML = "";
  const events = [];
  S.roles.forEach((r) => {
    (r.events || []).forEach((e) => events.push({
      at: e.at || "", stage: e.stage || "", note: e.note || "",
      title: r.title, key: r.key,
    }));
  });
  events.sort((a, b) => String(b.at).localeCompare(String(a.at)));
  if (!events.length) {
    host.appendChild(el("p", "muted",
      "Nothing yet. Scoring, drafting and applying all show up here."));
    return;
  }
  const said = {
    applied: "applied to", drafted: "drafted for", held: "held a draft for",
    responded: "heard back from", interview: "interview for",
    offer: "offer from", closed: "closed",
  };
  events.slice(0, 6).forEach((e) => {
    const row = el("button", "act");
    row.type = "button";
    row.append(el("span", "act-dot act-" + (e.stage || "")),
               el("span", "act-t", `${said[e.stage] || e.stage} ${e.title}`),
               el("span", "act-at", (e.at || "").replace(" UTC", "")));
    row.addEventListener("click", () => { S.selected = e.key; show("roles"); });
    host.appendChild(row);
  });
}

/* --- roles: list and the role itself ----------------------------------- */
const CHIPS = [["", "All"], ["email", "Can email"], ["fit", "Fit 75+"],
               ["unscored", "Not scored"], ["open", "Still open"]];

LOADERS.roles = async function () {
  await refresh();
  drawRoleList();
  if (S.selected) {
    const r = S.roles.find((x) => x.key === S.selected);
    if (r) openRole(r); else placeholderRole();
  } else placeholderRole();
};

function placeholderRole() {
  const d = $("#roleDetail");
  d.innerHTML = "";
  const p = el("div", "placeholder");
  const inner = el("div");
  inner.appendChild(el("b", "", S.roles.length ? "Pick a role" : "No roles yet"));
  inner.appendChild(el("div", "", S.roles.length
    ? "Everything you can do with it opens here."
    : "Search, or add a source and let it look for you."));
  p.appendChild(inner);
  d.appendChild(p);
}

function drawRoleList() {
  const chips = $("#roleChips");
  chips.innerHTML = "";
  CHIPS.forEach(([k, label]) => {
    const c = el("button", "chip" + (S.chip === k ? " is-on" : ""), label);
    c.type = "button";
    c.addEventListener("click", () => { S.chip = k; drawRoleList(); });
    chips.appendChild(c);
  });

  const q = S.filter.toLowerCase();
  let rows = S.roles.filter((r) => {
    if (q && !`${r.title} ${r.company} ${r.source || ""}`.toLowerCase().includes(q)) return false;
    const f = fitOf(r);
    if (S.chip === "email" && !r.apply_email) return false;
    if (S.chip === "fit" && !(f >= 75)) return false;
    if (S.chip === "unscored" && f !== undefined && f !== null) return false;
    if (S.chip === "open" && (r.stale || r.stage === "closed")) return false;
    return true;
  });
  const sorters = {
    fit: (a, b) => (fitOf(b) || -1) - (fitOf(a) || -1),
    new: (a, b) => String(b.found_at || "").localeCompare(a.found_at || ""),
    company: (a, b) => String(a.company || "").localeCompare(b.company || ""),
    stage: (a, b) => String(a.stage || "").localeCompare(b.stage || ""),
  };
  rows = rows.slice().sort(sorters[S.sort]);

  const list = $("#roleList");
  list.innerHTML = "";
  if (!rows.length) {
    empty(list, S.roles.length ? "Nothing matches" : "No roles yet",
          S.roles.length ? "Try a different filter." : "Go to Search to find some.");
  }
  rows.forEach((r) => {
    const it = el("div", "item" + (r.key === S.selected ? " is-on" : ""));
    it.setAttribute("role", "button");
    // the badge IS the tick box: click it to select, and it shows a tick. An
    // invisible checkbox meant you could select rows and never see which.
    const on = S.ticked.has(r.key);
    const mono = el("div", "mono" + (on ? " ticked" : ""), on ? "✓" : initial(r.company));
    mono.title = "Select";
    mono.addEventListener("click", (e) => {
      e.stopPropagation();
      S.ticked.has(r.key) ? S.ticked.delete(r.key) : S.ticked.add(r.key);
      drawRoleList();
    });
    const body = el("div");
    const t = el("div", "item-t", r.title || "(untitled)");
    // the same state the detail shows: the list said "drafted" beside a
    // detail that said "held", which is one role described two ways
    const st = r.bucket || r.stage || "found";
    if (st !== "found") {
      t.appendChild(el("span", "tag"
        + (st === "held" || st === "needs_redraft" ? " held"
           : st === "applied" ? " sent"
           : st === "closed" || st === "expired" || st === "screened" ? " stop" : ""),
        st === "needs_redraft" ? "needs rewriting" : st));
    }
    body.append(t, el("div", "item-m",
      [r.company, r.location, r.source].filter(Boolean).join(" · ")));
    const f = fitOf(r);
    const fit = el("div", "fit" + (f >= 75 ? " hi" : f !== undefined && f !== null ? "" : " lo"),
                   f === undefined || f === null ? "—" : String(f));
    it.append(mono, body, fit);
    it.addEventListener("click", () => { S.selected = r.key; drawRoleList(); openRole(r); });
    list.appendChild(it);
  });
  drawBulk();
}

function drawBulk() {
  const b = $("#bulk");
  b.hidden = S.ticked.size === 0;
  $("#bulkN").textContent = `${S.ticked.size} selected`;
}

async function bulk(action, btn) {
  const keys = Array.from(S.ticked);
  if (action === "clear") { S.ticked.clear(); drawRoleList(); return; }
  await busy(btn, "Working…", async () => {
    if (action === "remove") {
      await api("/api/jobs/remove", { keys });
      toast(`Removed ${keys.length}.`);
    } else if (action === "score") {
      // one at a time so a failure is counted as a failure, not a success
      let ok = 0, bad = 0;
      for (const key of keys) {
        try { await api("/api/jobs/score", withEngine({ key })); ok++; }
        catch (e) { bad++; }
      }
      toast(bad ? `Scored ${ok}, ${bad} failed.` : `Scored ${ok}.`, bad ? "warn" : "");
    }
    S.ticked.clear();
    await LOADERS.roles();
  });
}

// What the rehearsal worked out, in a form you can paste. Automation gets
// some way into most forms and stops — an upload it can't reach, a question
// in a widget — and retyping what the app already worked out is the moment
// people give up.
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const was = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = was; }, 1200);
  } catch (e) {
    // no clipboard permission: select it instead so ctrl-C works
    const ta = el("textarea", "copy-fallback");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    toast("Press Ctrl+C to copy.", "warn");
    setTimeout(() => ta.remove(), 8000);
  }
}

// It can stop mid-way and ask for you — a sign-in page, a captcha. The
// browser is open on this machine, so the useful thing is to say so and wait,
// then carry on from where it stopped.
function followPortal(r) {
  if (S._portalTimer) clearInterval(S._portalTimer);
  const tick = async () => {
    let s;
    try { s = await api(`/api/jobs/portal/session/${encodeURIComponent(r.key)}`); }
    catch (e) { return; }
    if (s.state === "waiting_for_you") {
      showPortalWaiting(s, r);
      return;
    }
    if (s.state === "running") {
      showPortalRunning(s, r);
      return;
    }
    if (s.state) {
      clearInterval(S._portalTimer);
      S._portalTimer = null;
      showPortal(s, r);
      refresh().then(drawRoleList);
    }
  };
  tick();
  S._portalTimer = setInterval(tick, 2000);
}

function portalBox(title) {
  const box = el("div", "out portal-out");
  box.appendChild(el("h4", "", title));
  return box;
}

function showPortalRunning(s, r) {
  const box = portalBox("Working through the form…");
  box.appendChild(el("p", "muted", s.message || "The browser is open on this machine."));
  const d = $("#roleDetail");
  const was = d.querySelector(".portal-out");
  if (was) was.replaceWith(box); else d.appendChild(box);
}

function showPortalWaiting(s, r) {
  const box = portalBox(s.blocked_by === "needs_captcha"
    ? "It needs you — there's a captcha"
    : "It needs you — this one wants you signed in");
  box.classList.add("portal-waiting");
  box.appendChild(el("p", "", s.message
    || "Handle it in the open browser window; it will carry on by itself."));
  box.appendChild(el("p", "muted",
    "It watches the page and continues the moment it clears. Press Continue "
    + "if it can't tell, or Stop to leave it."));
  const bar = el("div", "actions");
  const go = el("button", "btn primary sm", "I've handled it — continue");
  go.addEventListener("click", () => busy(go, "Carrying on…", async () => {
    await api("/api/jobs/portal/continue", { key: r.key });
  }));
  const stop = el("button", "btn sm ghost danger", "Stop");
  stop.addEventListener("click", () => busy(stop, "Stopping…", async () => {
    await api("/api/jobs/portal/cancel", { key: r.key });
  }));
  bar.append(go, stop);
  box.appendChild(bar);
  const d = $("#roleDetail");
  const was = d.querySelector(".portal-out");
  if (was) was.replaceWith(box); else d.appendChild(box);
  // a quiet nudge, because the browser window may be behind this one
  if (!S._portalNudged) {
    S._portalNudged = true;
    toast(s.blocked_by === "needs_captcha"
      ? "A captcha needs you — the browser window is open."
      : "A sign-in needs you — the browser window is open.", "warn");
  }
}

function showPortal(x, r) {
  const d = $("#roleDetail");
  const out = el("div", "out portal-out");
  const state = x.state || (x.ok ? "filled" : "failed");
  const head = el("h4", "", {
    submitted: "Submitted through the portal",
    filled: "Filled in — check it and press submit yourself",
    needs_answer: "It needs answers it couldn't work out",
    unknown_form: "Part of this form is beyond it",
    blocked: "The site blocked automation",
    failed: "Couldn't complete the form",
  }[state] || state);
  out.appendChild(head);
  if (x.message) out.appendChild(el("p", "muted", x.message));
  if (x.waited_for_you) {
    out.appendChild(el("p", "muted", x.waited_for_you === "needs_captcha"
      ? "Carried on after you solved the captcha."
      : "Carried on after you signed in."));
  }
  S._portalNudged = false;

  const answers = x.answers || [];
  if (answers.length) {
    out.appendChild(el("h5", "portal-h", "Its answers"));
    answers.forEach((a) => {
      const row = el("div", "ans ans-" + (a.source || ""));
      const q = el("div", "ans-q", a.question);
      const tag = el("span", "tag " + (a.source === "engine" ? "sent"
        : a.source === "held" ? "held" : ""),
        a.source === "engine" ? "from your profile"
          : a.source === "held" ? "held — check this" : "blank");
      q.appendChild(tag);
      row.appendChild(q);
      if (a.answer) {
        row.appendChild(el("div", "ans-a", a.answer));
        const c = el("button", "btn sm ghost", "Copy");
        c.addEventListener("click", () => copyText(a.answer, c));
        row.appendChild(c);
      } else {
        row.appendChild(el("div", "muted", a.why || "you'll need to type this"));
      }
      out.appendChild(row);
    });
  }

  if (x.paste_pack) {
    const bar = el("div", "actions");
    const all = el("button", "btn primary sm", "Copy everything");
    all.title = "Every field and answer, ready to paste into the form";
    all.addEventListener("click", () => copyText(x.paste_pack, all));
    bar.appendChild(all);
    if (r && r.url) {
      const open = el("button", "btn sm", "Open the form");
      open.addEventListener("click", () => window.open(r.url, "_blank", "noopener"));
      bar.appendChild(open);
    }
    out.appendChild(bar);
    const pre = el("pre", "paste-pack");
    pre.textContent = x.paste_pack;
    out.appendChild(pre);
  }
  const host = d.querySelector(".portal-out");
  if (host) host.replaceWith(out); else d.appendChild(out);
}

function fact(k, v, hi) {
  const w = el("div");
  w.append(el("div", "fact-k", k), el("div", "fact-v" + (hi ? " hi" : ""), v));
  return w;
}

function openRole(r) {
  const d = $("#roleDetail");
  d.innerHTML = "";
  enterView(d);
  d.appendChild(el("p", "doc-kicker",
    [r.source, r.days_listed ? `${r.days_listed} days listed` : ""]
      .filter(Boolean).join(" · ") || "role"));
  d.appendChild(el("h1", "doc-title", r.title || "(untitled)"));
  d.appendChild(el("p", "doc-by", [r.company, r.location].filter(Boolean).join(" — ")));

  const f = fitOf(r);
  const facts = el("div", "doc-facts");
  // the bucket, not the raw stage: a role could read "STAGE found" directly
  // above "scored, not drafted yet", which is the same thing said two ways
  const BUCKET = {
    found: "not scored", screened: "screened out", scored: "scored",
    drafted: "drafted", held: "held", needs_redraft: "needs rewriting",
    no_address: "portal only", applied: "applied", waiting: "waiting",
    responded: "replied", interview: "interview", offer: "offer",
    closed: "closed", expired: "expired",
  };
  facts.append(
    fact("Fit", f === undefined || f === null ? "not scored" : `${f}`, f >= 75),
    fact("Stage", BUCKET[r.bucket] || r.bucket || r.stage || "found"),
    fact("Apply by", r.apply_email ? "email" : "portal"));
  d.appendChild(facts);
  if (r.state_why) d.appendChild(el("p", "muted", r.state_why));
  if (r.summary) d.appendChild(el("div", "doc-body", r.summary));

  const out = el("div");
  // Highlight the next sensible step, not always the first button. "Score
  // it" glowing on a role that's scored and already applied told you to do
  // something you'd done.
  // keyed on the state, not the raw stage — a held draft was being told to
  // mark itself applied
  const nextStep = {
    found: "Score it", screened: "Score it", scored: "Draft the application",
    drafted: r.apply_email ? "I applied myself" : "Apply via the portal (rehearse)",
    held: "Draft the application", needs_redraft: "Draft the application",
    no_address: "Apply via the portal (rehearse)",
    applied: "Draft a follow-up", waiting: "Draft a follow-up",
    responded: "Interview prep", interview: "Interview prep",
  }[r.bucket || r.stage || "found"];
  const group = (title, buttons) => {
    const g = el("div", "actions-group");
    g.appendChild(el("h5", "", title));
    const a = el("div", "actions");
    buttons.forEach(([label, fn, cls]) => {
      const c2 = label === nextStep ? "primary"
        : (cls === "primary" ? "" : cls);
      const b = el("button", "btn sm" + (c2 ? " " + c2 : ""), label);
      b.addEventListener("click", () => fn(b));
      a.appendChild(b);
    });
    g.appendChild(a);
    d.appendChild(g);
  };
  const show = (title, content) => {
    out.innerHTML = "";
    const box = el("div", "out");
    box.appendChild(el("h4", "", title));
    if (typeof content === "string") box.appendChild(el("pre", "", content));
    else box.appendChild(content);
    out.appendChild(box);
  };
  const act = (path, label, render) => async (btn) => busy(btn, label, async () => {
    const res = await api(path, withEngine({ key: r.key }));
    render(res);
    await refresh(); railCounts(); drawRoleList();
  });

  group("Decide", [
    ["Score it", act("/api/jobs/score", "Scoring…", (x) =>
      show(`Fit ${((x.fit || x).score) ?? "—"}`,
           (x.fit || x).why || (x.fit || x).reason || "Scored.")), "primary"],
    ["ATS check", act("/api/jobs/ats", "Checking…", (x) => {
      const box = el("div");
      box.appendChild(el("p", "", `${x.score ?? "—"}% of the advert's terms appear in your profile.`));
      if ((x.missing || []).length) {
        box.appendChild(el("p", "muted", "Missing:"));
        const ul = el("ul");
        x.missing.slice(0, 12).forEach((m) => ul.appendChild(el("li", "", String(m))));
        box.appendChild(ul);
      }
      show("Keyword match", box);
    })],
    ["Open the posting", () => { if (r.url) window.open(r.url, "_blank", "noopener"); }],
  ]);

  group("Prepare", [
    ["Draft the application", act("/api/jobs/draft", "Drafting…", (x) => {
      const dr = x.draft || x;
      const ok = (dr.check || {}).ok !== false;
      show(ok ? (dr.subject || "Draft") : "Held — it claims too much",
           ok ? (dr.body || "") :
           ((dr.check || {}).problems || []).map((p) => "• " + (p.detail || p)).join("\n")
             + "\n\n" + (dr.body || ""));
    })],
    ["Tailor my CV", act("/api/jobs/cv", "Tailoring…", (x) =>
      show("Tailored CV", (x.text || x.cv || "") + (x.path ? `\n\nSaved to ${x.path}` : "")))],
    ["Interview prep", act("/api/jobs/interview", "Preparing…", (x) =>
      show("Likely questions", x.text || (x.questions || []).map((q) => "• " + (q.q || q)).join("\n")))],
    ["Save to a file", act("/api/jobs/save-file", "Saving…", (x) =>
      show("Saved", x.path || "Written."))],
  ]);

  group("Apply", [
    ["Apply via the portal (rehearse)", async (btn) => busy(btn, "Opening…", async () => {
      await api("/api/jobs/portal", withEngine({ key: r.key, submit: false }));
      followPortal(r);
    })],
    ["…and submit", async (btn) => {
      if (!confirm("Submit this application through the portal for real?")) return;
      busy(btn, "Submitting…", async () => {
        await api("/api/jobs/portal", withEngine({ key: r.key, submit: true }));
        followPortal(r);
      });
    }, "danger"],
    ["I applied myself", async (btn) => busy(btn, "Saving…", async () => {
      const note = prompt("How did you apply? (optional)", "") || "";
      await api("/api/jobs/applied", { key: r.key, how: "by hand", note });
      toast("Marked as applied — the follow-up clock has started.");
      await refresh(); drawRoleList();
    })],
    ["Draft a follow-up", act("/api/jobs/follow-up", "Drafting…", (x) =>
      show("Follow-up", x.body || x.text || ""))],
  ]);

  group("Track", [
    ...["responded", "interview", "offer", "closed"].map((st) =>
      [st[0].toUpperCase() + st.slice(1), async (btn) => busy(btn, "…", async () => {
        await api("/api/jobs/stage", { key: r.key, stage: st, note: "" });
        toast(`Moved to ${st}.`);
        await refresh(); drawRoleList();
        openRole(S.roles.find((x) => x.key === r.key) || r);
      })]),
    ["Remove", async (btn) => {
      if (!confirm("Remove this role?")) return;
      busy(btn, "Removing…", async () => {
        await api("/api/jobs/remove", { key: r.key });
        S.selected = null;
        await LOADERS.roles();
      });
    }, "danger"],
  ]);

  // what the app already knows about this role, rather than empty space:
  // why it scored, what the draft says, and what has happened to it
  const why = (r.fit || {}).why || (r.fit || {}).reason;
  if (why) {
    const box = el("div", "out");
    box.appendChild(el("h4", "", "Why this score"));
    box.appendChild(el("pre", "", why));
    d.appendChild(box);
  }
  const dr = r.draft || {};
  if (dr.body) {
    const box = el("div", "out");
    const held = (dr.check || {}).ok === false;
    box.appendChild(el("h4", "", held ? "Draft — held" : (dr.subject || "The draft")));
    if (held) {
      const ul = el("ul");
      ((dr.check || {}).problems || []).forEach((p) =>
        ul.appendChild(el("li", "", p.detail || String(p))));
      box.appendChild(ul);
    }
    box.appendChild(el("pre", "", dr.body));
    d.appendChild(box);
  }
  const ev = r.events || [];
  if (ev.length) {
    const box = el("div", "out");
    box.appendChild(el("h4", "", "History"));
    ev.slice().reverse().slice(0, 8).forEach((e) => {
      const row = el("div", "hist");
      row.append(el("span", "hist-at", (e.at || "").replace(" UTC", "")),
                 el("span", "hist-t", e.stage + (e.note ? " — " + e.note : "")));
      box.appendChild(row);
    });
    d.appendChild(box);
  }

  d.appendChild(out);
}

/* --- search ------------------------------------------------------------ */
LOADERS.search = function () {
  $("#q").focus && $("#q").focus();
  drawResults();
};

const SR = { results: [], filter: "all" };

// what a result's state looks like, and what you can do about it
function resultState(r) {
  if (r.already_tracked) return "tracked";
  if (r.removed_before) return "removed";
  return "new";
}

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  const isUrl = /^https?:\/\//i.test(q);
  await busy($("#qGo"), isUrl ? "Reading…" : "Searching…", async () => {
    const d = isUrl
      ? await api("/api/jobs/search/url", { url: q, use_browser: $("#qBrowser").checked })
      : await api("/api/jobs/search", { query: q });
    SR.results = d.results || [];
    SR.searched = true;
    SR.note = d.note || d.detail || "";
    SR.filter = "all";
    drawResults();
  });
}

async function track(items, restore, btn) {
  // Sent as "items" — the route's field. It once went as "roles", the server
  // quietly ignored it, answered ok with nothing added, and the window
  // showed every role as tracked.
  const run = async () => {
    const x = await api("/api/jobs/search/add", { items, restore: !!restore });
    const byKey = {};
    (x.outcomes || []).forEach((o) => { byKey[o.key] = o.status; });
    SR.results.forEach((r) => {
      const s = byKey[r.key];
      if (s === "added" || s === "already tracked") {
        r.already_tracked = true; r.removed_before = false;
      }
    });
    const n = (x.outcomes || []).reduce((a, o) => {
      a[o.status] = (a[o.status] || 0) + 1; return a; }, {});
    const parts = [];
    if (n["added"]) parts.push(`Now tracking ${n["added"]}`);
    if (n["already tracked"]) parts.push(`${n["already tracked"]} already tracked`);
    if (n["removed earlier"]) parts.push(`${n["removed earlier"]} removed earlier — use Restore`);
    if (n["not a role"]) parts.push(`${n["not a role"]} didn't look like a real role`);
    toast(parts.join(" · ") || "Nothing changed.", n["added"] ? "" : "warn");
    await refresh();
    drawResults();
  };
  return btn ? busy(btn, restore ? "Restoring…" : "Tracking…", run) : run();
}

function drawResults() {
  const host = $("#results");
  host.innerHTML = "";
  const all = SR.results;
  if (!all.length) {
    empty(host, SR.searched ? "Nothing found" : "Search your sources",
          SR.searched ? (SR.note || "Try broader words.")
                      : "Type what you're after above, or paste a posting's address.");
    return;
  }
  const count = { all: all.length, new: 0, tracked: 0, removed: 0 };
  all.forEach((r) => { count[resultState(r)]++; });

  const bar = el("div", "result-bar");
  const sum = el("div", "result-sum");
  sum.innerHTML = `<b>${all.length}</b> found · <span class="t-new">${count.new} new</span>`
    + ` · <span class="t-tracked">${count.tracked} tracked</span>`
    + (count.removed ? ` · <span class="t-removed">${count.removed} removed earlier</span>` : "");
  bar.appendChild(sum);
  const chips = el("div", "chips");
  [["all", "All"], ["new", "New"], ["tracked", "Tracked"], ["removed", "Removed"]]
    .filter(([k]) => k === "all" || count[k])
    .forEach(([k, label]) => {
      const c = el("button", "chip" + (SR.filter === k ? " is-on" : ""), `${label} ${count[k]}`);
      c.addEventListener("click", () => { SR.filter = k; drawResults(); });
      chips.appendChild(c);
    });
  bar.appendChild(chips);
  if (count.new) {
    const fresh = all.filter((r) => resultState(r) === "new");
    const b = el("button", "btn primary sm", `Track ${count.new} new`);
    b.addEventListener("click", () => track(fresh, false, b));
    bar.appendChild(b);
  }
  host.appendChild(bar);

  // a tracked role may already have a fit score; a fresh result never does,
  // and it says so rather than showing a number nobody computed
  const fitByKey = {};
  S.roles.forEach((r) => { if (r.fit && r.fit.score != null) fitByKey[r.key] = r.fit.score; });

  const grid = el("div", "role-grid");
  all.filter((r) => SR.filter === "all" || resultState(r) === SR.filter).forEach((r) => {
    const st = resultState(r);
    const card = el("article", "role-card is-" + st);
    const head = el("div", "rc-head");
    const logo = el("div", "rc-logo", initial(r.company));
    const who = el("div", "rc-who");
    who.append(el("div", "rc-title", r.title || "(untitled)"),
               el("div", "rc-co", [r.company, r.location].filter(Boolean).join(" · ")));
    head.append(logo, who);
    card.appendChild(head);

    const score = fitByKey[r.key];
    const line = el("div", "rc-line");
    const match = el("span", "rc-match" + (score == null ? " none" : score >= 75 ? " hi" : ""),
                     score == null ? "Not scored" : `${score}% Match`);
    const tag = el("span", "tag " + (st === "tracked" ? "sent" : st === "removed" ? "held" : "new"),
                   st === "tracked" ? "Tracked" : st === "removed" ? "Removed earlier" : "New");
    line.append(match, tag);
    card.appendChild(line);
    card.appendChild(el("p", "rc-desc", (r.summary || r.source || "").slice(0, 220)));

    const acts = el("div", "rc-acts");
    const add = (label, cls, fn) => {
      const b = el("button", "btn sm" + (cls ? " " + cls : ""), label);
      b.addEventListener("click", () => fn(b));
      acts.appendChild(b);
    };
    if (st === "new") {
      add("Track", "primary", (b) => track([r], false, b));
    } else if (st === "removed") {
      add("Restore", "", (b) => track([r], true, b));
    } else {
      add("Analyze", "primary", (b) => busy(b, "Scoring…", async () => {
        await api("/api/jobs/score", withEngine({ key: r.key }));
        await refresh(); drawResults();
        toast("Scored.");
      }));
      add("Open", "", () => { S.selected = r.key; show("roles"); });
    }
    if (r.url) add("Posting", "ghost", () => window.open(r.url, "_blank", "noopener"));
    card.appendChild(acts);
    grid.appendChild(card);
  });
  host.appendChild(grid);
}


/* --- drafts and claims ------------------------------------------------- */
LOADERS.drafts = async function () {
  let d = {};
  try { d = await api("/api/jobs/claims", undefined, "GET"); }
  catch (e) { empty($("#heldList"), "Couldn't load", e.message); return; }

  const held = d.held || [];
  $("#nDrafts").textContent = held.length || "";
  const hl = $("#heldList");
  hl.innerHTML = "";
  if (!held.length) empty(hl, "Nothing held", "Every draft so far stands on your profile.");
  held.forEach((h) => {
    const it = el("div", "item");
    const body = el("div");
    body.append(el("div", "item-t", h.title || "(untitled)"),
      el("div", "item-m", `${(h.problems || []).length} claim(s) — ${h.company || ""}`));
    it.append(el("div", "mono", initial(h.company)), body, el("span", "tag held", "held"));
    it.addEventListener("click", () => {
      $$("#heldList .item").forEach((x) => x.classList.remove("is-on"));
      it.classList.add("is-on");
      showDraft(h);
    });
    hl.appendChild(it);
  });

  const cl = $("#claimList");
  cl.innerHTML = "";
  const claims = d.claims || [];
  if (!claims.length) empty(cl, "No repeat claims", "");
  claims.forEach((c) => {
    const it = el("div", "item static");
    const body = el("div");
    body.append(el("div", "item-t", c.term || c.detail || "claim"),
      el("div", "item-m", `${(c.roles || []).length} draft(s) · ${c.kind || ""}`));
    const acts = el("div", "actions");
    acts.style.margin = "0";
    const yes = el("button", "btn sm", "It's true");
    yes.addEventListener("click", () => busy(yes, "…", async () => {
      await api("/api/jobs/claims/confirm", { term: c.term, where: c.kind || "skills" });
      toast(`Added "${c.term}" to your profile.`);
      LOADERS.drafts();
    }));
    const no = el("button", "btn sm ghost", "Dismiss");
    no.addEventListener("click", () => busy(no, "…", async () => {
      await api("/api/jobs/claims/dismiss", { term: c.term, where: c.kind || "" });
      toast(`Drafts will stop claiming "${c.term}".`);
      LOADERS.drafts();
    }));
    acts.append(yes, no);
    it.append(el("div", "mono", "?"), body, acts);
    cl.appendChild(it);
  });

  if (!held.length) {
    const dd = $("#draftDetail");
    dd.innerHTML = "";
    const p = el("div", "placeholder");
    const i = el("div");
    i.append(el("b", "", "Nothing to review"), el("div", "", "Held drafts open here."));
    p.appendChild(i);
    dd.appendChild(p);
  }
};

function showDraft(h) {
  const d = $("#draftDetail");
  d.innerHTML = "";
  enterView(d);
  d.appendChild(el("p", "doc-kicker", "held draft"));
  d.appendChild(el("h1", "doc-title", h.title || "Draft"));
  d.appendChild(el("p", "doc-by", h.company || ""));
  const why = el("div", "out");
  why.appendChild(el("h4", "", "Why it was held"));
  const ul = el("ul");
  // problems arrive as plain strings — reading them as objects showed nothing
  (h.problems || []).forEach((p) => ul.appendChild(el("li", "", String(p))));
  why.appendChild(ul);
  if (h.redraft_reason) why.appendChild(el("p", "muted", h.redraft_reason));
  d.appendChild(why);
  if (h.body) {
    const b = el("div", "out");
    b.appendChild(el("h4", "", h.subject || "The draft"));
    b.appendChild(el("pre", "", h.body));
    d.appendChild(b);
  }
  const a = el("div", "actions");
  const redraft = el("button", "btn primary", "Redraft it");
  redraft.addEventListener("click", () => busy(redraft, "Redrafting…", async () => {
    await api("/api/jobs/draft", withEngine({ key: h.key }));
    toast("Redrafted.");
    LOADERS.drafts();
  }));
  const open = el("button", "btn", "Open the role");
  open.addEventListener("click", () => { S.selected = h.key; show("roles"); });
  a.append(redraft, open);
  d.appendChild(a);
}

/* --- auto-apply -------------------------------------------------------- */
LOADERS.auto = async function () {
  await refresh();
  drawReadiness();
  const a = (S.data || {}).auto || {};
  $("#aOn").checked = !!a.enabled;
  $("#aDry").checked = a.dry_run !== false;
  $("#aClean").checked = a.require_clean_check !== false;
  $("#aMin").value = a.min_score ?? 75;
  $("#aCap").value = a.daily_cap ?? 5;
  $("#aSig").value = a.signature || "";
  $("#aPortal").value = a.portal_mode || "prepare";
  $("#aDaily").checked = !!(S.data || {}).daily;
  drawAutoPreview();
};

// Why nothing will send, stated before you go looking. Auto-apply is guarded
// by several separate conditions, and when one was off the run just reported
// "0 sent" — which reads as a broken feature rather than a setting.
async function drawReadiness() {
  const host = $("#autoReady");
  if (!host) return null;
  host.innerHTML = "";
  let r;
  try { r = await api("/api/jobs/auto/readiness"); }
  catch (e) { return null; }
  const box = el("div", "ready " + (r.ok ? "is-ok" : "is-blocked"));
  const head = el("div", "ready-head");
  head.append(el("span", "ready-dot"), el("span", "ready-sum", r.summary));
  box.appendChild(head);
  (r.blockers || []).forEach((b) => {
    const row = el("div", "ready-row");
    row.append(el("span", "ready-what", b.what), el("span", "ready-fix", b.fix));
    box.appendChild(row);
  });
  (r.notes || []).forEach((n) => box.appendChild(el("div", "ready-note", n)));
  host.appendChild(box);
  return r;
}

async function drawAutoPreview() {
  const host = $("#autoPreview");
  host.innerHTML = "";
  try {
    const p = await api("/api/jobs/auto/preview", undefined, "GET");
    host.appendChild(el("p", "", p.sentence || "Auto-apply is off."));
    const rows = [
      ["would send", (p.would_send || []).length],
      ["held by the claims check", (p.would_hold || []).length],
      ["need scoring first", (p.needs_scoring || []).length],
      ["portal-only", (p.no_address || []).length],
    ].filter(([, n]) => n);
    rows.forEach(([what, n]) => host.appendChild(el("p", "muted", `${n} ${what}`)));
    if (p.dry_run) host.appendChild(el("p", "muted", "Rehearsal is on — nothing leaves."));
  } catch (e) { host.appendChild(el("p", "muted", e.message)); }
}

async function saveAuto(btn) {
  await busy(btn, "Saving…", async () => {
    await api("/api/jobs/auto", {
      enabled: $("#aOn").checked, dry_run: $("#aDry").checked,
      require_clean_check: $("#aClean").checked,
      portal_mode: $("#aPortal").value,
      min_score: Number($("#aMin").value), daily_cap: Number($("#aCap").value),
      signature: $("#aSig").value,
    });
    toast("Rules saved.");
    await refresh(); railCounts(); drawAutoPreview(); drawReadiness();
  });
}

/* --- sources and alerts ------------------------------------------------ */
LOADERS.sources = async function () {
  const host = $("#srcList");
  let cfg = {};
  try { cfg = await api("/api/jobs/search/config", undefined, "GET"); }
  catch (e) { empty(host, "Couldn't load", e.message); return; }
  const src = cfg.sources || [];
  $("#nSources").textContent = src.length || "";
  // freshness is when a search last actually asked the sources
  $("#srcFresh").textContent = cfg.checked_at
    ? `Last checked: ${cfg.checked_at}` : "Not checked yet — run a search to check every source.";
  host.innerHTML = "";
  if (!src.length) {
    empty(host, "No sources yet", "Add one, or let it find some that actually return roles.");
  }
  src.forEach((s) => {
    const row = el("div", "src-row" + (s.on === false ? " paused" : ""));
    row.appendChild(el("div", "rc-logo", initial(s.name || s.url)));
    const who = el("div", "src-who");
    who.append(el("div", "src-name", s.name || s.url), el("div", "src-url", s.url || ""));
    row.appendChild(who);
    const status = s.on === false ? "paused" : (s.status || "pending");
    const badge = el("span", "badge " + status,
      { verified: "Verified", pending: "Pending", failing: "Failing", paused: "Paused" }[status]);
    badge.title = status === "verified" ? `${s.last_count} role(s) on the last check, ${s.checked_at}`
      : status === "failing" ? (s.error || "Returned nothing on the last check") + (s.checked_at ? ` — ${s.checked_at}` : "")
      : status === "paused" ? "Skipped by searches until you resume it"
      : "Not checked yet";
    row.appendChild(badge);
    const acts = el("div", "src-acts");
    const tog = el("button", "btn sm", s.on === false ? "Resume" : "Pause");
    tog.addEventListener("click", () => busy(tog, "…", async () => {
      await api("/api/jobs/sources", { name: s.name, on: s.on === false });
      LOADERS.sources();
    }));
    const view = el("button", "btn sm", "View");
    view.title = "Open this source";
    view.addEventListener("click", () => { if (s.url) window.open(s.url, "_blank", "noopener"); });
    const rm = el("button", "btn sm ghost danger", "Remove");
    rm.addEventListener("click", () => busy(rm, "…", async () => {
      await api("/api/jobs/sources/remove", { name: s.name, url: s.url });
      LOADERS.sources();
    }));
    acts.append(tog, view, rm);
    row.appendChild(acts);
    host.appendChild(row);
  });

  const sug = $("#srcSuggested");
  sug.innerHTML = "";
  try {
    const d = await api("/api/jobs/sources/suggested", undefined, "GET");
    const list = (d.suggested || []).slice(0, 6);
    if (!list.length) sug.appendChild(el("p", "muted", "Fill in your profile and suggestions appear."));
    list.forEach((s) => {
      const it = el("div", "item static");
      const body = el("div");
      body.append(el("div", "item-t", s.name || s.url),
                  el("div", "item-m", s.why || s.url || ""));
      const add = el("button", "btn sm", "Add");
      add.addEventListener("click", () => busy(add, "…", async () => {
        await api("/api/jobs/sources", { name: s.name, url: s.url, kind: s.kind || "" });
        LOADERS.sources();
      }));
      it.append(el("div", "mono", initial(s.name)), body, add);
      sug.appendChild(it);
    });
  } catch (e) { sug.appendChild(el("p", "muted", "Couldn't load suggestions.")); }
};

/* --- engines ----------------------------------------------------------- */
// Adding an engine here writes to the same place Agent Jo reads, so an engine
// added in either app appears in both. The presets exist because a base URL
// typed from memory is how a cloud engine ends up pointing at a local runner.
const ENGINE_PRESETS = [
  ["DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat", true],
  ["OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", true],
  ["Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", true],
  ["Together", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", true],
  ["OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat", true],
  ["Ollama (this machine)", "http://localhost:11434/v1", "qwen3:8b", false],
  ["LM Studio (this machine)", "http://localhost:1234/v1", "local-model", false],
];

function engField(id) { return ($("#" + id).value || "").trim(); }

function drawPresets() {
  const host = $("#engPresets");
  host.innerHTML = "";
  ENGINE_PRESETS.forEach(([name, url, model, cloud]) => {
    const b = el("button", "chip" + (cloud ? "" : " local"), name);
    b.type = "button";
    b.title = url;
    b.addEventListener("click", () => {
      $("#eName").value = name.split(" (")[0];
      $("#eUrl").value = url;
      $("#eModel").value = model;
      $("#eKey").placeholder = cloud ? "sk-…" : "not needed for a local model";
      $("#eOut").textContent = "";
    });
    host.appendChild(b);
  });
}

function engSay(msg, kind) {
  const out = $("#eOut");
  out.className = "eng-out " + (kind || "");
  out.textContent = msg;
}

LOADERS.engines = async function () {
  drawPresets();
  const host = $("#engList");
  host.innerHTML = "";
  let list = [];
  try { list = (await api("/api/engines")).engines || []; }
  catch (e) { empty(host, "Couldn't load engines", e.message); return; }
  const mine = list.filter((e) => e.custom);
  $("#nEngines").textContent = mine.length || "";
  if (!list.length) empty(host, "No engines yet", "Add one above.");
  list.forEach((e) => {
    const row = el("div", "src-row");
    row.appendChild(el("div", "rc-logo", initial(e.label || e.id)));
    const who = el("div", "src-who");
    who.append(el("div", "src-name", e.label || e.id),
               el("div", "src-url", [e.model, e.base_url].filter(Boolean).join("  ·  ")));
    row.appendChild(who);
    // amber is for something wrong; where an engine runs is just a fact
    const kind = el("span", "badge " + (e.kind === "local" ? "verified" : "paused"));
    kind.textContent = e.kind === "local" ? "On this machine" : (e.kind || "cloud");
    kind.title = e.kind === "local"
      ? "Runs here — costs nothing and nothing leaves the machine"
      : "A cloud provider — needs a key, and calls leave this machine";
    row.appendChild(kind);
    const acts = el("div", "src-acts");
    if (e.custom) {
      const test = el("button", "btn sm", "Test");
      test.addEventListener("click", () => busy(test, "Testing…", async () => {
        const r = await api("/api/engines/test", { name: e.id, base_url: e.base_url, model: e.model });
        toast(r.ok ? `${e.id}: ${r.detail}` : `${e.id}: ${r.error} ${r.fix || ""}`,
              r.ok ? "" : "bad");
      }));
      const use = el("button", "btn sm", "Use");
      use.title = "Score and draft with this engine";
      use.addEventListener("click", () => {
        S.engine = e.id;
        const sel = $("#engine"); if (sel) sel.value = e.id;
        toast(`Scoring and drafting will use ${e.id}.`);
      });
      const rm = el("button", "btn sm ghost danger", "Remove");
      rm.addEventListener("click", () => {
        if (!confirm(`Remove the engine "${e.id}"?`)) return;
        busy(rm, "…", async () => {
          await api(`/api/engines/${encodeURIComponent(e.id)}`, undefined, "DELETE");
          LOADERS.engines(); refreshEnginePicker();
        });
      });
      acts.append(test, use, rm);
    } else {
      acts.appendChild(el("span", "muted", "built in"));
    }
    row.appendChild(acts);
    host.appendChild(row);
  });
};

async function refreshEnginePicker() {
  try {
    const m = await api("/api/meta");
    const sel = $("#engine");
    if (!sel) return;
    const was = sel.value;
    sel.innerHTML = "";
    ["Auto"].concat(m.engines || []).forEach((n) => {
      const o = el("option", "", n); o.value = n;
      sel.appendChild(o);
    });
    sel.value = (m.engines || []).includes(was) ? was : (m.default_engine || "Auto");
    S.engine = sel.value;
  } catch (e) { /* the picker keeps what it had */ }
}

/* --- results ----------------------------------------------------------- */
LOADERS.results = async function () {
  let d = {};
  try { d = await api("/api/jobs/outcomes", undefined, "GET"); }
  catch (e) { empty($("#findings"), "Couldn't load", e.message); return; }
  $("#resHead").textContent = d.sent
    ? `${d.replied} ${d.replied === 1 ? "reply" : "replies"} from ${d.sent}.`
    : "Nothing sent yet.";
  $("#resLede").textContent = d.sent
    ? `Somewhere between ${d.overall.low}% and ${d.overall.high}% — the range matters more than the number while the sample is small.`
    : "There is nothing to learn from until applications go out.";
  const f = $("#findings");
  f.innerHTML = "";
  (d.findings || []).forEach((x) => {
    const row = el("div", "finding");
    row.appendChild(el("div", "finding-t", x.what));
    row.appendChild(el("p", "muted", x.why));
    if (x.do) row.appendChild(el("p", "", x.do));
    if (!x.confident) row.querySelector(".finding-t")
      .appendChild(el("span", "tag held", "not conclusive"));
    f.appendChild(row);
  });
  if (!(d.findings || []).length) empty(f, "Nothing conclusive yet", "");

  const b = $("#breakdown");
  b.innerHTML = "";
  b.appendChild(el("h3", "", "The detail"));
  [["By source", d.by_source, "source"], ["By fit", d.by_score, "band"],
   ["By route", d.by_method, "method"]].forEach(([title, rows, key]) => {
    if (!rows || !rows.length) return;
    b.appendChild(el("p", "muted", title));
    rows.forEach((r) => b.appendChild(el("p", "", `${r[key]} — ${r.reading}`)));
  });
  if (d.note) b.appendChild(el("p", "muted", d.note));
};

/* --- archive and tidy -------------------------------------------------- */
LOADERS.archive = async function () {
  const host = $("#archList");
  let d = {};
  try { d = await api("/api/jobs/archive", undefined, "GET"); }
  catch (e) { empty(host, "Couldn't load", e.message); return; }
  $("#nArchive").textContent = d.count || "";
  host.innerHTML = "";
  const rows = d.roles || [];
  if (!rows.length) empty(host, "The archive is empty", "Closed and old roles land here.");
  rows.forEach((r) => {
    const it = el("div", "item static");
    const body = el("div");
    body.append(el("div", "item-t", r.title || "(untitled)"),
      el("div", "item-m", [r.company, r.archived_why || r.reason].filter(Boolean).join(" · ")));
    const back = el("button", "btn sm", "Bring back");
    back.addEventListener("click", () => busy(back, "…", async () => {
      await api("/api/jobs/unarchive", { key: r.key });
      toast("Restored to your roles.");
      LOADERS.archive();
    }));
    it.append(el("div", "mono", initial(r.company)), body, back);
    host.appendChild(it);
  });
};

async function tidy(path, label, body, btn, say) {
  await busy(btn, label, async () => {
    const x = await api(path, body || {});
    toast(say(x));
    LOADERS.archive();
  });
}

/* --- profile ----------------------------------------------------------- */
const FIELDS = [
  ["target_roles", "Desired Roles", "Senior Data Engineer, BI Lead"],
  ["skills", "Core Skills", "Power BI dashboard development, DAX, data modelling"],
  ["technologies", "Tools", "Power BI, SQL Server, SSIS, Snowflake"],
  ["locations_ok", "Location Preferences", "Johannesburg, remote"],
];
const TICK = '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7"/><path d="m5 8.2 2 2L11 6"/></svg>';

LOADERS.profile = async function () {
  let p = {};
  try { p = (await api("/api/jobs/profile", {})).profile || {}; }
  catch (e) { /* render an empty form */ }
  const f = $("#profileForm");
  f.innerHTML = "";
  const fieldRow = (key, label, hint, value) => {
    const w = el("div", "field");
    const l = el("label", "", label);
    w.appendChild(l);
    const box = el("div", "field-box");
    const i = el("input", "input");
    i.id = "p_" + key; i.placeholder = hint; i.value = value;
    const t = el("span", "field-tick" + (value.trim() ? " on" : ""));
    t.innerHTML = TICK;
    t.title = value.trim() ? "A draft may claim these" : "Empty — nothing here can be claimed";
    i.addEventListener("input", () => t.classList.toggle("on", !!i.value.trim()));
    box.append(i, t);
    w.appendChild(box);
    return w;
  };
  const join = (v) => Array.isArray(v) ? v.join(", ") : (v || "");
  FIELDS.slice(0, 3).forEach(([k, label, hint]) => f.appendChild(fieldRow(k, label, hint, join(p[k]))));

  // employment history: one card per employer, as in the design
  const emp = el("div", "field");
  emp.appendChild(el("label", "", "Employment History"));
  const cards = el("div", "emp-cards");
  let employers = Array.isArray(p.employers) ? p.employers.slice() : [];
  const drawEmp = () => {
    cards.innerHTML = "";
    employers.forEach((name, idx) => {
      const c = el("div", "emp-card");
      c.append(el("div", "rc-logo", initial(name)), el("div", "emp-name", name));
      const x = el("button", "emp-x", "×");
      x.title = "Remove";
      x.addEventListener("click", () => { employers.splice(idx, 1); drawEmp(); });
      c.appendChild(x);
      cards.appendChild(c);
    });
    const add = el("input", "input emp-add");
    add.placeholder = "Add an employer and press Enter";
    add.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && add.value.trim()) {
        employers.push(add.value.trim()); drawEmp();
        const next = cards.querySelector(".emp-add"); if (next) next.focus();
      }
    });
    cards.appendChild(add);
  };
  drawEmp();
  emp.appendChild(cards);
  f.appendChild(emp);

  f.appendChild(fieldRow("locations_ok", FIELDS[3][1], FIELDS[3][2], join(p.locations_ok)));

  const save = el("button", "btn primary wide", "Save & Synchronize Profile");
  save.addEventListener("click", () => busy(save, "Saving…", async () => {
    const body = { employers };
    FIELDS.forEach(([k]) => {
      body[k] = ($("#p_" + k).value || "").split(",").map((s) => s.trim()).filter(Boolean);
    });
    await api("/api/jobs/profile", body);
    toast("Saved — drafts can now claim these.");
    await refresh(); railCounts(); LOADERS.profile();
  }));
  f.appendChild(save);

  // the coverage column: every claim a draft may make, grouped
  const side = $("#profileSide");
  side.innerHTML = "";
  const groups = [
    ["Desired roles", p.target_roles], ["Skills", p.skills], ["Tools", p.technologies],
    ["Employment history", p.employers], ["Location preferences", p.locations_ok],
    ["Achievements", p.achievements],
  ];
  let total = 0;
  groups.forEach(([title, items]) => {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    total += list.length;
    const g = el("div", "cov-group");
    const h = el("div", "cov-head");
    h.append(el("span", "", title), el("span", "cov-n", String(list.length)));
    g.appendChild(h);
    if (!list.length) g.appendChild(el("div", "cov-empty", "Nothing yet — a draft can't mention any."));
    list.slice(0, 12).forEach((x) => g.appendChild(el("div", "cov-item", String(x))));
    if (list.length > 12) g.appendChild(el("div", "cov-empty", `and ${list.length - 12} more`));
    side.appendChild(g);
  });
  const c = profileCompleteness(p);
  const foot = el("div", "cov-foot");
  foot.innerHTML = `<b>${total}</b> claim${total === 1 ? "" : "s"} a draft may make · profile ${c.pct}% complete`
    + (c.missing.length ? `<br><span class="muted">Missing: ${c.missing.join(", ")}</span>` : "");
  side.appendChild(foot);
};

/* --- wiring ------------------------------------------------------------ */
async function boot() {
  $$(".nav").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  $("#roleFilter").addEventListener("input", (e) => { S.filter = e.target.value; drawRoleList(); });
  $("#roleSort").addEventListener("change", (e) => { S.sort = e.target.value; drawRoleList(); });
  $$("[data-bulk]").forEach((b) => b.addEventListener("click", () => bulk(b.dataset.bulk, b)));

  const railAuto = $("#railAuto");
  if (railAuto) railAuto.addEventListener("click", async (e) => {
    e.stopPropagation();                       // the switch, not the view
    const on = !((S.data || {}).auto || {}).enabled;
    try {
      await api("/api/jobs/auto", { enabled: on });
      await refresh(); railCounts();
      toast(on ? "Auto-apply on — rehearsal settings still apply." : "Auto-apply off.");
    } catch (err) { toast(err.message, "bad"); }
  });

  $("#eTest").addEventListener("click", (e) => busy(e.target, "Testing…", async () => {
    const r = await api("/api/engines/test", {
      base_url: engField("eUrl"), api_key: engField("eKey"), model: engField("eModel"),
    });
    engSay(r.ok ? `${r.detail} It replied: "${r.reply}"` : `${r.error} ${r.fix || ""}`,
           r.ok ? "ok" : "bad");
  }));
  $("#eSave").addEventListener("click", (e) => busy(e.target, "Saving…", async () => {
    const name = engField("eName");
    if (!name) { engSay("Give the engine a name.", "bad"); return; }
    const r = await api("/api/engines", {
      name, base_url: engField("eUrl"), api_key: engField("eKey"),
      model: engField("eModel"),
    });
    // the server warns when the model id and the endpoint disagree — show it
    engSay(r.message || "Saved.", /looks like/.test(r.message || "") ? "warn" : "ok");
    $("#eKey").value = "";
    LOADERS.engines(); refreshEnginePicker();
  }));

  $("#qGo").addEventListener("click", runSearch);
  $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

  $("#aSave").addEventListener("click", (e) => saveAuto(e.target));
  $("#aRun").addEventListener("click", (e) => busy(e.target, "Running…", async () => {
    const x = await api("/api/jobs/auto/run", { engine: S.engine });
    toast(x.summary || "Ran once.", (x.sent || []).length ? "" : "warn");
    // a run that sent nothing says why, rather than leaving you guessing
    if (x.why_nothing) await drawReadiness();
    const prepared = x.prepared || [];
    if (prepared.length) {
      const host = $("#autoReady");
      const box = el("div", "ready is-ok");
      box.appendChild(el("div", "ready-head",
        `${prepared.length} portal form(s) filled — finish them yourself`));
      prepared.forEach((p) => {
        const row = el("div", "ready-row");
        row.append(el("span", "ready-what", `${p.title} — ${p.company || ""}`),
                   el("span", "ready-fix", p.needs_you && p.needs_you.length
                      ? "you answer: " + p.needs_you.join("; ")
                      : `${p.answered} answer(s) filled in`));
        box.appendChild(row);
      });
      host.appendChild(box);
    }
    const held = x.held || [];
    if (held.length) {
      const host = $("#autoReady");
      const box = el("div", "ready is-blocked");
      box.appendChild(el("div", "ready-head", `${held.length} held this run`));
      held.slice(0, 6).forEach((hh) => {
        const row = el("div", "ready-row");
        row.append(el("span", "ready-what", `${hh.title} — ${hh.company || ""}`),
                   el("span", "ready-fix", hh.reason));
        box.appendChild(row);
      });
      host.appendChild(box);
    }
    (x.errors || []).slice(0, 3).forEach((m) => toast(String(m), "bad"));
    await refresh(); drawAutoPreview();
  }));
  $("#aPilot").addEventListener("click", (e) => busy(e.target, "Setting up…", async () => {
    const x = await api("/api/jobs/autopilot", {});
    toast(x.note || "The daily loop is set up.");
    LOADERS.auto();
  }));
  $("#aDaily").addEventListener("change", async (e) => {
    try {
      await api("/api/jobs/schedule", { enabled: e.target.checked });
      toast(e.target.checked ? "Daily scan on — it finds and drafts, never sends by itself."
                             : "Daily scan off.");
    } catch (err) { e.target.checked = !e.target.checked; toast(err.message, "bad"); }
  });

  $("#srcAdd").addEventListener("click", (e) => busy(e.target, "Checking…", async () => {
    const url = $("#srcUrl").value.trim();
    if (!url) return;
    const name = $("#srcName").value.trim()
      || url.replace(/^https?:\/\//, "").split("/")[0];
    await api("/api/jobs/sources", { name, url });
    $("#srcUrl").value = ""; $("#srcName").value = "";
    LOADERS.sources();
  }));
  $("#srcAuto").addEventListener("click", (e) => busy(e.target, "Checking boards…", async () => {
    const x = await api("/api/jobs/boards/auto", {});
    toast(`Added ${x.added ?? 0} board(s) that actually return roles.`);
    LOADERS.sources();
  }));
  $("#alertPaste").addEventListener("click", (e) => busy(e.target, "Reading…", async () => {
    const raw = $("#alertRaw").value.trim();
    if (!raw) return;
    const x = await api("/api/jobs/alerts/paste", { raw, sender: "" });
    $("#alertOut").textContent = `Found ${x.added ?? (x.roles || []).length} role(s) in it.`;
    $("#alertRaw").value = "";
  }));
  $("#alertFolder").addEventListener("click", (e) => busy(e.target, "Reading…", async () => {
    const x = await api("/api/jobs/alerts/folder", {});
    $("#alertOut").textContent = x.summary || `Read ${x.files ?? 0} file(s), ${x.added ?? 0} role(s).`;
  }));
  $("#alertGuide").addEventListener("click", async () => {
    try {
      const x = await api("/api/jobs/alerts/guide", undefined, "GET");
      $("#alertOut").textContent = (x.guide || []).join ? x.guide.join("\n") : (x.guide || x.why || "");
    } catch (e) { toast(e.message, "bad"); }
  });

  $("#tArchive").addEventListener("click", (e) => tidy("/api/jobs/archive",
    "Archiving…", { applied_before_days: 45 }, e.target,
    (x) => `Archived ${x.archived ?? x.count ?? 0}.`));
  $("#tDedupe").addEventListener("click", (e) => tidy("/api/jobs/dedupe",
    "Merging…", {}, e.target, (x) => `Merged ${x.merged ?? x.removed ?? 0} duplicate(s).`));
  $("#tSweep").addEventListener("click", (e) => tidy("/api/jobs/sweep",
    "Checking…", {}, e.target, (x) => x.summary || `Checked ${x.checked ?? 0}.`));
  $("#tPrune").addEventListener("click", (e) => tidy("/api/jobs/prune",
    "Pruning…", {}, e.target, (x) => `Pruned ${x.pruned ?? x.removed ?? 0}.`));

  try {
    const m = await api("/api/meta", undefined, "GET");
    $("#railSub").textContent = `by ${m.brand || "Symbolic Synapse"}`;
    S.profileReady = !!m.profile_ready;
    $("#build").textContent = m.build ? `build ${m.build}` : "";
    const sel = $("#engine");
    sel.innerHTML = "";
    ["Auto"].concat(m.engines || []).forEach((n) => {
      const o = el("option", "", n); o.value = n;
      if (n === m.default_engine) o.selected = true;
      sel.appendChild(o);
    });
    S.engine = sel.value;
    sel.addEventListener("change", () => { S.engine = sel.value; });
  } catch (e) { /* the window still works */ }

  show("overview");
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

/* --- the last few: nothing the job search can do is left without a door */
function wireRemaining() {
  $("#ovCycle").addEventListener("click", (e) => busy(e.target, "Running a cycle…", async () => {
    const x = await api("/api/jobs/cycle", { engine: S.engine });
    toast(x.summary || x.sentence || "Cycle complete.");
    LOADERS.overview();
  }));
  $("#ovDiscover").addEventListener("click", (e) => busy(e.target, "Looking…", async () => {
    const x = await api("/api/jobs/discover", {});
    toast(`Found ${x.added ?? x.found ?? 0} new role(s).`);
    LOADERS.overview();
  }));
  $("#ovAdd").addEventListener("click", async () => {
    const title = prompt("Role title?"); if (!title) return;
    const company = prompt("Company?") || "";
    const url = prompt("Link to the posting (optional)") || "";
    try {
      await api("/api/jobs/add", { roles: [{ title, company, url, summary: "" }] });
      toast("Added.");
      LOADERS.overview();
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#tClear").addEventListener("click", (e) => {
    if (!confirm("Remove every role that was never scored?")) return;
    tidy("/api/jobs/clear", "Clearing…", { never_scored: true }, e.target,
         (x) => `Cleared ${x.removed ?? x.cleared ?? 0}.`);
  });
  $("#tUnignore").addEventListener("click", (e) => tidy("/api/jobs/unignore",
    "…", { keys: [] }, e.target,
    (x) => `${x.restored ?? x.count ?? 0} role(s) can reappear in searches.`));
  $("#srcMatch").addEventListener("click", (e) => busy(e.target, "Matching…", async () => {
    const x = await api("/api/jobs/boards/match", undefined, "GET");
    const names = (x.boards || x.matches || x.suggested || []).map((b) => b.name || b).slice(0, 8);
    $("#srcMatchOut").textContent = names.length ? names.join(" · ")
                                                 : (x.note || "Fill in your profile first.");
  }));
}

const _prevAuto = LOADERS.auto;
LOADERS.auto = async function () {
  await _prevAuto();
  const host = $("#portalHistory");
  host.innerHTML = "";
  try {
    const h = await api("/api/jobs/portal/history", undefined, "GET");
    const runs = h.runs || [];
    if (!runs.length) {
      host.appendChild(el("p", "muted", h.ready === false
        ? "The portal applier needs a browser: run  playwright install chromium"
        : "None yet."));
    }
    runs.slice(0, 10).forEach((r) => {
      const it = el("div", "item static");
      const body = el("div");
      body.append(el("div", "item-t", r.title || r.company || "application"),
        el("div", "item-m", [r.company, r.when || r.at].filter(Boolean).join(" · ")));
      it.append(el("div", "mono", initial(r.company)), body,
        el("span", "tag" + (r.submitted ? " sent" : ""), r.submitted ? "submitted" : "rehearsed"));
      host.appendChild(it);
    });
  } catch (e) { host.appendChild(el("p", "muted", "Couldn't read the history.")); }
};

document.addEventListener("DOMContentLoaded", wireRemaining);
if (document.readyState !== "loading") setTimeout(wireRemaining, 0);
