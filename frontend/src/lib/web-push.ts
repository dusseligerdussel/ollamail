/**
 * Web Push (#181): notifications on this device without an open tab. The browser subscribes
 * at its vendor's push service (Chrome: Google, Firefox: Mozilla, Safari: Apple, Edge:
 * Microsoft); the server pushes only IDs, and the service worker (public/sw.js) fetches what
 * it shows from ollamail itself.
 *
 * Thin wrappers around the Push API, so components and tests do not touch the globals.
 */

/** Strings the service worker shows; the app hands them over in the user's language. */
export interface ServiceWorkerStrings {
  unknownSender: string;
  /** Shown when the details cannot be loaded (signed out, mail gone). */
  fallbackTitle: string;
  fallbackBody: string;
  /** Names of the built-in categories by key. */
  categories: Record<string, string>;
}

export const SW_STRINGS_MESSAGE = "ollamail:notification-strings";

/** Remembers which registered device is this browser, to forget it when signing out. */
const DEVICE_ID_KEY = "ollamail.pushDeviceId";

export function webPushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) return undefined;
  if (!("PushManager" in window)) return undefined;
  try {
    return await navigator.serviceWorker.getRegistration();
  } catch {
    return undefined;
  }
}

/** True if the service worker that receives pushes is installed (production builds). */
export async function webPushReady(): Promise<boolean> {
  return webPushSupported() && (await registration()) !== undefined;
}

export function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64 + "=".repeat((4 - (base64.length % 4)) % 4));
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function sameKey(subscription: PushSubscription, publicKey: string): boolean {
  const key = subscription.options?.applicationServerKey;
  if (!key) return false;
  const current = new Uint8Array(key);
  const expected = base64UrlToBytes(publicKey);
  return current.length === expected.length && current.every((byte, i) => byte === expected[i]);
}

/**
 * This browser's subscription, if it was made with the server's current key. One made with
 * another key (the administrator changed it) can no longer receive anything.
 */
export async function currentPushSubscription(
  publicKey: string,
): Promise<PushSubscription | undefined> {
  const subscription = await (await registration())?.pushManager.getSubscription();
  if (!subscription || !sameKey(subscription, publicKey)) return undefined;
  return subscription;
}

/** Subscribes this browser (must run in a click handler: it may ask for permission). */
export async function subscribePush(publicKey: string): Promise<PushSubscription> {
  const reg = await registration();
  if (!reg) throw new Error("No service worker");
  const existing = await reg.pushManager.getSubscription();
  if (existing && !sameKey(existing, publicKey)) await existing.unsubscribe();
  return reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: base64UrlToBytes(publicKey),
  });
}

export async function unsubscribePush(): Promise<void> {
  const subscription = await (await registration())?.pushManager.getSubscription();
  await subscription?.unsubscribe();
}

/** Endpoint and keys in the shape the API expects. */
export function subscriptionBody(subscription: PushSubscription) {
  const json = subscription.toJSON();
  return {
    endpoint: subscription.endpoint,
    keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" },
  };
}

export function rememberDevice(id: string | null) {
  try {
    if (id) localStorage.setItem(DEVICE_ID_KEY, id);
    else localStorage.removeItem(DEVICE_ID_KEY);
  } catch {
    // Storage blocked: the device is then only removed through the list.
  }
}

export function rememberedDevice(): string | null {
  try {
    return localStorage.getItem(DEVICE_ID_KEY);
  } catch {
    return null;
  }
}

/** Hands the strings in the user's language to the service worker. */
export async function sendServiceWorkerStrings(strings: ServiceWorkerStrings): Promise<void> {
  const reg = await registration();
  reg?.active?.postMessage({ type: SW_STRINGS_MESSAGE, strings });
}
