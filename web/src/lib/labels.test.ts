import { describe, expect, it } from "vitest";
import { ERROR_MESSAGES, messageFor, waitPhrase } from "./labels";

// Every code API.md 1.11 lets a Phase 1 request return, plus the two the client invents for a call with no answer.
// Kept as a literal here on purpose: if a code is added to a screen's reach and not to labels.ts, this list is the check.
const CODES_A_PHASE_1_SCREEN_CAN_MEET = [
  "invalid_request",
  "validation_failed",
  "idempotency_key_missing",
  "idempotency_key_reused",
  "request_in_progress",
  "unauthenticated",
  "code_invalid",
  "forbidden",
  "not_found",
  "conflict",
  "unsupported_media_type",
  "rate_limited",
  "internal_error",
  "ai_disabled",
  "dependency_unavailable",
  "network_error",
  "unreadable_response",
];

const failure = (code: string, extra: { requestId?: string | null; retryAfterSeconds?: number | null } = {}) => ({
  code,
  requestId: extra.requestId ?? null,
  retryAfterSeconds: extra.retryAfterSeconds ?? null,
});

describe("error messages", () => {
  it.each(CODES_A_PHASE_1_SCREEN_CAN_MEET)("has a plain message for %s", (code) => {
    expect(Object.keys(ERROR_MESSAGES)).toContain(code);
    expect(messageFor(failure(code)).length).toBeGreaterThan(10);
  });

  it("uses the wording from FRONTEND_UI 6.4 for a wrong code", () => {
    expect(messageFor(failure("code_invalid"))).toMatch(/^That code does not match or has expired\./);
  });

  it("says when to try again if the server said how long to wait", () => {
    expect(messageFor(failure("rate_limited", { retryAfterSeconds: 840 }))).toBe("Too many attempts. Try again in 14 minutes.");
    expect(messageFor(failure("rate_limited"))).toBe("Too many requests. Try again in a few minutes.");
  });

  it("gives an unknown code the generic message and the request id, never the code itself", () => {
    const message = messageFor(failure("something_new", { requestId: "req_8f2c" }));
    expect(message).toContain("req_8f2c");
    expect(message).not.toContain("something_new");
  });

  it("adds the request id to a server error when there is one", () => {
    expect(messageFor(failure("internal_error", { requestId: "req_1" }))).toContain("request id is req_1");
    expect(messageFor(failure("internal_error"))).not.toContain("request id");
  });
});

describe("waitPhrase", () => {
  it("rounds up and keeps the unit plural only when it should be", () => {
    expect(waitPhrase(1)).toBe("1 second");
    expect(waitPhrase(45)).toBe("45 seconds");
    expect(waitPhrase(61)).toBe("2 minutes");
    expect(waitPhrase(3540)).toBe("59 minutes");
    expect(waitPhrase(3600)).toBe("1 hour");
    expect(waitPhrase(3601)).toBe("2 hours");
  });
});
