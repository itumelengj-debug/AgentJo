/* Runs in <head>, before the stylesheet paints anything.

   The theme used to be resolved by an inline <script> in index.html, and the
   page's own security policy (script-src 'self') blocks inline scripts — so
   under the real server it never ran, and every load painted the default
   theme until app.js caught up. A script from this origin is allowed. */
(function () {
  var root = document.documentElement;
  try {
    /* must agree with currentTheme() in app.js: Glass by default, and
       "System" in dark mode is Glass too */
    var c = localStorage.getItem("agentjo-theme");
    if (!localStorage.getItem("agentjo-theme-glass-default")
        && (!c || c === "system")) c = "glass";
    c = c || "glass";
    var r = c;
    if (c === "system") {
      r = (window.matchMedia &&
           window.matchMedia("(prefers-color-scheme: dark)").matches)
          ? "glass" : "fluent-light";
    }
    if (r !== "instruments") root.setAttribute("data-theme", r);
    /* the look, chosen in Settings: "new" from build 152 on, or "previous" */
    root.setAttribute("data-look",
      localStorage.getItem("agentjo-look") === "previous" ? "previous" : "new");
  } catch (e) {}
})();
