/* Agent Jo — service worker.
 *
 * Its only job is to make the app installable and to make the shell load
 * instantly on a phone. It deliberately does NOT cache API responses.
 *
 * That restraint is the whole design. This is a control surface for an agent
 * doing real work on another machine: a cached dashboard would show a token
 * count from yesterday, a cached health board would show green for a service
 * that has since fallen over, and a cached "needs you" list would hide a
 * decision waiting on you. Stale data here is worse than no data, so anything
 * under /api goes to the network every time and fails honestly when the PC
 * isn't reachable.
 */
const SHELL = "agentjo-shell-v1";
const SHELL_FILES = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;                       // let the network handle it
  }
  // Never serve a cached answer for live state.
  if (url.pathname.startsWith("/api/")) return;

  // Shell files: use the network, fall back to cache only if it's unreachable,
  // and keep the cache fresh so a new build isn't masked by an old one.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(event.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) =>
        hit || new Response(
          "Agent Jo can't reach your computer. Check it's switched on, " +
          "awake, and on the same network.",
          { status: 503, headers: { "Content-Type": "text/plain" } })))
  );
});
