import type { AnswerClockData, Invitation, Membership, Preview, RoleView, ShortlistItem } from "./api";

// Invented ids and people, for tests only.
export const ORG = "0a1b2c3d-0000-4000-8000-000000000001";
export const ROLE = "0a1b2c3d-0000-4000-8000-000000000002";
export const INTRO_A = "0a1b2c3d-0000-4000-8000-0000000000a1";
export const INTRO_B = "0a1b2c3d-0000-4000-8000-0000000000b2";

export const membership: Membership = { id: ORG, name: "Halden Demo", role: "owner", is_synthetic: true };
export const invitation: Invitation = {
  organization_id: ORG,
  organization_name: "Halden Demo",
  role: "member",
  invited_at: "2026-11-20T09:00:00Z",
};

export function me(extra: Record<string, unknown> = {}) {
  return { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "owner@example.test", timezone: "UTC", created_at: "2026-11-01T00:00:00Z", organizations: [membership], invitations: [], ...extra };
}

export const DESCRIPTION = "We need a backend engineer. You have run Python in production for five years. Kubernetes is a plus.";

export const QUOTE = "run Python in production for five years";
export const QUOTE_START = DESCRIPTION.indexOf(QUOTE);
export const QUOTE_SPAN = { start: QUOTE_START, end: QUOTE_START + Array.from(QUOTE).length };

export function role(extra: Partial<RoleView> = {}): RoleView {
  return {
    id: ROLE,
    organization_id: ORG,
    title: "Senior Backend Engineer",
    description: DESCRIPTION,
    status: "draft",
    country: null,
    city: null,
    work_arrangements: [],
    employment_type: "full_time",
    seniority: "senior",
    visa_sponsorship: "unknown",
    pay_range: null,
    job_id: null,
    requirements: [
      { id: "r1", kind: "experience", text: "Python in production", importance: "required", span: QUOTE_SPAN, quote: QUOTE, is_confirmed: false },
      { id: "r2", kind: "skill", text: "Kubernetes", importance: "preferred", span: null, quote: null, is_confirmed: true },
    ],
    can_publish: false,
    blockers: ["pay_range_required", "requirements_unconfirmed"],
    created_at: "2026-11-20T09:00:00Z",
    opened_at: null,
    closed_at: null,
    ...extra,
  };
}

export const preview: Preview = {
  role_id: ROLE,
  as_of: "2026-11-23T08:00:00Z",
  eligible_candidates: 14,
  selected_to_ask: "fewer than 5",
  waiting_for_reply: "fewer than 5",
  said_yes: 2,
  said_no_or_expired: 0,
  suppressed_below: 5,
  note: "Counts only.",
};

export function shortlistItem(extra: Partial<ShortlistItem> = {}): ShortlistItem {
  return {
    introduction_id: INTRO_A,
    status: "accepted",
    responded_at: "2026-11-20T10:00:00Z",
    answer_due_at: "2026-11-27T10:00:00Z",
    is_overdue: false,
    employer_answer: null,
    employer_answer_reason: null,
    employer_answered_at: null,
    shared: {
      name: "Demo Candidate A",
      headline: "Backend engineer, payments",
      evidence: [
        { requirement: "Python in production", importance: "required", verdict: "met", record_quotes: ["Built and ran Python services"] },
        { requirement: "PostgreSQL at scale", importance: "required", verdict: "partly_met", record_quotes: ["MySQL at 2,000 rps"] },
        { requirement: "Kubernetes", importance: "preferred", verdict: "no_evidence", record_quotes: [] },
      ],
      facts: [
        { kind: "role", statement: "Backend Engineer, Globex, 2021-03 to 2024-06" },
        { kind: "education", statement: "B.Tech, Example Institute" },
      ],
    },
    signals: { from_candidate: true, from_you: false },
    email: null,
    ...extra,
  };
}

export function clock(extra: Partial<AnswerClockData> = {}): AnswerClockData {
  return {
    org_id: ORG,
    timezone: "UTC",
    as_of: "2026-11-23T09:00:00Z",
    is_asking_paused: false,
    paused_since: null,
    pause_reason: null,
    owed: [
      { introduction_id: INTRO_A, role: { id: ROLE, title: "Senior Backend Engineer" }, name: "Demo Candidate A", responded_at: "2026-11-20T10:00:00Z", answer_due_at: "2026-11-27T10:00:00Z", business_days_left: 3, is_overdue: false },
    ],
    answered_last_30_days: { total: 4, on_time: 4 },
    ...extra,
  };
}
