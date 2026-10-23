import { describe, expect, it } from "vitest";

import { contactPointValues, readContactPoints } from "./contact-points";

describe("contactPointValues", () => {
  it("puts the primary value first and preserves distinct stored alternatives", () => {
    expect(contactPointValues(" primary@example.test ", ["other@example.test", "primary@example.test", " other@example.test ", "", "—"]))
      .toEqual(["primary@example.test", "other@example.test"]);
  });

  it("supports missing values and the display placeholder", () => {
    expect(contactPointValues()).toEqual([]);
    expect(contactPointValues("—", [" ", "+7 000 000-00-01"])).toEqual(["+7 000 000-00-01"]);
  });

  it("does not silently truncate stored values above the current entry limit", () => {
    const values = Array.from({ length: 21 }, (_, index) => `test${index}@example.test`);
    expect(contactPointValues(values[0], values)).toEqual(values);
  });
});

describe("readContactPoints", () => {
  it("trims email rows, ignores blanks and files, and deduplicates email regardless of case", () => {
    const formData = new FormData();
    for (const value of [" ", " First@example.test ", "first@EXAMPLE.test", "second@example.test"]) formData.append("email", value);
    formData.append("email", new File(["test"], "test.txt"));
    formData.append("phone", "+7 000 000-00-01");

    expect(readContactPoints(formData, "email")).toEqual(["First@example.test", "second@example.test"]);
  });

  it("keeps phone order and returns no values when every row is empty", () => {
    const formData = new FormData();
    for (const value of [" +7 000 000-00-01 ", "", "+7 000 000-00-01", "+7 000 000-00-02"]) formData.append("phone", value);

    expect(readContactPoints(formData, "phone")).toEqual(["+7 000 000-00-01", "+7 000 000-00-02"]);
    expect(readContactPoints(formData, "email")).toEqual([]);
  });
});
