import type { CandidateSignals, IntroductionDetail, IntroductionItem } from "./api";

// Invented ids, people and organizations, for tests only.
export const ASK = "7a1b2c3d-0000-4000-8000-0000000000a1";
export const OTHER = "7a1b2c3d-0000-4000-8000-0000000000b2";
export const FUTURE = "2999-11-30T09:00:00Z";
export const PAST = "2001-11-30T09:00:00Z";

export function item(over: Partial<IntroductionItem> = {}): IntroductionItem {
  return {
    id: ASK,
    status: "asked",
    asked_at: "2026-11-23T08:00:00Z",
    expires_at: FUTURE,
    responded_at: null,
    employer_answer: null,
    employer_answered_at: null,
    organization_name: "Halden Demo",
    role_title: "Backend engineer",
    ...over,
  };
}

export function detail(over: Partial<IntroductionDetail> = {}): IntroductionDetail {
  return {
    view: "candidate",
    id: ASK,
    status: "asked",
    asked_at: "2026-11-23T08:00:00Z",
    expires_at: FUTURE,
    responded_at: null,
    employer_answer: null,
    employer_answer_reason: null,
    employer_answered_at: null,
    organization: { id: "org-1", name: "Halden Demo", is_synthetic: true },
    role: { id: "role-1", title: "Backend engineer", job_id: 7, country: null, city: null, work_arrangements: [], pay_range: { min: 70000, max: 95000, currency: "EUR" } },
    has_employer_signal: false,
    terms: "If you accept, Halden Demo receives what is shown below at once and must answer yes or no with a reason within 5 business days. Accepting cannot be undone.",
    share_preview: {
      on_accept: {
        name: "Demo Candidate",
        headline: "Backend engineer at Initech Demo, 2018 to 2024.",
        evidence: [
          { requirement: "Python and SQL services in production.", importance: "required", verdict: "met", record_quotes: ["Skills: Python, SQL, Quillscript."] },
          { requirement: "Experience leading a small team is a plus.", importance: "preferred", verdict: "partly_met", record_quotes: ["Led a team of three engineers."] },
        ],
        facts: [
          { kind: "role", statement: "Backend engineer at Initech Demo, 2018 to 2024." },
          { kind: "education", statement: "BSc Computer Science, Example University Demo, 2018." },
        ],
      },
      on_employer_yes: { email: "candidate@example.test" },
    },
    share_preview_hash: "hash-one",
    consent_text_version: "introduction-2026-11",
    ...over,
  };
}

export function signals(over: Partial<CandidateSignals> = {}): CandidateSignals {
  return { sent: [], received: [], quota: { used: 1, limit: 3, resets_at: "2026-11-30T00:00:00Z" }, ...over };
}
