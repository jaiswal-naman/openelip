import type { ApiFailure } from "./api/errors";
import type { components } from "./api/schema";

/**
 * One plain message per API error code a Phase 1 screen can meet (API.md 1.11, FRONTEND_UI 6.4), plus two codes the
 * client makes up for a call that never got an answer. `labels.test.ts` fails when a code in its list has no message.
 */
export const ERROR_MESSAGES = {
  invalid_request: "The request was not valid. Check what you entered and try again.",
  validation_failed: "Some of what you entered is not valid. Check it and try again.",
  idempotency_key_missing: "This page sent a request the server could not accept. Reload the page and try again.",
  idempotency_key_reused: "This page sent a request the server could not accept. Reload the page and try again.",
  request_in_progress: "That request is still running. Give it a moment.",
  unauthenticated: "Sign in to continue.",
  code_invalid: "That code does not match or has expired. Check it, or send a new one.",
  forbidden: "You do not have access to this, or it does not exist.",
  not_found: "You do not have access to this, or it does not exist.",
  conflict: "This changed in another tab or by an agent. Showing the latest.",
  unsupported_media_type: "The server could not accept that kind of request. Reload the page and try again.",
  rate_limited: "Too many requests. Try again in a few minutes.",
  internal_error: "Something went wrong on the server. Nothing was sent. Try again.",
  ai_disabled: "AI features are not set up on this server. Search and everything already saved still work.",
  dependency_unavailable: "A service this needs is unavailable. Try again shortly.",
  network_error: "Could not reach the server. Nothing was changed. Check your connection and try again.",
  unreadable_response: "The server's answer could not be read. Nothing was changed. Try again.",
} as const;

export type KnownErrorCode = keyof typeof ERROR_MESSAGES;

/** "45 seconds", "14 minutes", "2 hours": rounded up, because telling someone to try too early wastes their time. */
export function waitPhrase(seconds: number): string {
  const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "s"}`;
  if (seconds < 60) return plural(Math.max(1, Math.ceil(seconds)), "second");
  const minutes = Math.ceil(seconds / 60);
  return minutes < 60 ? plural(minutes, "minute") : plural(Math.ceil(minutes / 60), "hour");
}

/** The sentence a person reads for a failed call. Unknown codes get the generic one, with the request id for the operator. */
export function messageFor(failure: Pick<ApiFailure, "code" | "requestId" | "retryAfterSeconds">): string {
  if (failure.code === "rate_limited" && failure.retryAfterSeconds !== null) {
    return `Too many attempts. Try again in ${waitPhrase(failure.retryAfterSeconds)}.`;
  }
  const known = (ERROR_MESSAGES as Record<string, string>)[failure.code];
  if (known !== undefined && failure.code !== "internal_error") return known;
  const id = failure.requestId ? `, and if it repeats, the request id is ${failure.requestId}` : "";
  return `Something went wrong on the server. Nothing was sent. Try again${id}.`;
}

// ---- The job screens (IDX-05, IDX-06, IDX-08) ---------------------------------------------------------------------------
// Typed from the generated API types, so a code the API adds without a label here fails the type check, and
// jobLabels.test.ts compares the keys with web/openapi.json (FRONTEND_UI 6.2).

type Schemas = components["schemas"];

/** FRONTEND_UI 6.3: what to say when the posting does not say, and when the system could not tell. Never a dash or a guess. */
export const NOT_STATED = "Not stated in the posting";
export const LOCATION_UNKNOWN = "Location: unknown";
export const REMOTE_NOT_STATED = "Remote: not stated";

/** Where a posting came from. `direct` is an employer's own role, which the public API never lists. */
export const ATS_LABELS: Record<Schemas["JobSource"]["ats"], string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  direct: "Direct",
};

export const WORK_ARRANGEMENT_LABELS: Record<Schemas["Job"]["work_arrangements"][number], string> = {
  onsite: "On-site",
  hybrid: "Hybrid",
  remote: "Remote",
};

export const EMPLOYMENT_TYPE_LABELS: Record<Schemas["Job"]["employment_type"], string> = {
  full_time: "Full time",
  part_time: "Part time",
  contract: "Contract",
  internship: "Internship",
  temporary: "Temporary",
  unknown: NOT_STATED,
};

export const IMPORTANCE_LABELS: Record<Schemas["Requirement"]["importance"], string> = {
  required: "Required",
  preferred: "Preferred",
};
