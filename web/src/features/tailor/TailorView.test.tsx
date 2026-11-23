import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DISCARD_REASON_LABELS } from "@/lib/labels";
import { fail, ok, renderWithApi } from "@/test/render";
import { TailorView } from "./TailorView";

const JOB = 42;
const edit = (over: Record<string, unknown> = {}) => ({
  id: "e1",
  ordinal: 1,
  op: "rephrase",
  section: "experience",
  before_text: "Built billing services in Python for the payments team",
  after_text: "Built billing services in Python and PostgreSQL for the payments team",
  supporting_fact_ids: ["f-1"],
  decision: "pending",
  decided_at: null,
  ...over,
});
const view = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  job_id: JOB,
  job_title: "Platform Engineer",
  company_name: "Initech Demo",
  status: "ready",
  discarded_count: 2,
  discarded_by_reason: { new_entity: 1, changed_number: 1 },
  prompt_version: "abc",
  record_version: 5,
  created_at: "2026-11-23T10:00:00Z",
  edits: [edit()],
  ...over,
});
const packet = (over: Record<string, unknown> = {}) => ({
  id: "p1",
  job_id: JOB,
  job_title: "Platform Engineer",
  company_name: "Initech Demo",
  status: "ready",
  tailored_resume_id: "t1",
  answers: [
    { question_id: "why_role", question: "Why?", status: "drafted", answer: "I built platforms.", fact_ids: [], removed_sentences: 1 },
    { question_id: "expected_pay", question: "Pay?", status: "candidate_must_answer", answer: "", fact_ids: [], removed_sentences: 0 },
  ],
  cover_note: "A short note.",
  removed_sentences: 1,
  created_at: "2026-11-23T10:05:00Z",
  ...over,
});
const listOf = (...items: unknown[]) => ok(items);
const noPackets = { "GET /v1/applications/packets": listOf() };

afterEach(() => vi.unstubAllGlobals());

