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
  // Campaigns and the Guardian (FRONTEND_UI 6.4)
  budget_exhausted: "This campaign reached its budget. Raise the budget to continue, or cancel.",
  job_closed: "This job has closed, so a campaign cannot start.",
  violates_constraint: "This job breaks one of your hard constraints, so a campaign cannot start.",
  autonomy_not_allowed: "That level is not allowed for this kind of action.",
  version_conflict: "These settings changed in another tab or by an agent. Showing the latest.",
  consent_required: "Campaigns need your agreement to AI processing. Review it in Settings, then try again.",
} as const;

export type KnownErrorCode = keyof typeof ERROR_MESSAGES;

/** What a 409 conflict says when it names the state the thing is really in (campaigns, approvals, undo). */
const CONFLICT_STATE_MESSAGES: Record<string, string> = {
  closed: "This campaign is closed.",
  sending: "Sending has started, so this can no longer be undone.",
  sent: "This was already sent, so it can no longer be undone.",
  executed: "This was already sent, so it can no longer be undone.",
  cancelled: "This was already undone.",
  several_accounts:
    "This database holds more than one account, so it was used in server mode. Start local mode on a new database, or set APP_MODE=server in .env.",
  set_in_environment: "The model is set in your .env file (LLM_PROVIDER or an API key). Remove it there to save a key here.",
};

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
  if (failure.code === "conflict" && typeof failure.details?.campaign_id === "string") return "You already have a campaign for this job.";
  if (failure.code === "conflict" && typeof failure.details?.state === "string") {
    const said = CONFLICT_STATE_MESSAGES[failure.details.state];
    if (said !== undefined) return said;
  }
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

/** FRONTEND_UI 3.3, 4.6: the candidate's side of an introduction (Today card, accepted list, settings). */
export const CANDIDATE_INTRODUCTION_LABELS = {
  kind: "Introduction",
  review: "Review and accept",
  demo: "Demo (invented)",
  waiting: "Waiting for their answer",
  employerSignal: "Serious interest from the employer",
  youSentSignal: "You sent serious interest",
  acceptedHeading: "Introductions you accepted",
  openToIntroductions: "Open to introductions from employers",
} as const;

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

// ---- Chat, tailoring and applications (CHT-01 to CHT-04, TLR-01 to TLR-03, APP-01, APP-02) -------------------------------

/** Why a proposed edit was discarded before the person saw it (TLR-02). The API sends counts under these codes only. */
export const DISCARD_REASON_LABELS: Record<string, string> = {
  no_supporting_fact: "Not backed by a fact in your record",
  changed_employer: "Changed an employer",
  changed_title: "Changed a job title",
  changed_date: "Changed a date",
  changed_number: "Changed or added a number",
  new_entity: "Added a skill, tool or name your record does not hold",
  not_entailed: "A second check did not find your facts support it",
};

export const APPLICATION_STATUS_LABELS: Record<Schemas["ApplicationView"]["status"], string> = {
  applied: "Applied",
  interview: "Interview",
  offer: "Offer",
  rejected: "Rejected",
  no_response: "No response",
};

/** How a reply ended when it did not simply finish (API.md 4.2 `message.end`). */
export const FINISH_REASON_LABELS: Record<Schemas["MessageView"]["finish_reason"] & string, string> = {
  complete: "",
  length: "The reply stopped early because it reached a limit. Ask again to continue.",
  stopped: "You stopped this reply.",
  interrupted: "This reply was interrupted and is incomplete. Ask again.",
};

/** The packet's fixed questions. Four are always left for the candidate to answer. */
export const PACKET_QUESTION_LABELS: Record<string, string> = {
  why_role: "Why are you interested in this role?",
  relevant_experience: "What experience of yours is most relevant to this role?",
  work_authorization: "Are you authorised to work in the country of this job?",
  expected_pay: "What pay do you expect?",
  notice_period: "What is your notice period?",
  start_date: "When could you start?",
};

// ---- Campaigns and the Guardian (CMP-01 to CMP-07, GRD-01 to GRD-04, GRD-07) -----------------------------------------------
// FRONTEND_UI 3.6, 3.7, 3.19 and 6.2 word these.

export type CampaignStage = Schemas["Column"]["stage"];

export const CAMPAIGN_STAGE_LABELS: Record<CampaignStage, string> = {
  researching: "Researching",
  needs_you: "Needs you",
  applied: "Applied",
  talking: "Talking",
  interview: "Interview",
  offer: "Offer",
  closed: "Closed",
};

export const CLOSED_REASON_LABELS: Record<string, string> = {
  cancelled: "Cancelled by you",
  rejected: "Rejected",
  no_response: "No response",
  job_closed: "The job closed",
  offer_accepted: "Offer accepted",
  offer_declined: "Offer declined",
};

export const PAUSE_LABELS: Record<NonNullable<Schemas["Card"]["pause_reason"]>, string> = {
  candidate: "You paused",
  budget_exhausted: "Budget reached",
  scam_suspected: "Possible scam",
};

