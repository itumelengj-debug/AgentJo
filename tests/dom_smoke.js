const fs=require('fs');
const html=fs.readFileSync('web/static/index.html','utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
const made={};
function mk(id){
  return made[id] || (made[id]={id, hidden:true, style:{}, dataset:{},
    classList:{add(){},remove(){},toggle(){},contains(){return false}},
    children:[], value:'', textContent:'', innerHTML:'', checked:false,
    addEventListener(){}, appendChild(){}, append(){}, prepend(){},
    querySelector(){return null}, querySelectorAll(){return []},
    setAttribute(){}, removeAttribute(){}, getAttribute(){return null},
    focus(){}, click(){}, remove(){}, insertBefore(){}, scrollIntoView(){}});
}
const mq={matches:false,addEventListener(){},addListener(){},removeEventListener(){}};
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({}),text:()=>Promise.resolve('')}),
  addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
global.document={documentElement:mk('html'), body:mk('body'),
  getElementById:id=>ids.includes(id)?mk(id):null,
  querySelector:sel=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(sel);return m?(ids.includes(m[1])?mk(m[1]):null):mk('g');},
  querySelectorAll:()=>[], createElement:()=>mk('new'), addEventListener(){},
  createTextNode:()=>({})};
global.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
global.fetch=global.window.fetch;
global.navigator={userAgent:'node',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{}; global.confirm=()=>true;
global.setTimeout=(f)=>0; global.setInterval=()=>0; global.matchMedia=()=>mq;

const src=fs.readFileSync('web/static/app.js','utf8');
try{
  const api=new Function(src+"\n;return {wireEvents:typeof wireEvents!=='undefined'?wireEvents:null};")();
  console.log("script evaluates:", true);
  if(api.wireEvents){
    try{ api.wireEvents(); console.log("wireEvents() completed:", true); }
    catch(e){ console.log("*** wireEvents THREW:", e.constructor.name, "-", e.message); }
  } else console.log("*** wireEvents missing");
}catch(e){ console.log("*** script failed to evaluate:", e.constructor.name, "-", e.message); }

// ---------------------------------------------------------------------------
// Beyond "does it wire up": does the dashboard actually POPULATE?
// Two bugs shipped from the same blind spot — code that existed but nothing
// reachable ever called it. Defining a renderer proves nothing; running it
// against a realistic payload and finding DOM children does.
// ---------------------------------------------------------------------------
const PAYLOAD = {
  at: "12:00 UTC", all_clear: false,
  counts: { act: 1, review: 1, note: 0 },
  needs_you: [
    { severity: "act", title: "No backup exists", detail: "one disk",
      panel: "backupBtn", why: "one click" },
    { severity: "review", title: "Trends waiting", detail: "2 pending",
      panel: "trendsBtn", why: "" },
  ],
  running: { engine: "Auto", spend: { usd: 1.2, cap: 10, pct: 12,
             blocked: false }, top_spender: "trends",
             next_job: { name: "x", in_minutes: 30, overdue: false },
             active_schedules: 2, privacy: "off" },
  tiles: [
    { label: "Tokens this month", value: "2.01M", sub: "1.6M in \u00b7 390K out",
      spark: [1,4,2,8,3,9,5,2,7,4,6,3,8,5], tone: "", panel: "", title: "t" },
    { label: "Spend", value: "$1.20", sub: "of $10.00", tone: "good",
      progress: 12, panel: "", title: "" },
    { label: "Capabilities used", value: "3/21", sub: "here", tone: "warn",
      progress: 14, panel: "capsBtn", title: "" },
  ],
};

// a DOM stub that actually records structure, so emptiness is detectable
function node(tag) {
  const n = { tag, children: [], attrs: {}, style: {}, classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    set innerHTML(v) { if (v === "") this.children.length = 0; },
    get innerHTML() { return ""; },
    textContent: "", hidden: false, type: "", title: "", href: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    addEventListener() {}, setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; }, getAttribute() { return null; },
    focus() {}, click() {}, remove() {}, querySelector() { return null; },
    querySelectorAll() { return []; },
    get clientWidth() { return 1200; },
    get parentElement() { return null; } };
  return n;
}
const store = {};
const el2 = (id) => store[id] || (store[id] = node(id));
global.document.getElementById = (id) => el2(id);
global.document.querySelector = (sel) => {
  const m = /^#([A-Za-z0-9_-]+)$/.exec(sel);
  return m ? el2(m[1]) : node("generic");
};
global.document.createElement = (t) => node(t);
global.document.createElementNS = (ns, t) => node(t);
global.fetch = (url) => Promise.resolve({ ok: true,
  json: () => Promise.resolve(url.includes("/api/dashboard") ? PAYLOAD : {}) });

(async () => {
  const api2 = new Function(src +
    "\n;return {refreshDashboard: typeof refreshDashboard!=='undefined'?refreshDashboard:null};")();
  if (!api2.refreshDashboard) { console.log("*** refreshDashboard missing"); return; }
  try {
    await api2.refreshDashboard();
    const att = el2("dashAttention").children.length;
    const run = el2("dashRunning").children.length;
    const tiles = el2("dashTiles").children.length;
    console.log("dashboard renders attention rows:", att === 2);
    console.log("dashboard renders running chips:", run > 0);
    console.log("dashboard renders tiles:", tiles === 3);
    console.log("dashboard visible after render:", el2("dash").hidden === false);
  } catch (e) {
    console.log("*** refreshDashboard THREW:", e.constructor.name, "-", e.message);
  }
  // the renderer is useless if nothing reachable calls it
  const calledFromPoll = /async function refreshStats\(\)[\s\S]{0,700}refreshDashboard\(\)/.test(src);
  console.log("dashboard refreshed by the stats poll:", calledFromPoll);
})();