describe("TailorView", () => {
  it("offers Tailor my resume when there is none, and polls until it is ready", async () => {
    const user = userEvent.setup();
    let reads = 0;
    renderWithApi(<TailorView jobId={JOB} />, {
      ...noPackets,
      "GET /v1/tailored-resumes": () => (reads++ === 0 ? listOf() : listOf(view({ status: "ready" }))),
      "POST /v1/tailored-resumes": () => ok(view({ status: "generating", edits: [] }), 202),
    });
    await user.click(await screen.findByRole("button", { name: "Tailor my resume" }));
    expect(await screen.findByText(/1 edit proposed\. 2 discarded before you saw them/)).toBeInTheDocument();
  });

  it("always says how many were discarded, including 0, and lists counts by reason in words", async () => {
    const zero = renderWithApi(<TailorView jobId={JOB} />, {
      ...noPackets,
      "GET /v1/tailored-resumes": listOf(view({ discarded_count: 0, discarded_by_reason: {} })),
    });
    expect(await screen.findByRole("heading", { level: 2, name: "1 edit proposed. 0 discarded before you saw them" })).toBeInTheDocument();
    zero.unmount();

    const user = userEvent.setup();
    renderWithApi(<TailorView jobId={JOB} />, { ...noPackets, "GET /v1/tailored-resumes": listOf(view()) });
    await user.click(await screen.findByText(/Why were they discarded/));
    expect(screen.getByText(`${DISCARD_REASON_LABELS.new_entity}: 1`)).toBeInTheDocument();
    expect(screen.getByText(`${DISCARD_REASON_LABELS.changed_number}: 1`)).toBeInTheDocument();
  });

  it("has a plain label for every reason the verifier can give", () => {
    for (const code of ["no_supporting_fact", "changed_employer", "changed_title", "changed_date", "changed_number", "new_entity", "not_entailed"]) {
      expect(DISCARD_REASON_LABELS[code]).toBeTruthy();
    }
  });

  it("says so when every edit was discarded", async () => {
    renderWithApi(<TailorView jobId={JOB} />, {
      ...noPackets,
      "GET /v1/tailored-resumes": listOf(view({ edits: [], discarded_count: 3, discarded_by_reason: { new_entity: 3 } })),
    });
    expect(await screen.findByText(/Every proposed edit added something your record does not contain, so none are shown\. Your resume is unchanged\./)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /0 edits proposed\. 3 discarded/ })).toBeInTheDocument();
  });

  it("marks removed and added words with text a screen reader can say", async () => {
    renderWithApi(<TailorView jobId={JOB} />, { ...noPackets, "GET /v1/tailored-resumes": listOf(view()) });
    await screen.findByText(/1 edit proposed/);
    const added = document.querySelector("ins");
    expect(added).toHaveTextContent("Added: and PostgreSQL");
    const removed = view({ edits: [edit({ before_text: "Old words here", after_text: "Words here" })] });
    expect(removed.edits[0].before_text).toContain("Old");
  });

  it("shows a reorder as a reorder and records approve and reject per edit, printing the state", async () => {
    const user = userEvent.setup();
    const decided = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ decisions: [{ edit_id: "e1", decision: "approved" }] });
      return ok(view({ edits: [edit({ decision: "approved", decided_at: "2026-11-23T11:00:00Z" }), edit({ id: "e2", ordinal: 2, op: "reorder", before_text: "a\nb", after_text: "b\na" })] }));
    });
    renderWithApi(<TailorView jobId={JOB} />, {
      ...noPackets,
      "GET /v1/tailored-resumes": listOf(view({ edits: [edit(), edit({ id: "e2", ordinal: 2, op: "reorder", before_text: "a\nb", after_text: "b\na" })] })),
      "POST /v1/tailored-resumes/t1/decisions": decided,
    });
    await screen.findByText(/2 edits proposed/);
    expect(screen.getByText("Reordered to match the job's emphasis. No new skills.")).toBeInTheDocument();
    expect(screen.getAllByText("Not decided yet")).toHaveLength(2);
    const [first] = screen.getAllByRole("radiogroup", { name: "Your decision on this edit" });
    await user.click(within(first).getByRole("radio", { name: "Approve" }));
    await waitFor(() => expect(decided).toHaveBeenCalled());
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    expect(screen.getByText(/1 approved edit is used\. 1 undecided is left out\./)).toBeInTheDocument();
  });

  it("offers DOCX when the PDF cannot hold the characters", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "validation_failed", details: { fields: [{ issue: "pdf_unsupported_characters" }] } } }, { status: 422 })));
    renderWithApi(<TailorView jobId={JOB} />, { ...noPackets, "GET /v1/tailored-resumes": listOf(view()) });
    await user.click(await screen.findByRole("button", { name: "Export PDF" }));
    expect(await screen.findByText(/Export the DOCX instead/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export DOCX" })).toHaveAttribute("href", "/v1/tailored-resumes/t1/file?format=docx");
  });

  it("offers a retry when tailoring failed", async () => {
    renderWithApi(<TailorView jobId={JOB} />, { ...noPackets, "GET /v1/tailored-resumes": listOf(view({ status: "failed", edits: [] })) });
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("explains a refusal to start tailoring", async () => {
    const user = userEvent.setup();
    renderWithApi(<TailorView jobId={JOB} />, {
      ...noPackets,
      "GET /v1/tailored-resumes": listOf(),
      "POST /v1/tailored-resumes": () => fail(409, "conflict", { state: "no_resume" }),
    });
    await user.click(await screen.findByRole("button", { name: "Tailor my resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Upload a resume first");
  });
});

describe("the packet section", () => {
  const ready = { "GET /v1/tailored-resumes": listOf(view()) };

  it("prepares a packet, then shows the note, each answer with Copy, and what the candidate answers", async () => {
    const user = userEvent.setup();
    let reads = 0;
    const write = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: write } });
    renderWithApi(<TailorView jobId={JOB} />, {
      ...ready,
      "GET /v1/applications/packets": () => (reads++ === 0 ? listOf() : listOf(packet())),
      "POST /v1/applications/packets": () => ok(packet({ status: "draft", answers: [], cover_note: null }), 202),
    });
    await user.click(await screen.findByRole("button", { name: "Prepare packet" }));
    expect(await screen.findByText("A short note.")).toBeInTheDocument();
    expect(screen.getByText("You answer this")).toBeInTheDocument();
    expect(screen.getByText("1 drafted sentence was removed because your record does not support it.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download packet PDF" })).toHaveAttribute("href", "/v1/applications/packets/p1/file?format=pdf");
    await user.click(screen.getByRole("button", { name: "Copy the answer to: Why are you interested in this role?" }));
    await waitFor(() => expect(write).toHaveBeenCalledWith("I built platforms."));
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("records that the candidate applied, with the packet id", async () => {
    const user = userEvent.setup();
    const posted = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ job_id: JOB, application_packet_id: "p1" });
      return ok({ id: "a1", job_id: JOB, job_title: "x", company_name: "y", status: "applied", status_changed_at: "2026-11-23T12:00:00Z", application_packet_id: "p1", events: [] }, 201);
    });
    renderWithApi(<TailorView jobId={JOB} />, { ...ready, "GET /v1/applications/packets": listOf(packet()), "POST /v1/applications": posted });
    await user.click(await screen.findByRole("button", { name: "Record that I applied" }));
    expect(await screen.findByRole("link", { name: "your applications" })).toHaveAttribute("href", "/app/campaigns");
    expect(posted).toHaveBeenCalledTimes(1);
  });

  it("says when an application for this job is already recorded", async () => {
    const user = userEvent.setup();
    renderWithApi(<TailorView jobId={JOB} />, {
      ...ready,
      "GET /v1/applications/packets": listOf(packet()),
      "POST /v1/applications": () => fail(409, "conflict", { state: "application_exists" }),
    });
    await user.click(await screen.findByRole("button", { name: "Record that I applied" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already recorded applying");
  });
});
