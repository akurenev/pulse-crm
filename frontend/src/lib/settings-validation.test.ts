import { describe, expect, it } from "vitest";

import { parseAmoUserMapping, parseSelectOptions } from "./settings-validation";

describe("parseAmoUserMapping", () => {
  it("accepts an empty mapping and normalizes valid UUID values", () => {
    expect(parseAmoUserMapping("  ")).toEqual({});
    expect(parseAmoUserMapping('{"88421":"A0EBC999-9C0B-4EF8-9E6A-684FE6772C9D"}')).toEqual({
      "88421": "a0ebc999-9c0b-4ef8-9e6a-684fe6772c9d",
    });
  });

  it.each([
    ["{", "некорректный JSON"],
    ["[]", "JSON-объектом"],
    ['{"88421":17}', "UUID пользователя Pulse CRM"],
    ['{"88421":"not-a-uuid"}', "UUID пользователя Pulse CRM"],
  ])("rejects invalid mapping %s", (value, message) => {
    expect(() => parseAmoUserMapping(value)).toThrow(message);
  });
});

describe("parseSelectOptions", () => {
  it("splits comma/newline values, trims them and removes duplicates", () => {
    expect(parseSelectOptions("Розница, Опт\nРозница\n VIP ")).toEqual(["Розница", "Опт", "VIP"]);
  });
});
