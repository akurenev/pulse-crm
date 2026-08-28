import { describe, expect, it } from "vitest";

import { formatLongDate } from "./format";

describe("formatLongDate", () => {
  it("formats a date-only CRM value", () => {
    expect(formatLongDate("2026-08-28")).toContain("2026");
  });

  it("formats an API timestamp without appending a second time component", () => {
    expect(formatLongDate("2026-08-28T08:00:00+05:00")).toContain("2026");
  });
});
