import { describe, expect, it } from "vitest";

import { deepLinkEntityId, normalizeInternalAppPath } from "./deep-links";

describe("deep links", () => {
  it("normalizes supported deal and task routes", () => {
    const origin = "https://crm.example.test";
    expect(normalizeInternalAppPath("/deals/deal-123", origin)).toBe("/deals?deal=deal-123");
    expect(normalizeInternalAppPath("https://crm.example.test/tasks?task=task_456&ignored=true", origin))
      .toBe("/tasks?task=task_456");
    expect(normalizeInternalAppPath("/contacts?contact=contact-789&ignored=true", origin))
      .toBe("/contacts?contact=contact-789");
    expect(normalizeInternalAppPath("/contacts?company=company_789", origin))
      .toBe("/contacts?company=company_789");
    expect(normalizeInternalAppPath("/contacts?unused=true", origin)).toBe("/contacts");
  });

  it("rejects cross-origin, unknown and unsafe entity targets", () => {
    const origin = "https://crm.example.test";
    expect(normalizeInternalAppPath("https://attacker.example/deals/deal-123", origin)).toBeNull();
    expect(normalizeInternalAppPath("https://user:password@crm.example.test/deals/deal-123", origin)).toBeNull();
    expect(normalizeInternalAppPath("//attacker.example/tasks/task-123", origin)).toBeNull();
    expect(normalizeInternalAppPath("/api/v1/deals/deal-123", origin)).toBeNull();
    expect(normalizeInternalAppPath("/deals/../settings", origin)).toBeNull();
    expect(normalizeInternalAppPath("/deals/%", origin)).toBeNull();
    expect(normalizeInternalAppPath("/deals/%2e%2e/settings", origin)).toBeNull();
    expect(deepLinkEntityId(new URLSearchParams("deal=../../settings"), "deal")).toBeNull();
    expect(deepLinkEntityId(new URLSearchParams("contact=../../settings"), "contact")).toBeNull();
    expect(normalizeInternalAppPath("/contacts?company=../../settings", origin)).toBe("/contacts");
  });
});
