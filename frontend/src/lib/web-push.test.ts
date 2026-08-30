import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  base64UrlToUint8Array,
  cleanupWebPushBeforeLogout,
  disableWebPush,
  enableWebPush,
  getCurrentPushSubscription,
  serializePushSubscription,
  syncCurrentWebPush,
} from "./web-push";

const { apiDeleteMock, apiPostMock } = vi.hoisted(() => ({
  apiDeleteMock: vi.fn(),
  apiPostMock: vi.fn(),
}));

vi.mock("./api", () => ({
  api: {
    delete: apiDeleteMock,
    post: apiPostMock,
  },
}));

const originalNotification = Object.getOwnPropertyDescriptor(globalThis, "Notification");
const originalPushManager = Object.getOwnPropertyDescriptor(window, "PushManager");
const originalServiceWorker = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");

function makeSubscription(overrides: Partial<PushSubscription> = {}): PushSubscription {
  return {
    endpoint: "https://push.example.test/subscription",
    expirationTime: null,
    options: {
      applicationServerKey: new Uint8Array([1, 2, 3, 251, 255]).buffer,
      userVisibleOnly: true,
    },
    getKey: vi.fn(),
    toJSON: () => ({
      endpoint: "https://push.example.test/subscription",
      expirationTime: null,
      keys: { p256dh: "public-key", auth: "auth-secret" },
    }),
    unsubscribe: vi.fn().mockResolvedValue(true),
    ...overrides,
  };
}

function installPushApi({ current, created = makeSubscription() }: { current: PushSubscription | null; created?: PushSubscription }) {
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(current),
    subscribe: vi.fn().mockResolvedValue(created),
  };
  const registration = { pushManager } as unknown as ServiceWorkerRegistration;
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      getRegistration: vi.fn().mockResolvedValue(registration),
      ready: Promise.resolve(registration),
    },
  });
  return { created, pushManager, registration };
}

beforeEach(() => {
  apiDeleteMock.mockReset().mockResolvedValue(undefined);
  apiPostMock.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(window, "PushManager", { configurable: true, value: class PushManagerMock {} });
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: { permission: "default", requestPermission: vi.fn().mockResolvedValue("granted") },
  });
});

afterEach(() => {
  if (originalNotification) Object.defineProperty(globalThis, "Notification", originalNotification);
  else Reflect.deleteProperty(globalThis, "Notification");
  if (originalPushManager) Object.defineProperty(window, "PushManager", originalPushManager);
  else Reflect.deleteProperty(window, "PushManager");
  if (originalServiceWorker) Object.defineProperty(navigator, "serviceWorker", originalServiceWorker);
  else Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("web push helpers", () => {
  it("converts a URL-safe VAPID key to bytes", () => {
    expect(Array.from(base64UrlToUint8Array("AQID-_8"))).toEqual([1, 2, 3, 251, 255]);
  });

  it("serializes the browser subscription for the API", () => {
    const subscription = makeSubscription({
      expirationTime: Date.UTC(2026, 7, 30, 12, 0, 0),
      toJSON: () => ({
        endpoint: "https://push.example.test/subscription",
        expirationTime: Date.UTC(2026, 7, 30, 12, 0, 0),
        keys: { p256dh: "public-key", auth: "auth-secret" },
      }),
    });

    expect(serializePushSubscription(subscription)).toEqual({
      endpoint: "https://push.example.test/subscription",
      expiration_time: "2026-08-30T12:00:00.000Z",
      keys: { p256dh: "public-key", auth: "auth-secret" },
    });
  });

  it("requests permission, subscribes through the ready worker, and registers with the server", async () => {
    const { created, pushManager } = installPushApi({ current: null });

    await expect(enableWebPush("AQID-_8")).resolves.toBe(created);

    expect(Notification.requestPermission).toHaveBeenCalledOnce();
    expect(pushManager.subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: new Uint8Array([1, 2, 3, 251, 255]),
    });
    expect(apiPostMock).toHaveBeenCalledWith("/push/subscriptions", {
      endpoint: "https://push.example.test/subscription",
      expiration_time: null,
      keys: { p256dh: "public-key", auth: "auth-secret" },
    });
  });

  it("replaces a subscription after VAPID key rotation", async () => {
    const oldSubscription = makeSubscription({
      options: {
        applicationServerKey: new Uint8Array([9, 9, 9]).buffer,
        userVisibleOnly: true,
      },
    });
    const { created, pushManager } = installPushApi({ current: oldSubscription });

    await expect(enableWebPush("AQID-_8")).resolves.toBe(created);

    expect(oldSubscription.unsubscribe).toHaveBeenCalledOnce();
    expect(pushManager.subscribe).toHaveBeenCalledOnce();
    expect(apiPostMock).toHaveBeenCalledWith("/push/subscriptions", expect.objectContaining({
      endpoint: created.endpoint,
    }));
  });

  it("re-syncs an existing local subscription with the server", async () => {
    const subscription = makeSubscription();
    const { pushManager } = installPushApi({ current: subscription });

    await expect(syncCurrentWebPush("AQID-_8")).resolves.toBe(true);

    expect(pushManager.subscribe).not.toHaveBeenCalled();
    expect(apiPostMock).toHaveBeenCalledWith("/push/subscriptions", {
      endpoint: "https://push.example.test/subscription",
      expiration_time: null,
      keys: { p256dh: "public-key", auth: "auth-secret" },
    });
  });

  it("finds and removes the current subscription locally even if server cleanup fails", async () => {
    const subscription = makeSubscription();
    installPushApi({ current: subscription });
    apiDeleteMock.mockRejectedValueOnce(new Error("offline"));

    await expect(disableWebPush()).rejects.toThrow("offline");

    expect(apiDeleteMock).toHaveBeenCalledWith("/push/subscriptions", { endpoint: subscription.endpoint });
    expect(subscription.unsubscribe).toHaveBeenCalledOnce();
  });

  it("keeps logout cleanup best-effort and never rejects", async () => {
    const subscription = makeSubscription();
    installPushApi({ current: subscription });
    apiDeleteMock.mockRejectedValueOnce(new Error("offline"));
    const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    await expect(cleanupWebPushBeforeLogout()).resolves.toBeUndefined();
    expect(subscription.unsubscribe).toHaveBeenCalledOnce();
    expect(warning).toHaveBeenCalledOnce();
  });

  it("returns no subscription when no worker is registered", async () => {
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { getRegistration: vi.fn().mockResolvedValue(undefined) },
    });
    await expect(getCurrentPushSubscription()).resolves.toBeNull();
  });
});
