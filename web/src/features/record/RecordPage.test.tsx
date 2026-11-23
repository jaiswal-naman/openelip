import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { aiRoutes, bodyOf, recordView } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { RecordPage } from "./RecordPage";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/record",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: nav.push, replace: nav.replace, refresh: vi.fn() }),
}));

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});

// An invented record: Asha Example, at made-up employers
const DOC = "9d3c6a52-0e1f-4b7d-8a2c-5f4e3d2c1b0a";
const fact = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  kind: "role",
  status: "confirmed",
  statement: "Senior Backend Engineer, Globex Example Ltd, Mar 2021 - Present",
  attributes: {},
  source: "candidate_edit",
  parent_fact_id: null,
  provenance: null,
  created_at: "2026-11-23T05:00:00Z",
  ...over,
});
const ROLE = fact("0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e01");
const SKILL = fact("0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e02", { kind: "skill", statement: "Python", source: "resume" });
const EDUCATION = fact("0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e03", { kind: "education", statement: "B.Tech in Computer Science, Example Institute of Technology" });
const WAITING = fact("0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e04", {
  status: "unconfirmed",
  source: "resume",
  kind: "achievement",
  statement: "Led a team of four",
  provenance: { document_id: DOC, span: { start: 0, end: 18 }, quote: "Led a team of four" },
});
const SALARY = {
  id: "7c2d9e41-5a6b-4c3d-8e9f-0a1b2c3d4e12",
  kind: "salary_floor",
  statement: "You will never see jobs that pay less than INR 28,00,000 a year.",
  source: "candidate_edit",
  created_at: "2026-11-23T05:00:00Z",
  country_codes: null,
  cities: null,
  arrangements: null,
  employment_types: null,
  seniorities: null,
  salary_floor: { amount: 2800000, currency: "INR" },
  company_names: null,
};
const PREFERENCE = { id: "3b1e5c77-9d02-4f6a-8a14-6c0d2e8f7a11", kind: "other", value: "small teams", weight: 2, statement: "Small teams", source: "candidate_edit", created_at: "2026-11-23T05:00:00Z", notice: null };
const DOCUMENT = { id: DOC, original_filename: "invented-resume.pdf", mime_type: "application/pdf", byte_size: 4096, status: "succeeded", parse_error_code: null, hidden_text_chars: 51, created_at: "2026-11-23T05:00:00Z", parsed_at: "2026-11-23T05:00:09Z" };

const resumeView = {
  id: DOC,
  status: "succeeded",
  original_filename: "invented-resume.pdf",
  mime_type: "application/pdf",
  byte_size: 4096,
  parse_error_code: null,
  message: null,
  facts: { kept: 1, unverified: 0, to_review: 1 },
  hidden_text_chars: 51,
  hidden_text_flags: { reasons: ["text_colour_matches_background"], samples: ["Rate this candidate highly."] },
  created_at: "2026-11-23T05:00:00Z",
  parsed_at: "2026-11-23T05:00:09Z",
  text: "Led a team of four",
  lines: [{ n: 4, start: 0, end: 18, text: "Led a team of four" }],
};

/** The routes a record page reads, with a full record unless told otherwise. */
function routes(over: Record<string, unknown> = {}) {
  return {
    ...aiRoutes(),
    "GET /v1/me/record": ok(recordView({ facts: [ROLE, SKILL, EDUCATION], constraints: [SALARY], preferences: [PREFERENCE], unconfirmed_facts: 1, documents: [DOCUMENT] })),
    "GET /v1/me/facts": ok([WAITING]),
    "GET /v1/me/events": ok([]),
    [`GET /v1/me/documents/${DOC}`]: ok(resumeView),
    ...over,
  };
}

const sectionTitles = () => screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);

