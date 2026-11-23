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
  // The hiring desk (FRONTEND_UI 6.4)
  answers_overdue: "New introductions are paused until overdue answers are given.",
  preview_changed: "What would be shared changed. Review it again.",
  pay_range_required: "Publishing needs a pay range.",
  requirements_unconfirmed: "Publishing needs every requirement confirmed.",
  quota_exhausted: "No serious-interest signals left this week. They reset on Monday (UTC).",
  already_resolved: "This changed in another tab or by an agent. Showing the latest.",
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
export function messageFor(failure: Pick<ApiFailure, "code" | "requestId" | "retryAfterSeconds"> & { details?: Record<string, unknown> }): string {
  if (failure.code === "rate_limited" && failure.retryAfterSeconds !== null) {
    return `Too many attempts. Try again in ${waitPhrase(failure.retryAfterSeconds)}.`;
  }
  if (failure.code === "conflict" && failure.details?.state === "answers_overdue") return ERROR_MESSAGES.answers_overdue;
  if (failure.code === "quota_exhausted" && typeof failure.details?.resets_at === "string") {
    const reset = new Date(failure.details.resets_at);
    if (!Number.isNaN(reset.getTime())) {
      const date = reset.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
      return `No serious-interest signals left this week. They reset on ${date} (UTC).`;
    }
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

// ---- Matches and the morning brief (MAT-02 to MAT-08, BRF-01 to BRF-03) ---------------------------------------------------
// FRONTEND_UI 6.2 and 6.3 word these. Each map is typed from the generated schema, so a code the API adds without a label
// fails the type check, and matchLabels.test.ts compares the keys with web/openapi.json.

export const VERDICT_LABELS: Record<Schemas["EvidenceItem"]["verdict"], string> = {
  met: "Met",
  partly_met: "Partly met",
  not_met: "Not met",
  no_evidence: "No evidence",
};

export const OUTCOME_LABELS: Record<Schemas["DecisionView"]["outcome"], string> = {
  shown: "Shown",
  introduced: "Introduced",
  withheld: "Withheld",
  filtered: "Filtered",
  not_evaluated: "Not checked yet",
};

export const MATCH_REASON_LABELS: Record<Schemas["MatchView"]["reason_codes"][number], string> = {
  top_ranked: "Top ranked",
  material_change: "Changed since you saw it",
  selected_for_role: "Selected for a role",
  weak_fit: "Weak fit",
  required_not_met: "A required item is not met",
  below_daily_cap: "Below today's five",
  citation_unresolved: "Withheld: a citation did not resolve",
  already_shown: "Already shown",
  job_closed: "Job closed",
  liveness_unverified: "Could not confirm it is still open",
  not_scored: "Not scored",
  not_retrieved: "Not found by search",
  hard_constraint: "Filtered by your constraints",
  injection_suspected: "Withheld: the posting tried to instruct the AI",
};

export const CONSTRAINT_KIND_LABELS: Record<Schemas["ConstraintCheck"]["kind"], string> = {
  location: "Location",
  remote_policy: "Remote policy",
  work_authorization: "Work authorization",
  salary_floor: "Salary floor",
  employment_type: "Employment type",
  seniority: "Seniority",
  excluded_company: "Excluded companies",
};

/** "Satisfied", or "Not stated (unverified)": an unknown value passes a constraint and is shown that way (FRONTEND_UI 3.4). */
export const CONSTRAINT_STATE_LABELS: Record<Schemas["ConstraintCheck"]["state"], string> = {
  satisfied: "Satisfied",
  not_stated: "Not stated (unverified)",
};

/** What the posting leaves unsaid about a field a hard constraint covers (the match's `unverified` flags). */
export const UNVERIFIED_LABELS: Record<Schemas["MatchView"]["unverified"][number], string> = {
  location_not_stated: "Location: not stated (unverified)",
  arrangement_not_stated: "Work arrangement: not stated (unverified)",
  arrangement_ambiguous: "Work arrangement: unclear in the posting (unverified)",
  salary_not_stated: "Pay: not stated (unverified)",
  salary_currency_differs: "Pay: in a different currency from your floor (unverified)",
  employment_type_not_stated: "Employment type: not stated (unverified)",
  seniority_not_stated: "Level: not stated (unverified)",
};

/** The reasons a person can give for dismissing a match (MAT-08). Some of them propose a change to the record. */
export const DISMISS_REASON_LABELS: Record<NonNullable<Schemas["FeedbackBody"]["reason"]>, string> = {
  not_interested_role: "Not my field",
  not_interested_company: "Not this company",
  seniority_mismatch: "Wrong level",
  compensation: "Pay is too low",
  location: "Wrong location",
  skills_mismatch: "The skills do not fit",
  already_applied: "I already applied",
  explanation_wrong: "The explanation is wrong",
  duplicate: "A duplicate of another job",
  job_closed: "The job has closed",
  other: "Other",
};

export const BRIEF_FREQUENCY_LABELS: Record<Schemas["BriefSettings"]["frequency"], string> = {
  daily: "Daily",
  weekly: "Weekly",
  paused: "Paused",
};

// ---- The hiring desk (HIR-01 to HIR-08) -----------------------------------------------------------------------------------
// FRONTEND_UI 3.12 to 3.17, 4.7 and 6.4 word these. Typed from the generated schema where the API has an enum.

export const ROLE_STATUS_LABELS: Record<Schemas["RoleView"]["status"], string> = {
  draft: "Draft",
  open: "Open",
  closed: "Closed",
};

export const ORG_ROLE_LABELS: Record<Schemas["Member"]["role"], string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
};

export const MEMBER_STATUS_LABELS: Record<Schemas["Member"]["status"], string> = {
  active: "Active",
  invited: "Invited (pending)",
  removed: "Removed",
};

/** The introduction states an employer can meet. The shortlist holds only accepted ones. */
export const INTRODUCTION_STATUS_LABELS = {
  asked: "Asked",
  accepted: "Accepted",
  declined: "Declined",
  expired: "Expired",
  withdrawn: "Withdrawn",
} as const;

export const EMPLOYER_ANSWER_LABELS = { yes: "Answered yes", no: "Answered no" } as const;

/** FRONTEND_UI 3.14: the preset reasons, in the order shown. A preset fills the reason box, which stays editable. */
export const ANSWER_PRESETS = [
  { label: "Strong fit", text: "Strong fit for the role." },
  { label: "Skills gap", text: "The skills the role needs are not shown clearly enough." },
  { label: "Experience gap", text: "The role needs more experience than the record shows." },
  { label: "Location mismatch", text: "The location or working arrangement does not match the role." },
  { label: "Pay mismatch", text: "The pay range does not match what was shared." },
  { label: "Role filled", text: "The role has been filled." },
  { label: "Role on hold", text: "The role is on hold." },
  { label: "Other", text: "Other: " },
] as const;


export const PUBLISH_BLOCKER_LABELS: Record<Schemas["RoleView"]["blockers"][number], string> = {
  requirements_unconfirmed: "confirmed requirements",
  pay_range_required: "pay range",
};
