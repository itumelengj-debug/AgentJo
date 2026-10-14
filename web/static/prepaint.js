/* Runs in <head>, before the stylesheet paints anything.

   The theme used to be resolved by an inline <script> in index.html, and the
   page's own security policy (script-src 'self') blocks inline scripts — so
   under the real server it never ran, and every load painted the default
   theme until app.js caught up. A script from this origin is allowed. */
(function () {
  var root = document.documentElement;
  /* themes drawn as a skin over Glass: must agree with THEMES in app.js */
  var SKINS = { synapse: "synapse" };
  try {
    /* must agree with currentTheme() in app.js, which also saves what it
       decides: each one-time move, in order, then Symbolic Synapse (the
       main look) when nothing is chosen */
    var ls = localStorage;
    var c = ls.getItem("agentjo-theme");
    if (!ls.getItem("agentjo-theme-glass-migrated")
        && (!c || c === "fluent" || c === "system")) c = "glass";
    if (!ls.getItem("agentjo-theme-glass-default")
        && (!c || c === "system")) c = "glass";
    if (!ls.getItem("agentjo-theme-synapse-default")
        && (!c || c === "glass")) c = "synapse";
    c = c || "synapse";
    var r = c;
    if (c === "system") {
      /* System follows Windows: the main look when it's dark */
      r = (window.matchMedia &&
           window.matchMedia("(prefers-color-scheme: dark)").matches)
          ? "synapse" : "fluent-light";
    }
    if (SKINS[r]) {
      root.setAttribute("data-skin", SKINS[r]);
      r = "glass";
    } else {
      root.removeAttribute("data-skin");
    }
    if (r !== "instruments") root.setAttribute("data-theme", r);
    else root.removeAttribute("data-theme");
    /* one look now: the previous one (the switch was removed) */
    root.setAttribute("data-look", "previous");
  } catch (e) {}
})();
