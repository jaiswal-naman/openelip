import { describe, expect, it } from "vitest";
import { ApiFailure } from "./errors";
import { problemOf } from "./problems";

const failure = (status: number, code: string, details: Record<string, unknown> = {}) => new ApiFailure({ status, code, details });

describe("problemOf", () => {
  it("names a protected characteristic only when a field issue says so", () => {
    const protectedField = failure(422, "validation_failed", { fields: [{ field: "statement", issue: "protected_characteristic" }] });
    expect(problemOf(protectedField)).toEqual({ kind: "protected_characteristic" });
    const tooLong = failure(422, "validation_failed", { fields: [{ field: "statement", issue: "too_long" }] });
    expect(problemOf(tooLong).kind).toBe("other");
    expect(problemOf(failure(422, "validation_failed")).kind).toBe("other");
  });

  it("carries the preference the API suggests for a constraint it cannot enforce", () => {
    const suggestion = { kind: "untyped_dealbreaker", statement: "No heavy travel" };
    expect(problemOf(failure(422, "constraint_not_enforceable", { suggested_preference: suggestion }))).toEqual({
      kind: "constraint_not_enforceable",
      preferenceStatement: "No heavy travel",
    });
    expect(problemOf(failure(422, "constraint_not_enforceable"))).toEqual({ kind: "constraint_not_enforceable", preferenceStatement: null });
  });

  it("tells a missing consent from one whose providers changed", () => {
    expect(problemOf(failure(403, "consent_required", { purpose: "ai_processing", reason: "providers_changed" }))).toEqual({
      kind: "consent_required",
      reason: "providers_changed",
    });
    expect(problemOf(failure(403, "consent_required", { purpose: "ai_processing", reason: "missing" }))).toEqual({
      kind: "consent_required",
      reason: "missing",
    });
  });

  it("recognises a stale consent wording, and no other conflict", () => {
    expect(problemOf(failure(409, "conflict", { state: "consent_text_changed" }))).toEqual({ kind: "consent_text_changed" });
    expect(problemOf(failure(409, "conflict", { state: "confirmed" })).kind).toBe("other");
  });

  it("reads the limit, the tries left and the plain codes", () => {
    expect(problemOf(failure(413, "payload_too_large", { max_bytes: 10485760 }))).toEqual({ kind: "payload_too_large", maxBytes: 10485760 });
    expect(problemOf(failure(415, "unsupported_media_type")).kind).toBe("unsupported_media_type");
    expect(problemOf(failure(401, "code_invalid", { attempts_remaining: 3 }))).toEqual({ kind: "code_invalid", attemptsRemaining: 3 });
    expect(problemOf(failure(401, "code_invalid"))).toEqual({ kind: "code_invalid", attemptsRemaining: null });
    expect(problemOf(failure(503, "ai_disabled")).kind).toBe("ai_disabled");
    expect(problemOf(failure(401, "unauthenticated")).kind).toBe("unauthenticated");
  });

  it("hands anything that is not an API failure back untouched", () => {
    const stray = new Error("a bug in this page");
    expect(problemOf(stray)).toEqual({ kind: "other", error: stray });
  });
});