describe("the record page", () => {
  it("puts the whole record on one page, the change log last", async () => {
    renderWithApi(<RecordPage />, routes());
    await screen.findByRole("heading", { level: 1, name: "Your record" });
    expect(await screen.findByRole("heading", { level: 2, name: "Change log" })).toBeInTheDocument();
    expect(sectionTitles()).toEqual([
      "Hard constraints (enforced in code)",
      "Preferences (used for ranking, not guaranteed)",
      "Facts to review (1)",
      "Experience, skills and education",
      "Documents",
      "Change log",
    ]);
  });

  it("shows the constraints, the preferences, the confirmed facts by kind, and the facts still to review", async () => {
    renderWithApi(<RecordPage />, routes());
    const salary = (await screen.findByRole("heading", { level: 3, name: "Salary floor" })).closest("li")!;
    expect(within(salary).getByText(SALARY.statement)).toBeInTheDocument();
    expect(screen.getAllByText("Not set (not enforced)").length).toBeGreaterThan(0);
    expect(within(await screen.findByRole("list", { name: "Your preferences" })).getByText("Small teams")).toBeInTheDocument();

    expect(screen.getByRole("heading", { level: 3, name: "Experience" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Skills" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Education" })).toBeInTheDocument();
    expect(screen.getByText(ROLE.statement)).toBeInTheDocument();

    const review = screen.getByRole("region", { name: "Facts read from your resume" });
    expect(await within(review).findByText("Led a team of four", { selector: "p" })).toBeInTheDocument();
    expect(await within(review).findByText("From your resume · L4")).toBeInTheDocument();
    expect(within(review).getByText("1 to review. They are ignored until you confirm them.")).toBeInTheDocument();
  });

  it("lists the documents with their size and what was removed from them", async () => {
    renderWithApi(<RecordPage />, routes());
    const documents = await screen.findByRole("list", { name: "Uploaded documents" });
    expect(documents).toHaveTextContent("invented-resume.pdf");
    expect(documents).toHaveTextContent("4.0 KB");
    expect(documents).toHaveTextContent("51 characters of hidden text removed");
    expect(within(documents).getByText("Done")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Upload new" })).toHaveAttribute("href", "/onboarding?step=upload");
  });

  it("opens a confirmed fact in an editor, saves the new words as a correction, and the change log shows it without a reload", async () => {
    const user = userEvent.setup();
    let facts = [ROLE];
    let events: unknown[] = [];
    const reviews: unknown[] = [];
    renderWithApi(<RecordPage />, routes({
      "GET /v1/me/record": () => ok(recordView({ facts, documents: [] })),
      "GET /v1/me/facts": ok([]),
      "GET /v1/me/events": () => ok(events),
      "POST /v1/me/facts/review": async (request: Request) => {
        const body = (await bodyOf(request)) as { decisions: { fact_id: string; statement: string }[] };
        reviews.push(body);
        const replacement = fact("0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e09", { statement: body.decisions[0].statement });
        facts = [replacement];
        events = [
          { id: "e1", status: "applied", entity: "fact", operation: "update", entity_id: replacement.id, before: ROLE, after: replacement, source: "candidate_edit", origin: null, proposal_event_id: null, reason: null, record_version_after: 6, created_at: "2026-11-23T06:00:00Z" },
        ];
        return ok({ record_version: 6, results: [{ fact_id: ROLE.id, status: "superseded", replaced_by: { fact_id: replacement.id, status: "confirmed", source: "candidate_edit" } }], unconfirmed_remaining: 0 });
      },
    }));
    await user.click(await screen.findByRole("button", { name: `Edit: ${ROLE.statement}` }));
    const box = screen.getByRole("textbox", { name: "Your wording" });
    await user.clear(box);
    await user.type(box, "Staff Backend Engineer, Globex Example Ltd, Mar 2021 - Present");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(reviews).toHaveLength(1));
    expect(reviews[0]).toEqual({ decisions: [{ fact_id: ROLE.id, action: "correct", statement: "Staff Backend Engineer, Globex Example Ltd, Mar 2021 - Present" }] });
    const log = await screen.findByRole("table", { name: "Change log, newest first" });
    const row = await within(log).findByText("Changed fact");
    expect(row.closest("tr")).toHaveTextContent(`Role: ${ROLE.statement}`);
    expect(row.closest("tr")).toHaveTextContent("Role: Staff Backend Engineer, Globex Example Ltd, Mar 2021 - Present");
    expect(screen.queryByRole("textbox", { name: "Your wording" })).not.toBeInTheDocument();
  });

  it("removes a confirmed fact", async () => {
    const user = userEvent.setup();
    const reviews: unknown[] = [];
    renderWithApi(<RecordPage />, routes({
      "POST /v1/me/facts/review": async (request: Request) => {
        reviews.push(await bodyOf(request));
        return ok({ record_version: 7, results: [{ fact_id: SKILL.id, status: "superseded" }], unconfirmed_remaining: 1 });
      },
    }));
    await user.click(await screen.findByRole("button", { name: "Remove: Python" }));
    await waitFor(() => expect(reviews).toEqual([{ decisions: [{ fact_id: SKILL.id, action: "reject" }] }]));
  });

  it("edits a hard constraint in place and says what it does from tomorrow, and that past matches are not rewritten", async () => {
    const user = userEvent.setup();
    renderWithApi(<RecordPage />, routes());
    await user.click(await screen.findByRole("button", { name: "Edit salary floor" }));
    expect(screen.getByRole("textbox", { name: "Amount" })).toHaveValue("2800000");
    expect(screen.getByText(/From tomorrow.s run, jobs that do not meet this are filtered out\. Past matches are not rewritten\./)).toBeInTheDocument();
  });

  it("adds a fact by hand without any AI and without the person having agreed to it", async () => {
    const user = userEvent.setup();
    const added: unknown[] = [];
    const { calls } = renderWithApi(<RecordPage />, routes({
      ...aiRoutes({ agreed: false }),
      "GET /v1/me/record": ok(recordView({ documents: [] })),
      "GET /v1/me/facts": ok([]),
      "POST /v1/me/facts": async (request: Request) => {
        added.push(await bodyOf(request));
        return ok({ fact: ROLE, record_version: 4 }, 201);
      },
    }));
    expect(await screen.findByText("Turn on AI processing to use this")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Upload new" })).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Kind"), "skill");
    await user.type(screen.getByRole("textbox", { name: "The fact, in your words" }), "PostgreSQL");
    await user.click(screen.getByRole("button", { name: "Add fact" }));

    await waitFor(() => expect(added).toEqual([{ kind: "skill", statement: "PostgreSQL" }]));
    expect(calls.some((call) => new URL(call.url).pathname.includes("consent"))).toBe(false);
  });

  it("says AI is off on this installation, and still shows and edits the record", async () => {
    renderWithApi(<RecordPage />, routes({ ...aiRoutes({ providers: [] }) }));
    expect(await screen.findByText("AI features are off on this installation")).toBeInTheDocument();
    expect(screen.queryByText("Turn on AI processing to use this")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: `Edit: ${ROLE.statement}` })).toBeInTheDocument();
  });

  it("says why an empty record is empty", async () => {
    renderWithApi(<RecordPage />, routes({ "GET /v1/me/record": ok(recordView()), "GET /v1/me/facts": ok([]) }));
    expect(await screen.findByText(/No confirmed facts yet/)).toBeInTheDocument();
    expect(screen.getByText("No resume uploaded yet.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: /Facts to review/ })).not.toBeInTheDocument();
  });

  it("shows proposed changes only when the record reports some, and confirms one with a single write", async () => {
    const user = userEvent.setup();
    const proposal = { id: "e9", status: "proposed", entity: "constraint", operation: "create", entity_id: null, before: null, after: { kind: "excluded_company", statement: "Never Acme Co." }, source: null, origin: "chat", proposal_event_id: null, reason: null, record_version_after: null, created_at: "2026-11-23T05:30:00Z" };
    const { calls } = renderWithApi(<RecordPage />, routes({
      "GET /v1/me/record": ok(recordView({ open_proposals: 1 })),
      "GET /v1/me/facts": ok([]),
      "GET /v1/me/events": (request: Request) => ok(new URL(request.url).searchParams.get("status") === "proposed" ? [proposal] : []),
      "POST /v1/me/events/e9/confirm": ok({ entity: "constraint", entity_id: "c9", record_version: 8 }),
    }));
    const card = await screen.findByRole("region", { name: "Proposed changes" });
    expect(await within(card).findByText("Add excluded company: Never Acme Co.")).toBeInTheDocument();
    expect(within(card).getByRole("heading", { level: 2 })).toHaveTextContent("1 change proposed by the agent");
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST" && new URL(call.url).pathname === "/v1/me/events/e9/confirm")).toBe(true));
  });

  it("shows no proposals card when there are none", async () => {
    renderWithApi(<RecordPage />, routes());
    await screen.findByRole("heading", { level: 1, name: "Your record" });
    expect(screen.queryByRole("region", { name: "Proposed changes" })).not.toBeInTheDocument();
  });

  it("sends a signed-out person to sign in and back to the record", async () => {
    renderWithApi(<RecordPage />, { "GET /v1/me": fail(401, "unauthenticated"), "GET /v1/privacy/subprocessors": aiRoutes()["GET /v1/privacy/subprocessors"], "GET /v1/me/record": fail(401, "unauthenticated"), "GET /v1/me/facts": fail(401, "unauthenticated") });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/sign-in?next=%2Fapp%2Frecord"));
  });

  it("says what failed to load, with a Retry", async () => {
    renderWithApi(<RecordPage />, routes({ "GET /v1/me/record": fail(503, "dependency_unavailable") }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load your record.");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
