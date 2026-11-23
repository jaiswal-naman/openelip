import type { Job, JobDetail, Requirement } from "./types";

/**
 * Invented jobs for tests, in the shape the API returns. Example Co and example.test are made up: nothing here is a
 * real posting, company or address.
 */
export function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 7,
    title: "Platform Engineer",
    company_name: "Example Co",
    location_raw: "Pune, India",
    country: "IN",
    city: "Pune",
    work_arrangements: ["hybrid"],
    is_remote: false,
    employment_type: "full_time",
    pay: null,
    apply_url: "https://jobs.example.test/7",
    source: { ats: "greenhouse", board: "example-co" },
    status: "open",
    posted_at: null,
    last_verified_at: "2026-11-22T12:21:54Z",
    content_version: 1,
    ...overrides,
  };
}

export function jobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    ...job(),
    description: "Example Co builds tools for teams.",
    closed_at: null,
    requirements_version: 1,
    requirements: [],
    ...overrides,
  };
}

/** A requirement whose span is where `text` first appears in `description`, counted in code points as the API counts. */
export function requirementIn(description: string, text: string, overrides: Partial<Requirement> = {}): Requirement {
  const startUnit = description.indexOf(text);
  if (startUnit < 0) throw new Error(`"${text}" is not in the description`);
  const start = Array.from(description.slice(0, startUnit)).length;
  return {
    id: 100 + (overrides.ordinal ?? 1),
    ordinal: 1,
    text,
    importance: "required",
    kind: "skill",
    span: { start, end: start + Array.from(text).length },
    min_years: null,
    ...overrides,
  };
}
