import { describe, expect, it } from "vitest";

import { demoMailAccount, demoMailMessages } from "./demo";
import { composeFromMessage } from "./helpers";

describe("mail replies", () => {
  it("honors Reply-To while excluding self and hidden recipients", () => {
    const result = composeFromMessage({ ...demoMailMessages[0], reply_to_addresses: ["reply@example.com"], cc_addresses: [demoMailAccount.email, "REPLY@example.com", "colleague@example.com"], bcc_addresses: ["hidden@example.com"] }, demoMailAccount, "reply-all");
    expect(result.to_addresses).toEqual(["reply@example.com"]);
    expect(result.cc_addresses).toEqual(["colleague@example.com"]);
    expect(result.bcc_addresses).toEqual([]);
  });

  it("keeps long replies within editor limits and explicitly marks shortened quotations", () => {
    const original = { ...demoMailMessages[0], subject: "Т".repeat(998), body_text: "А".repeat(100_000) };
    const result = composeFromMessage(original, demoMailAccount, "reply");
    expect(result.subject).toHaveLength(998);
    expect(result.body_text).toHaveLength(100_000);
    expect(result.body_text).toContain("Цитата сокращена");
  });
});
