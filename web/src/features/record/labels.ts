import { describeFailure, problemOf } from "@/lib/api/problems";

/** REC-07: shown when the API refuses a free-text field because it states a protected characteristic. Nothing was stored. */
export const PROTECTED_NOTICE = "We do not keep this in your record.";

/** REC-04: a dealbreaker no job field can check lowers a job's rank and guarantees nothing. */
export const NOT_GUARANTEED_NOTICE = "We cannot check this in job data, so it is a preference, not a guarantee.";

/** The sentence under a failed write: the protected-characteristic refusal in its own words, anything else by its code. */
export function writeProblemText(error: unknown): string {
  return problemOf(error).kind === "protected_characteristic" ? PROTECTED_NOTICE : describeFailure(error);
}

// One label per code, as FRONTEND_UI 6.2 words them. A code this page does not know is shown as it came, never guessed.
const FACT_KINDS: Record<string, string> = {
  role: "Role",
  achievement: "Achievement",
  education: "Education",
  skill: "Skill",
  certification: "Certification",
  project: "Project",
  other: "Other",
};
const SOURCES: Record<string, string> = {
  resume: "From your resume",
  candidate_edit: "Edited by you",
  confirmed_proposal: "Confirmed from chat",
};
const DOCUMENT_STATUSES: Record<string, string> = { queued: "Queued", running: "Running", succeeded: "Done", failed: "Failed" };
const PARSE_PROBLEMS: Record<string, string> = {
  corrupt: "The file could not be read.",
  encrypted: "The file is password protected.",
  scanned: "The file is a scanned image, with no text to read.",
  consent_required: "Not read: AI processing was not agreed.",
  ai_disabled: "Not read: AI features are off on this installation.",
  provider_unavailable: "The AI provider did not answer.",
  parse_failed: "Something went wrong while reading the file.",
  partial_extraction: "Part of the resume could not be read.",
};
const HIDDEN_REASONS: Record<string, string> = {
  tiny_font: "text too small to read",
  off_page: "text outside the page",
  overlapped: "text covered by other text",
  text_colour_matches_background: "text the same colour as the page",
  docx_hidden_run: "text marked as hidden",
};
const CONSTRAINT_KINDS: Record<string, string> = {
  location: "Location",
  remote_policy: "Remote policy",
  work_authorization: "Work authorization",
  salary_floor: "Salary floor",
  employment_type: "Employment type",
  seniority: "Seniority",
  excluded_company: "Excluded companies",
};
const ENTITIES: Record<string, string> = { fact: "Fact", constraint: "Constraint", preference: "Preference" };

const labelIn = (labels: Record<string, string>) => (code: string) => labels[code] ?? code;

export const factKindLabel = labelIn(FACT_KINDS);
export const sourceLabel = labelIn(SOURCES);
export const documentStatusLabel = labelIn(DOCUMENT_STATUSES);
export const parseProblemLabel = labelIn(PARSE_PROBLEMS);
export const hiddenReasonLabel = labelIn(HIDDEN_REASONS);
export const constraintKindLabel = labelIn(CONSTRAINT_KINDS);
export const entityLabel = labelIn(ENTITIES);

const OPERATIONS: Record<string, string> = { create: "Added", update: "Changed", remove: "Removed", confirm: "Confirmed", reject: "Rejected" };
const FACT_STATUSES: Record<string, string> = { unconfirmed: "To review", confirmed: "Confirmed", rejected: "Rejected", superseded: "Replaced" };
const ORIGINS: Record<string, string> = {
  onboarding: "Proposed in onboarding",
  chat: "Proposed in chat",
  feedback: "Proposed from your feedback",
  agent: "Proposed by your agent",
};

export const operationLabel = labelIn(OPERATIONS);
export const factStatusLabel = labelIn(FACT_STATUSES);
export const originLabel = labelIn(ORIGINS);

/** "4.0 KB", "1.2 MB": a file size a person can read. */
export function sizeLabel(bytes: number, locale: string): string {
  const number = (value: number) => new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(value);
  if (bytes < 1024) return `${bytes} bytes`;
  return bytes < 1024 * 1024 ? `${number(bytes / 1024)} KB` : `${number(bytes / (1024 * 1024))} MB`;
}

/** The kind of a constraint as it reads in a sentence: "salary floor", "excluded company" (the card's heading is the plural). */
export const constraintSubject = (kind: string) => (kind === "excluded_company" ? "excluded company" : constraintKindLabel(kind).toLowerCase());
