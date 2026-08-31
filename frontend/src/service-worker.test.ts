import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

import { describe, expect, it, vi } from "vitest";

type NotificationClickHandler = (event: {
  notification: { data?: { url?: unknown }; close: () => void };
  waitUntil: (promise: Promise<unknown>) => void;
}) => void;

function loadWorker(windowClients: Array<Record<string, unknown>> = []) {
  const handlers = new Map<string, unknown>();
  const openWindow = vi.fn(async () => undefined);
  const worker = {
    location: { origin: "https://crm.example.test" },
    addEventListener: (type: string, handler: unknown) => handlers.set(type, handler),
    skipWaiting: vi.fn(),
    registration: { showNotification: vi.fn() },
    clients: {
      claim: vi.fn(),
      matchAll: vi.fn(async () => windowClients),
      openWindow,
    },
  };
  const source = readFileSync(resolve(process.cwd(), "public/sw.js"), "utf8");
  vm.runInNewContext(source, { self: worker, URL, Set, decodeURIComponent, encodeURIComponent });
  return { handler: handlers.get("notificationclick") as NotificationClickHandler, openWindow };
}

async function click(handler: NotificationClickHandler, url: unknown) {
  let completion: Promise<unknown> | undefined;
  const close = vi.fn();
  handler({
    notification: { data: { url }, close },
    waitUntil: (promise) => { completion = promise; },
  });
  await completion;
  return close;
}

describe("service worker notification navigation", () => {
  it("focuses an existing app window after navigating to a canonical deal deep link", async () => {
    const focus = vi.fn(async () => undefined);
    const navigate = vi.fn(async () => ({ focus }));
    const { handler, openWindow } = loadWorker([{ url: "https://crm.example.test/tasks", navigate, focus }]);

    const close = await click(handler, "/deals/deal-123");

    expect(close).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith("https://crm.example.test/deals?deal=deal-123");
    expect(focus).toHaveBeenCalledOnce();
    expect(openWindow).not.toHaveBeenCalled();
  });

  it.each([
    "https://attacker.example/tasks/task-123",
    "https://user:password@crm.example.test/tasks/task-123",
    "/api/v1/deals/deal-123",
    "/tasks/../../settings",
    "/deals/%",
    "/deals/%2e%2e/settings",
  ])("falls back to the app root for an unsafe target: %s", async (target) => {
    const { handler, openWindow } = loadWorker();
    await click(handler, target);
    expect(openWindow).toHaveBeenCalledWith("https://crm.example.test/");
  });

  it("opens a canonical task URL when no application window exists", async () => {
    const { handler, openWindow } = loadWorker();
    await click(handler, "/tasks?task=task_456&ignored=true");
    expect(openWindow).toHaveBeenCalledWith("https://crm.example.test/tasks?task=task_456");
  });

  it("opens a new window when an existing application client cannot navigate", async () => {
    const navigate = vi.fn().mockRejectedValue(new Error("stale client"));
    const focus = vi.fn();
    const { handler, openWindow } = loadWorker([{ url: "https://crm.example.test/", navigate, focus }]);

    await click(handler, "/deals?deal=deal-123");

    expect(openWindow).toHaveBeenCalledWith("https://crm.example.test/deals?deal=deal-123");
    expect(focus).not.toHaveBeenCalled();
  });
});
