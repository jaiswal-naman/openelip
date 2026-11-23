import { messageFor } from "@/lib/labels";
import { ApiFailure } from "./errors";

/** The sentence under a failed write: the API's code in plain words. A stray error is a bug in this page, so it gets the generic one. */
export function describeFailure(error: unknown): string {
  return messageFor(error instanceof ApiFailure ? error : { code: "unknown", requestId: null, retryAfterSeconds: null });
}

/**
 * The few API failures a Career Record screen answers in its own words, as one union the component switches on
 * (API.md 1.11). Anything else is `other` and goes through `messageFor`. Nothing here carries text a person typed.
 */
export type Problem =
  | { kind: "protected_characteristic" } // 422 validation_failed with the field issue protected_characteristic (REC-07)
  | { kind: "constraint_not_enforceable"; preferenceStatement: string | null }
  | { kind: "consent_required"; reason: "missing" | "providers_changed" }
  | { kind: "consent_text_changed" } // 409 conflict: the providers changed while the sheet was open
  | { kind: "ai_disabled" }
  | { kind: "payload_too_large"; maxBytes: number | null }
  | { kind: "unsupported_media_type" }
  | { kind: "code_invalid"; attemptsRemaining: number | null }
  | { kind: "unauthenticated" }
  | { kind: "other"; error: unknown };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasProtectedField(details: Record<string, unknown>): boolean {
  const fields = details.fields;
  return Array.isArray(fields) && fields.some((field) => isRecord(field) && field.issue === "protected_characteristic");
}

export function problemOf(error: unknown): Problem {
  if (!(error instanceof ApiFailure)) return { kind: "other", error };
  const { code, details } = error;
  switch (code) {
    case "validation_failed":
      return hasProtectedField(details) ? { kind: "protected_characteristic" } : { kind: "other", error };
    case "constraint_not_enforceable": {
      const suggested = isRecord(details.suggested_preference) ? details.suggested_preference.statement : null;
      return { kind: "constraint_not_enforceable", preferenceStatement: typeof suggested === "string" ? suggested : null };
    }
    case "consent_required":
      return { kind: "consent_required", reason: details.reason === "providers_changed" ? "providers_changed" : "missing" };
    case "conflict":
      return details.state === "consent_text_changed" ? { kind: "consent_text_changed" } : { kind: "other", error };
    case "ai_disabled":
      return { kind: "ai_disabled" };
    case "payload_too_large":
      return { kind: "payload_too_large", maxBytes: typeof details.max_bytes === "number" ? details.max_bytes : null };
    case "unsupported_media_type":
      return { kind: "unsupported_media_type" };
    case "code_invalid":
      return { kind: "code_invalid", attemptsRemaining: typeof details.attempts_remaining === "number" ? details.attempts_remaining : null };
    case "unauthenticated":
      return { kind: "unauthenticated" };
    default:
      return { kind: "other", error };
  }
}
