import { describe, expect, it } from "vitest";

import { normalizeDealTags, parseDealTags } from "./deal-tags";

describe("deal tags", () => {
  it("parses comma and newline separated tags", () => {
    expect(parseDealTags("VIP, Повторная покупка\nHoReCa")).toEqual([
      "VIP",
      "Повторная покупка",
      "HoReCa",
    ]);
  });

  it("trims hashes and removes case-insensitive duplicates", () => {
    expect(normalizeDealTags([" #VIP ", "vip", "Новый клиент", ""])).toEqual([
      "VIP",
      "Новый клиент",
    ]);
  });

  it("limits the API payload to one hundred tags", () => {
    expect(normalizeDealTags(Array.from({ length: 105 }, (_, index) => `tag-${index}`))).toHaveLength(100);
  });

  it("limits each tag to one hundred characters", () => {
    expect(normalizeDealTags([`  #${"т".repeat(105)}  `])).toEqual(["т".repeat(100)]);
  });
});
