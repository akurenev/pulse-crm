import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

const originalFetch = Object.getOwnPropertyDescriptor(globalThis, "fetch");
const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset().mockResolvedValue(new Response(null, { status: 204 }));
  Object.defineProperty(globalThis, "fetch", { configurable: true, value: fetchMock });
  api.setCsrf("csrf-test");
});

afterEach(() => {
  api.setCsrf("");
  if (originalFetch) Object.defineProperty(globalThis, "fetch", originalFetch);
  else Reflect.deleteProperty(globalThis, "fetch");
});

describe("api.delete", () => {
  it("sends an optional JSON body for Web Push subscription cleanup", async () => {
    await api.delete("/push/subscriptions", { endpoint: "https://push.example.test/subscription" });

    expect(fetchMock).toHaveBeenCalledWith("/api/v1/push/subscriptions", expect.objectContaining({
      method: "DELETE",
      body: JSON.stringify({ endpoint: "https://push.example.test/subscription" }),
      credentials: "include",
      headers: expect.objectContaining({
        "Content-Type": "application/json",
        "X-CSRF-Token": "csrf-test",
      }),
    }));
  });
});

describe("api.postBlob", () => {
  it("downloads binary data with the session cookie and CSRF protection", async () => {
    fetchMock.mockResolvedValueOnce(new Response("test-xlsx", { headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" } }));
    const blob = await api.postBlob("/deals/export", { month: "2026-10" });
    expect(blob.size).toBe(9);
    expect(blob.type).toBe("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/deals/export", expect.objectContaining({
      method: "POST",
      credentials: "include",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-test" }),
      body: JSON.stringify({ month: "2026-10" }),
    }));
  });
});
