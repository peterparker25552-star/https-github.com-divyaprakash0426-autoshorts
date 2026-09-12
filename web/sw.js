/* Qyro service worker — installable-PWA polish, nothing more.
 *
 * Strategy: network first for everything. Media and API responses are never
 * cached (they are big, and clips are frequently re-rendered); only the app
 * shell is kept so an installed icon opened with the server stopped shows a
 * real page instead of a browser error. Versioned cache name = clean upgrade.
 */
// Bump this whenever the shell assets change so an installed PWA cannot keep
// the previous intro/logo in its offline cache.
const SHELL = "qyro-v0.6.8-shell";
const ASSETS = ["/", "/static/app.js", "/static/intro.js", "/static/style.css",
                "/static/manifest.webmanifest",
                "/static/icons/favicon.svg", "/static/icons/icon-192.png",
                "/static/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(ASSETS)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;            // always live

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && ASSETS.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put(request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then((hit) => hit || caches.match("/")).then((hit) =>
          hit || new Response("Qyro server is not running. Start it with: ./run.sh", {
            status: 503, headers: { "Content-Type": "text/plain" },
          })
        )
      )
  );
});
