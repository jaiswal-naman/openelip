import { ok } from "./render";

/** Invented people, providers and records for the Career Record tests. example.com is reserved for examples. */

export const VERSION = "a1b2c3d4e5f6";

/**
 * The signed-in account as GET /v1/me answers it. `ai` overrides fields of the ai_processing consent; `over` overrides
 * fields of the account itself (the brief schedule, the capabilities, the consents).
 */
export function account(ai: Record<string, unknown> = {}, over: Record<string, unknown> = {}) {
  return {
    id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780",
    email: "asha@example.com",
    timezone: "UTC",
    created_at: "2026-11-22T00:00:00Z",
    brief: { frequency: "daily", weekday: null },
    capabilities: [],
    consents: [
      { purpose: "terms", granted: false, granted_at: null, text_version: null },
      {
        purpose: "ai_processing",
        granted: false,
        granted_at: null,
        text_version: null,
        providers: ["Example Model Provider"],
        current_text_version: VERSION,
        ...ai,
      },
      { purpose: "brief_email", granted: false, granted_at: null, text_version: null },
      { purpose: "open_to_introductions", granted: false, granted_at: null, text_version: null },
    ],
    ...over,
  };
}

export const AGREED = { granted: true, granted_at: "2026-11-22T10:00:00Z", text_version: VERSION };

export const PROVIDER = {
  name: "Example Model Provider",
  kind: "model_provider",
  purpose: "Reads resume lines and returns the facts they state.",
  receives: "Resume lines and chat messages, which can identify a person.",
  tasks: ["resume.facts.roles"],
};
export const EMAIL_HOST = {
  name: "mail.example.test",
  kind: "email",
  purpose: "Delivers sign-in codes.",
  receives: "Email addresses.",
  tasks: [],
};
export const OUTSIDE_COPIES = "Copies made outside this system, for example an email that has already been delivered to someone else, cannot be reached by deletion.";

export const subprocessors = (...items: unknown[]) => ok({ items, outside_copies_statement: OUTSIDE_COPIES });

/** The two routes every screen that gates on AI reads. Signed in; agreed or not; with model providers or none. */
export function aiRoutes({ agreed = true, providers = [PROVIDER] }: { agreed?: boolean; providers?: unknown[] } = {}) {
  return {
    "GET /v1/me": ok(account(agreed ? AGREED : {})),
    "GET /v1/privacy/subprocessors": subprocessors(...providers, EMAIL_HOST),
  };
}

export const bodyOf = (request: Request) => request.clone().json();

/** GET /v1/me/record. Override any of its parts. */
export function recordView(over: Record<string, unknown> = {}) {
  return {
    record_version: 3,
    facts: [],
    constraints: [],
    preferences: [],
    unconfirmed_facts: 0,
    open_proposals: 0,
    documents: [],
    ...over,
  };
}
