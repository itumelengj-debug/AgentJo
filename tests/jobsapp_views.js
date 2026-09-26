/* Agent Jo Jobs — the window switches views and draws what the server sends.
 *
 * Replaces four harnesses that drove the old panel inside the main app. The
 * behaviour they protected still matters — views switch, rows render, the
 * blocked stage is marked — so it moved here rather than being dropped.
 */
const fs = require('fs');

function node(id) {
  const n = {
    id, children: [], hidden: false, textContent: "", value: "", type: "",
    dataset: {}, style: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); return !!on; },
      contains(c) { return this._s.has(c); },
    },
    set innerHTML(v) { if (v === "") this.children.length = 0; },
    get innerHTML() { return ""; },
    appendChild(c) { c._parent = this; this.children.push(c); return c; },
    append(...c) { c.forEach(x => { x._parent = this; }); this.children.push(...c); },
    _clicks: [], _changes: [],
    addEventListener(ev, fn) {
      if (ev === "click") this._clicks.push(fn);
      if (ev === "input" || ev === "change") this._changes.push(fn);
    },
    click() { this._clicks.forEach(f => f({ target: this, stopPropagation() {} })); },
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    focus() {}, remove() {}, scrollIntoView() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  Object.defineProperty(n, "className", {
    get() { return [...n.classList._s].join(" "); },
    set(v) { n.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return n;
}

const store = {};
const E = (id) => (store[id] = store[id] || node(id));

// the ids the window uses
const html = fs.readFileSync("web_jobs/index.html", "utf8");
(html.match(/id="([^"]+)"/g) || []).forEach((m) => E(m.slice(4, -1)));

const views = ["overview", "roles", "search", "drafts", "auto", "sources",
               "results", "archive", "profile"];
views.forEach((v) => E("v-" + v));
const navs = views.map((v) => { const n = node("nav-" + v); n.dataset.view = v; return n; });

global.document = {
  readyState: "complete",
  querySelector: (s) => store[s.replace("#", "")] || null,
  querySelectorAll: (s) => (s === ".nav" ? navs
    : s === ".view" ? views.map((v) => E("v-" + v))
    : s.startsWith("[data-bulk]") ? [] : []),
  createElement: () => node(""),
  addEventListener() {},
};
global.window = { open() {} };
// a real tick, not a synchronous call: the views load asynchronously and
// checking before they resolve tests nothing
const realSetTimeout = setTimeout;
global.setTimeout = (f, ms) => realSetTimeout(f, ms || 0);
const tick = () => new Promise((r) => realSetTimeout(r, 0));

// the server's real payload shapes, as observed from the running app
const PAYLOAD = {
  "/api/meta": { app: "Agent Jo Jobs", build: "x", brand: "Symbolic Synapse",
                 profile_ready: false, profile_note: "thin", engines: ["Local"],
                 default_engine: "Auto" },
  "/api/jobs/pipeline": {
    stages: { found: 4, scored: 2, drafted: 2, held: 2, applied: 0 },
    total: 4, blocked_at: "held", why: "2 drafts claim too much",
    counts: { screened: 1, no_address: 1 },
  },
  "/api/jobs": { profile: { skills: ["Python"] }, auto: { enabled: false },
    follow_ups: [], daily: false, roles: [
    { key: "a", title: "Senior Data Engineer", company: "Acme",
      source: "Remotive", stage: "found", fit: { score: 88 },
      apply_email: "a@b.io" },
    { key: "b", title: "BI Lead", company: "Zeta", stage: "held",
      fit: { score: 61 } },
  ] },
  "/api/jobs/claims": { held: [
    { key: "b", title: "BI Lead", company: "Zeta",
      problems: ["claims leading a team of 12 — not in your profile"],
      body: "Dear..." },
  ], claims: [] },
  "/api/jobs/outcomes": { sent: 0, replied: 0, overall: { low: 0, high: 0 },
                          findings: [], by_source: [] },
  "/api/jobs/search/config": { sources: [
    { name: "remotive.com", url: "https://remotive.com/feed", kind: "board" },
  ] },
  "/api/jobs/profile": { profile: { target_roles: ["Data Engineer"] },
                         ready: false, why: "thin" },
  "/api/jobs/auto/preview": { sentence: "It would send 0.", dry_run: true },
  "/api/jobs/sources/suggested": { suggested: [] },
};
const asked = [];
global.fetch = async (path, opts) => {
  asked.push(path);
  const key = path.split("?")[0];
  return { ok: true, status: 200, json: async () => PAYLOAD[key] || {} };
};

const src = fs.readFileSync("web_jobs/jobs.js", "utf8");
const api = new Function(src + "\n;return {show,S,boot};")();

(async () => {
  await api.boot();
  await tick(); await tick(); await tick();

  const on = views.filter((v) => E("v-" + v).classList.contains("is-on"));
  console.log("opens on the pipeline:", on.length === 1 && on[0] === "overview");

  const stages = E("track").children;
  console.log("pipeline draws every stage:", stages.length === 6);
  console.log("the blocked stage is marked:",
    stages.filter((s) => s.classList.contains("blocked")).length === 1);
  console.log("counts come from the server:",
    // the count sits inside the ring now
    stages[0].children[0].children[0].textContent === "4");

  api.show("roles");
  await tick(); await tick();
  console.log("switching views works:",
    E("v-roles").classList.contains("is-on")
    && !E("v-overview").classList.contains("is-on"));
  console.log("roles render:", E("roleList").children.length === 2);

  api.show("drafts");
  await tick(); await tick();
  console.log("held drafts render:", E("heldList").children.length === 1);

  api.show("sources");
  await tick(); await tick();
  console.log("sources render:", E("srcList").children.length === 1);

  const bad = asked.filter((p) => !Object.keys(PAYLOAD).includes(p.split("?")[0]));
  console.log("every call hits a known endpoint:", bad.length === 0, bad.join(","));
})();
