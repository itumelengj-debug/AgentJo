/* Agent Jo's phone companion. The key comes in the address's #fragment
 * (browsers never send that part over the network), is kept on this phone,
 * and is removed from the address bar so a screenshot doesn't show it. */
"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
let KEY = "";
(function readKey() {
  const m = location.hash.match(/k=([A-Za-z0-9_-]{20,})/);
  if (m) {
    KEY = m[1];
    try { localStorage.setItem("jo-agent-phone-key", KEY); } catch (e) {}
    history.replaceState(null, "", location.pathname);
  } else {
    try { KEY = localStorage.getItem("jo-agent-phone-key") || ""; } catch (e) {}
  }
})();

async function api(path, body) {
  const opts = { headers: { "X-Phone-Key": KEY } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(d.error || ("Error " + r.status));
  return d;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 4200);
}
async function busy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; }
}

async function load() {
  const main = $("#main");
  if (!KEY) {
    main.innerHTML = "";
    main.appendChild(el("p", "pad", "Open this page by scanning the code in Agent Jo on your computer (Settings → Phone companion)."));
    return;
  }
  let s;
  try { s = await api("/api/state"); }
  catch (e) { main.innerHTML = ""; main.appendChild(el("p", "pad error", e.message)); return; }
  $("#level").textContent = s.paused ? "Scheduled tasks paused from your phone."
    : `${s.running} scheduled task(s) on.`;
  main.innerHTML = "";

  main.appendChild(el("h2", "", `Waiting for your OK (${s.held.length})`));
  if (!s.held.length) main.appendChild(el("p", "empty", "Nothing waiting. Sending, submitting and posting wait here."));
  s.held.forEach((h) => {
    const c = el("div", "card");
    c.appendChild(el("h3", "", h.tool));
    c.appendChild(el("p", "", h.summary));
    c.appendChild(el("p", "muted", `${h.at} · from ${h.source || "agent"} · ${h.why}`));
    const det = el("details");
    det.appendChild(el("summary", "", "Exactly what would be sent"));
    det.appendChild(el("p", "letter", h.args));
    c.appendChild(det);
    const row = el("div", "row");
    const ok = el("button", "primary", "Approve and run");
    ok.addEventListener("click", () => busy(ok, async () => {
      if (!confirm(`Run ${h.tool}: ${h.summary}?`)) return;
      const x = await api("/api/approve", { id: h.id }); toast(x.then || "Done."); load();
    }));
    const no = el("button", "danger", "Refuse");
    no.addEventListener("click", () => busy(no, async () => {
      const x = await api("/api/refuse", { id: h.id }); toast(x.then || "Refused."); load();
    }));
    row.append(ok, no);
    c.appendChild(row);
    main.appendChild(c);
  });

  if ((s.recent || []).length) {
    main.appendChild(el("h2", "", "Decided lately"));
    s.recent.forEach((r) => {
      const c = el("div", "card");
      c.appendChild(el("p", "", `${r.tool} — ${r.summary}`));
      c.appendChild(el("p", "muted", r.state + (r.result ? ": " + r.result : "")));
      main.appendChild(c);
    });
  }

  main.appendChild(el("h2", "", `Scheduled tasks (${s.schedules.length})`));
  if (!s.schedules.length) main.appendChild(el("p", "empty", "None set up."));
  s.schedules.forEach((x) => {
    const c = el("div", "card");
    c.appendChild(el("h3", "", x.name || `Task ${x.id}`));
    const tags = el("div");
    tags.appendChild(el("span", "tag" + (x.enabled ? " ok" : ""), x.enabled ? "on" : "off"));
    if (x.last_status) tags.appendChild(el("span", "tag" + (x.last_status === "ok" ? "" : " warn"), "last: " + x.last_status));
    c.appendChild(tags);
    if (x.last_summary) c.appendChild(el("p", "muted", x.last_summary));
    main.appendChild(c);
  });

  const p = s.paused ? el("button", "pause", "Resume the tasks I paused")
                     : el("button", "danger pause", "Pause every scheduled task");
  p.addEventListener("click", () => busy(p, async () => {
    if (!s.paused && !confirm("Pause every scheduled task until you resume them?")) return;
    const x = await api(s.paused ? "/api/resume" : "/api/pause", {});
    toast(s.paused ? `Resumed ${x.resumed}.` : `Paused ${x.paused}.`); load();
  }));
  if (s.running || s.paused) main.appendChild(p);
}

document.addEventListener("DOMContentLoaded", () => {
  $("#reload").addEventListener("click", load);
  load();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
