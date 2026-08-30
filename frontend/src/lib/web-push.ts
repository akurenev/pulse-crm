import { api } from "./api";
import type {
  ApiPushSubscriptionDeletePayload,
  ApiPushSubscriptionPayload,
  ApiPushTestQueued,
} from "../types/api";

export type WebPushSupport = "supported" | "unsupported";

export function getWebPushSupport(): WebPushSupport {
  if (typeof window === "undefined" || typeof navigator === "undefined") return "unsupported";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return "unsupported";
  }
  return "supported";
}

export function isIosDevice(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

export function isStandalonePwa(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(display-mode: standalone)").matches
    || ("standalone" in navigator && (navigator as Navigator & { standalone?: boolean }).standalone === true);
}

export function base64UrlToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(base64);
  const output = new Uint8Array(new ArrayBuffer(decoded.length));
  for (let index = 0; index < decoded.length; index += 1) output[index] = decoded.charCodeAt(index);
  return output;
}

export function serializePushSubscription(subscription: PushSubscription): ApiPushSubscriptionPayload {
  const serialized = subscription.toJSON();
  const p256dh = serialized.keys?.p256dh;
  const auth = serialized.keys?.auth;
  if (!serialized.endpoint || !p256dh || !auth) {
    throw new Error("Браузер не предоставил ключи push-подписки");
  }
  return {
    endpoint: serialized.endpoint,
    expiration_time: serialized.expirationTime === null || serialized.expirationTime === undefined
      ? null
      : new Date(serialized.expirationTime).toISOString(),
    keys: { p256dh, auth },
  };
}

function applicationServerKeyMatches(subscription: PushSubscription, publicKey: string): boolean {
  const currentKey = subscription.options.applicationServerKey;
  if (!currentKey) return false;
  const currentBytes = new Uint8Array(currentKey);
  const expectedBytes = base64UrlToUint8Array(publicKey);
  if (currentBytes.byteLength !== expectedBytes.byteLength) return false;
  return currentBytes.every((byte, index) => byte === expectedBytes[index]);
}

async function getServiceWorkerRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (getWebPushSupport() === "unsupported") return null;
  return (await navigator.serviceWorker.getRegistration()) ?? null;
}

export async function getCurrentPushSubscription(): Promise<PushSubscription | null> {
  const registration = await getServiceWorkerRegistration();
  if (!registration) return null;
  return registration.pushManager.getSubscription();
}

export async function enableWebPush(publicKey: string): Promise<PushSubscription> {
  if (getWebPushSupport() === "unsupported") throw new Error("Push-уведомления не поддерживаются");

  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Разрешение на уведомления не предоставлено");

  const registration = await navigator.serviceWorker.ready;
  let current = await registration.pushManager.getSubscription();
  if (current && !applicationServerKeyMatches(current, publicKey)) {
    await current.unsubscribe();
    current = null;
  }
  const subscription = current ?? await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: base64UrlToUint8Array(publicKey),
  });

  try {
    await api.post<unknown>("/push/subscriptions", serializePushSubscription(subscription));
  } catch (error) {
    if (!current) await subscription.unsubscribe().catch(() => false);
    throw error;
  }
  return subscription;
}

export async function syncCurrentWebPush(publicKey: string): Promise<boolean> {
  if (getWebPushSupport() === "unsupported") return false;
  const registration = await getServiceWorkerRegistration();
  if (!registration) return false;

  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) return false;
  if (!applicationServerKeyMatches(subscription, publicKey)) {
    await subscription.unsubscribe();
    if (Notification.permission !== "granted") return false;
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToUint8Array(publicKey),
    });
  }
  await api.post<unknown>("/push/subscriptions", serializePushSubscription(subscription));
  return true;
}

export async function disableWebPush(): Promise<void> {
  const subscription = await getCurrentPushSubscription();
  if (!subscription) return;

  let serverError: unknown;
  try {
    const payload: ApiPushSubscriptionDeletePayload = { endpoint: subscription.endpoint };
    await api.delete("/push/subscriptions", payload);
  } catch (error) {
    serverError = error;
  }
  const removed = await subscription.unsubscribe();
  if (!removed) throw new Error("Браузер не подтвердил удаление push-подписки");
  if (serverError) throw serverError;
}

export async function cleanupWebPushBeforeLogout(): Promise<void> {
  if (getWebPushSupport() === "unsupported") return;
  try {
    await disableWebPush();
  } catch (error) {
    console.warn("Pulse CRM push cleanup failed", error);
  }
}

export async function sendTestWebPush(): Promise<ApiPushTestQueued> {
  return api.post<ApiPushTestQueued>("/push/test", {});
}
