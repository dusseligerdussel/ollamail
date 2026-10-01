/** Registers public/sw.js, which caches the app shell only (never API responses). */
export function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Offline support is optional; the app works without a service worker.
    });
  });
}
