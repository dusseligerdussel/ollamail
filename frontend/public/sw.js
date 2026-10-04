// ollamail service worker: caches the app shell (HTML, JS, CSS, fonts, icons) for fast starts and
// offline launch. It deliberately never caches API responses, so no mail content, subjects or
// addresses end up in the browser cache (see docs/PRIVACY.md).
//
// It also shows notifications received through Web Push (#181) while no tab is in use. A push
// holds only IDs; what the notification shows comes from GET /api/notifications/messages/{id}.

const CACHE = "ollamail-shell-v1";
// UI strings for notifications in the user's language, handed over by the app (no mail data).
const STRINGS_CACHE = "ollamail-strings-v1";
const STRINGS_URL = "/__ollamail/notification-strings";
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
        Promise.all(
          keys
            .filter((key) => key !== CACHE && key !== STRINGS_CACHE)
            .map((key) => caches.delete(key)),
        ),
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

// --- Web Push notifications ------------------------------------------------------------------

// Used until the app has handed over the strings in the user's language.
const DEFAULT_STRINGS = {
  unknownSender: "Unknown sender",
  fallbackTitle: "ollamail",
  fallbackBody: "New mail. Open ollamail to see it.",
  categories: {},
};

self.addEventListener("message", (event) => {
  const data = event.data;
  if (data?.type !== "ollamail:notification-strings" || typeof data.strings !== "object") {
    return;
  }
  event.waitUntil(
    caches
      .open(STRINGS_CACHE)
      .then((cache) => cache.put(STRINGS_URL, new Response(JSON.stringify(data.strings)))),
  );
});

async function notificationStrings() {
  try {
    const cached = await caches.match(STRINGS_URL, { cacheName: STRINGS_CACHE });
    if (cached) return { ...DEFAULT_STRINGS, ...(await cached.json()) };
  } catch {
    // Fall back to the defaults.
  }
  return DEFAULT_STRINGS;
}

/** True if a tab of ollamail is visible and focused: the app is in use, no notification. */
async function appInUse() {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return windows.some((client) => client.visibilityState === "visible" && client.focused);
}

function mailUrl(messageId) {
  return `/inbox?message=${encodeURIComponent(messageId)}`;
}

async function showMailNotification(payload) {
  if (await appInUse()) return;
  const messageId = payload.message_id;
  const strings = await notificationStrings();
  // Same tag as notifications from an open tab: a mail is shown once, never twice.
  const options = { tag: `ollamail-message-${messageId}`, icon: "/icons/icon-192.png" };
  let title = strings.fallbackTitle;
  let body = strings.fallbackBody;
  let silent = true;
  try {
    const response = await fetch(`/api/notifications/messages/${encodeURIComponent(messageId)}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.ok) {
      const content = await response.json();
      const category = content.category
        ? strings.categories[content.category.builtin_key] || content.category.name
        : null;
      title = (content.sender || "").trim() || strings.unknownSender;
      body = [category, content.subject].filter((part) => part?.trim()).join(" · ");
      silent = !content.sound;
    }
  } catch {
    // Offline or signed out: the neutral text, without details.
  }
  await self.registration.showNotification(title, {
    ...options,
    body,
    silent,
    data: { url: mailUrl(messageId) },
  });
}

self.addEventListener("push", (event) => {
  let payload;
  try {
    payload = event.data?.json();
  } catch {
    return;
  }
  if (payload?.type !== "notification.message" || typeof payload.message_id !== "string") {
    return;
  }
  event.waitUntil(showMailNotification(payload));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url;
  if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) return;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (open) {
        await open.focus();
        await open.navigate(url).catch(() => undefined);
        return;
      }
      await self.clients.openWindow(url);
    })(),
  );
});
