import type { BriefData, BriefDecision, DecisionData, DecisionView, EvidenceItem, MatchData, MatchView, TodayMatches } from "./queries";

/**
 * Invented matches, briefs and decisions for tests, in the shape the API returns. "Initech Demo", the candidate and every
 * address are made up (example.test): nothing here is a real posting, company or person.
 */

export const DECISION_ID = "7a1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d";
export const OTHER_DECISION_ID = "8b2d3e4f-5061-4b7c-9d8e-0f1a2b3c4d5e";
export const DOCUMENT_ID = "9d3c6a52-0e1f-4b7d-8a2c-5f4e3d2c1b0a";

export const POSTING = [
  "Demo posting (invented): Quillscript Platform Engineer",
  "We are looking for an engineer with 3+ years of backend experience who has shipped services in Quillscript.",
  "Preferred: Kubernetes in production. You will also lead a small team.",
].join("\n");

export const RESUME = [
  "Demo Candidate (invented)",
  "Backend engineer at Initech Demo, 2019 to 2024.",
  "Built Quillscript services on PostgreSQL.",
  "Led a team of three engineers.",
].join("\n");

/** Where `quote` first appears in `text`, counted in code points as the API counts them (never by UTF-16 index). */
export function spanIn(text: string, quote: string) {
  const unit = text.indexOf(quote);
  if (unit < 0) throw new Error(`"${quote}" is not in the text`);
  const start = Array.from(text.slice(0, unit)).length;
  return { start, end: start + Array.from(quote).length };
}

const citation = (quote: string, over: Partial<EvidenceItem["record_citations"][number]> = {}) => ({
  fact_id: `fact-${quote.length}`,
  quote,
  source: { document_id: DOCUMENT_ID, span: spanIn(RESUME, quote) },
  is_current: true,
  ...over,
});

function row(id: number, quote: string, importance: "required" | "preferred", verdict: EvidenceItem["verdict"], citations: EvidenceItem["record_citations"]): EvidenceItem {
  return { requirement: { id, text: quote, importance, job_citation: { span: spanIn(POSTING, quote), quote } }, verdict, record_citations: citations };
}

/** Four requirements of the posting above: two required and met, one preferred with no evidence, one preferred partly met. */
export function evidence(): EvidenceItem[] {
  return [
    row(11, "3+ years of backend experience", "required", "met", [citation("Backend engineer at Initech Demo, 2019 to 2024.")]),
    row(12, "shipped services in Quillscript", "required", "met", [citation("Built Quillscript services on PostgreSQL.")]),
    row(13, "Kubernetes in production", "preferred", "no_evidence", []),
    row(14, "lead a small team", "preferred", "partly_met", [citation("Led a team of three engineers.")]),
  ];
}

const job = {
  id: 501,
  title: "Demo posting (invented): Quillscript Platform Engineer",
  company_name: "Initech Demo",
  pay: null,
  status: "open" as const,
  last_verified_at: "2026-11-23T03:30:00Z",
  location_raw: "Remote, United States",
  country: "US",
  city: null,
  work_arrangements: ["remote" as const],
  is_remote: true,
  employment_type: "full_time" as const,
  apply_url: "https://apply.initech-demo.invalid/1",
  source: { ats: "lever" as const, board: "initech-demo" },
  posted_at: null,
  content_version: 1,
};

export function matchView(over: Partial<MatchView> = {}): MatchView {
  return {
    decision_id: DECISION_ID,
    rank_position: 1,
    reason_codes: ["top_ranked"],
    change_note: null,
    summary: "Meets 2 of 2 required. 1 preferred item has no evidence.",
    unverified: [],
    job: { ...job },
    evidence: evidence(),
    feedback: { is_saved: false, is_dismissed: false },
    ...over,
  };
}

export function matchData(over: Partial<MatchData> = {}): MatchData {
  return {
    ...matchView(),
    description: POSTING,
    constraint_checks: [
      { kind: "location", statement: "Only jobs in the United States.", state: "satisfied" },
      { kind: "salary_floor", statement: "Never below USD 120,000 a year.", state: "not_stated" },
    ],
    ...over,
  };
}

export function todayMatches(over: Partial<TodayMatches> = {}): TodayMatches {
  return {
    local_date: "2026-11-23",
    timezone: "Etc/GMT-9",
    run: { id: "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9", record_version: 1, shown: 1, withheld: 2, filtered: 3, checker: "passed" },
    matches: [matchView()],
    ...over,
  };
}

export function briefDecision(over: Partial<BriefDecision> = {}): BriefDecision {
  return {
    kind: "match",
    title: "Demo posting (invented): Quillscript Platform Engineer at Initech Demo",
    why: "Meets 2 of 2 required. 1 preferred item has no evidence.",
    ref: { decision_id: DECISION_ID },
    actions: [
      { action: "open", method: "GET", path: `/v1/matches/${DECISION_ID}` },
      { action: "save", method: "POST", path: `/v1/matches/${DECISION_ID}/feedback`, body: { kind: "save" } },
      { action: "dismiss", method: "POST", path: `/v1/matches/${DECISION_ID}/feedback`, body: { kind: "dismiss" } },
    ],
    is_assessed: true,
    change_note: null,
    note: null,
    ...over,
  };
}

export function brief(over: Partial<BriefData> = {}): BriefData {
  return {
    id: "3b4c5d6e-7f80-4a9b-8c1d-2e3f4a5b6c7d",
    local_date: "2026-11-23",
    status: "composed",
    delivery: { in_app: "composed", email: "sent", email_sent_at: "2026-11-23T01:02:00Z" },
    decisions: [briefDecision()],
    done_since_last_brief: [
      { at: "2026-11-23T01:00:00Z", text: "Checked the index on request: 1 shown, 2 held back, 3 filtered by your constraints", ref: { match_run_id: "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9" } },
    ],
    open_record_proposals: 0,
    ...over,
  };
}

export function decisionView(over: Partial<DecisionView> = {}): DecisionView {
  return {
    decision_id: OTHER_DECISION_ID,
    local_date: "2026-11-23",
    outcome: "filtered",
    reason_codes: ["hard_constraint"],
    constraint: { id: "c-1", kind: "location", statement: "Only jobs in the United States." },
    gaps: [],
    job: { id: 502, title: "Demo posting (invented): Onsite Support Engineer", company_name: "Initech Demo", pay: null, status: "open", last_verified_at: "2026-11-23T03:30:00Z" },
    ...over,
  };
}

export function decisionData(over: Partial<DecisionData> = {}): DecisionData {
  return {
    ...decisionView(),
    violated_kinds: ["location", "remote_policy"],
    unverified: [],
    rank_position: null,
    summary: null,
    change_note: null,
    evidence: [],
    ...over,
  };
}

/** GET /v1/me/documents/{id}?include=text for the resume above. */
export function resumeDocument(over: Record<string, unknown> = {}) {
  let start = 0;
  const lines = RESUME.split("\n").map((text, index) => {
    const line = { n: index + 1, start, end: start + Array.from(text).length, text };
    start = line.end + 1;
    return line;
  });
  return {
    id: DOCUMENT_ID,
    status: "succeeded",
    original_filename: "demo-resume.docx",
    mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    byte_size: 4096,
    parse_error_code: null,
    message: null,
    facts: { kept: 3, unverified: 0, to_review: 0 },
    hidden_text_chars: 0,
    hidden_text_flags: { reasons: [], samples: [] },
    created_at: "2026-11-23T01:00:00Z",
    parsed_at: "2026-11-23T01:00:05Z",
    text: RESUME,
    lines,
    ...over,
  };
}
