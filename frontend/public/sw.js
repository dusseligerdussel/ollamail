// ollamail service worker: caches the app shell (HTML, JS, CSS, fonts, icons) for fast starts and
// offline launch. It deliberately never caches API responses, so no mail content, subjects or
// addresses end up in the browser cache (see docs/PRIVACY.md).

const CACHE = "ollamail-shell-v1";
const SHELL_URL = "/index.html";
const PRECACHE = [SHELL_URL, "/theme-init.js", "/favicon.svg", "/manifest.webmanifest"];

/** Static, content-hashed or public files that make up the shell. */
function isShellAsset(url) {
  return (
    url.pathname.startsWith("/assets/") ||
    url.pathname.startsWith("/icons/") ||
    PRECACHE.includes(url.pathname)
  );
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  // Only same-origin shell files; everything else (in particular /api) goes straight to the network.
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    // Network first so deployments are picked up immediately; the cached shell is the offline fallback.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            void caches.open(CACHE).then((cache) => cache.put(SHELL_URL, copy));
          }
          return response;
        })
        .catch(() => caches.match(SHELL_URL).then((cached) => cached ?? Response.error())),
    );
    return;
  }

  if (!isShellAsset(url)) return;

  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ??
        fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            void caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        }),
    ),
  );
});