export const STEP_STATUS_LABELS: Record<string, string> = {
  pending: "Not started",
  running: "Working",
  waiting: "Waiting",
  done: "Done",
  failed: "Could not finish",
  skipped: "Skipped",
  cancelled: "Cancelled",
};

export const AGENT_ROLE_LABELS: Record<string, string> = {
  scout: "Scout",
  researcher: "Researcher",
  analyst: "Analyst",
  writer: "Writer",
  operator: "Operator",
  chaser: "Chaser",
  coach: "Coach",
  twin: "Twin",
  guardian: "Guardian",
};

export const CHECK_KIND_LABELS: Record<string, string> = {
  citations: "Every claim has a source",
  consent: "Your consent",
  scope: "Only what you allowed",
  budget: "Budget",
  injection: "No instructions hidden in text",
  rate_limit: "Follow-up limit",
};

export const POLICY_DECISION_LABELS: Record<string, string> = {
  allow: "Allowed without asking",
  ask: "Ask you first",
  deny: "Not allowed",
};

export const ACTION_STATUS_LABELS: Record<string, string> = {
  recorded: "Recorded",
  proposed: "Proposed",
  awaiting_approval: "Waiting for you",
  scheduled: "Scheduled",
  executed: "Done",
  blocked: "Blocked",
  rejected: "You declined",
  expired: "Expired",
  cancelled: "Cancelled",
  failed: "Failed",
};

export const ACTION_TYPE_LABELS: Record<string, string> = {
  submit_application: "Submit an application",
  send_follow_up: "Send a follow-up",
  twin_answer: "Answer an employer's question",
};

/** Where an approved follow-up is in the outbox (`delivery`). */
export const DELIVERY_LABELS: Record<string, string> = {
  pending: "Scheduled",
  sending: "Sending",
  sent: "Sent",
  failed: "Failed",
  cancelled: "Undone",
  refused: "Blocked",
  unknown: "May have sent",
};

/** The Guardian's reason codes in words (the same sentences as backend guardian/explain.py; a code with no entry gets UNKNOWN_REASON). */
export const REASON_LABELS: Record<string, string> = {
  dial_never: "You set this kind of action to never, so it was not attempted.",
  campaign_paused: "This campaign is paused, so nothing was prepared or sent.",
  campaign_closed: "This campaign is closed, so nothing was prepared or sent.",
  account_inactive: "Your account is not active, so nothing was prepared or sent.",
  type_not_available: "This kind of action is not available yet.",
  citation_unresolved: "The draft cites something that is not in your confirmed record or in the company's sources, or says something it does not cite.",
  unsupported_claim: "The draft says more than its sources support.",
  citation_check_missing: "The check that compares a draft with its sources is not available, so it was held.",
  consent_missing: "You have not allowed AI processing, so a draft written with it cannot be used.",
  constraint_conflict: "The job now breaks one of your hard constraints, so nothing was sent for it.",
  employer_blocked: "You blocked this employer, so nothing was sent to them.",
  restricted_data: "The draft holds something only you may state, so it was held.",
  budget_exhausted: "This campaign reached its model budget, so work stopped until you raise it or cancel.",
  injection_suspected: "Text the agent read carried an instruction or an address that does not come from your record or the employer's own site, so the draft was held.",
  limit_reached: "Two follow-ups were already sent for this campaign, which is the most allowed.",
  candidate: "You paused this campaign.",
  scam_suspected: "The posting looks like a scam, so work stopped before anything was shared.",
  expired: "You did not answer in time, so the request expired.",
  undone: "You undid this before it was sent.",
  cancelled: "The campaign was cancelled before this was sent.",
  action_not_scheduled: "This was no longer scheduled when its time came, so it was not sent.",
  preview_changed: "The draft changed after you saw it; look at the new version before deciding.",
};
export const UNKNOWN_REASON = "This did not go ahead.";
export const reasonText = (code: string | null): string => (code === null ? "" : (REASON_LABELS[code] ?? UNKNOWN_REASON));

export const SCAM_SIGNAL_LABELS: Record<string, string> = {
  fee_request: "It asks you to pay a fee or buy something.",
  documents_before_interview: "It asks for identity or bank documents before an interview.",
  off_platform: "It asks you to move to a chat app or a personal mailbox.",
};
export const scamSignalText = (kind: string): string => SCAM_SIGNAL_LABELS[kind] ?? "The posting matched a rule for scam postings.";

/** The three kinds of action the dial covers (GRD-01), and why a row can be locked. */
export const AUTONOMY_ROW_LABELS: Record<string, { name: string; locked: string }> = {
  submit_application: { name: "Submit an application", locked: "The system never submits an application for you. You approve the packet and you apply." },
  send_follow_up: { name: "Send a follow-up", locked: "A follow-up is never sent without your approval, so Tell me after is not offered." },
  twin_answer: { name: "Answer an employer's question", locked: "Answers on your behalf arrive in a later version. Your choice is saved for then." },
};
export const AUTONOMY_LEVEL_LABELS: Record<string, string> = {
  ask_first: "Ask me first",
  tell_me_after: "Tell me after",
  never: "Never",
};
