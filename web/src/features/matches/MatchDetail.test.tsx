import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { MatchDetail } from "./MatchDetail";
import { DECISION_ID, DOCUMENT_ID, matchData, POSTING, RESUME, resumeDocument, spanIn } from "./testing";

const nav = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.search),
  usePathname: () => `/app/matches/${DECISION_ID}`,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const scrollIntoView = vi.fn();

/** jsdom has neither scrollIntoView nor matchMedia, so the test says what the browser would. */
beforeEach(() => {
  nav.search = "";
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  window.history.replaceState(null, "", `/app/matches/${DECISION_ID}`);
});

const DOCUMENT_ROUTE = `GET /v1/me/documents/${DOCUMENT_ID}`;
const FEEDBACK_ROUTE = `POST /v1/matches/${DECISION_ID}/feedback`;
const resumeRoutes = { [DOCUMENT_ROUTE]: ok(resumeDocument()) };

const marks = (container: HTMLElement) => [...container.querySelectorAll("mark")].map((mark) => mark.textContent);
const announced = (text: string) => screen.getByText(text).closest('[role="status"]');
const row = (name: RegExp) => screen.getByRole("row", { name });
const documentCalls = (calls: Request[]) => calls.filter((call) => new URL(call.url).pathname.startsWith("/v1/me/documents"));

describe("the match page", () => {
  it("shows the job, where it came from, what it says about pay, and the summary the match stored", () => {
    renderWithApi(<MatchDetail match={matchData()} />);
    expect(screen.getByRole("heading", { level: 1, name: "Demo posting (invented): Quillscript Platform Engineer" })).toBeInTheDocument();
    expect(screen.getByText("Initech Demo")).toBeInTheDocument();
    expect(screen.getByText("Remote, United States")).toBeInTheDocument();
    expect(screen.getByText("Pay: Not stated in the posting")).toBeInTheDocument();
    expect(screen.getByText("Source: Lever")).toBeInTheDocument();
    expect(screen.getByText(/^Verified open/)).toBeInTheDocument();
    expect(screen.getByText("Meets 2 of 2 required. 1 preferred item has no evidence.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /View posting for Demo posting/ })).toHaveAttribute("href", "https://apply.initech-demo.invalid/1");
  });

  it("marks nothing, and fetches no resume, until a requirement is selected", () => {
    const { container, calls } = renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    expect(marks(container)).toEqual([]);
    expect(screen.getByText("Select a requirement to see where your resume states it.")).toBeInTheDocument();
    expect(documentCalls(calls)).toHaveLength(0);
  });

  it("marks the exact job sentence and the cited resume line when a row is selected, and says so (MAT-04)", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    await user.click(row(/3\+ years of backend experience/));

    await waitFor(() => expect(marks(container)).toEqual(["3+ years of backend experience", "Backend engineer at Initech Demo, 2019 to 2024."]));
    // each mark sits in the right pane
    expect(within(screen.getByRole("region", { name: "Job text" })).getByText("3+ years of backend experience").tagName).toBe("MARK");
    expect(within(screen.getByRole("region", { name: "Your resume" })).getByText("Backend engineer at Initech Demo, 2019 to 2024.").tagName).toBe("MARK");
    expect(announced("Job sentence and resume line 2 highlighted.")).toHaveAttribute("aria-live", "polite");
    expect(scrollIntoView).toHaveBeenCalledTimes(2); // both panes are scrolled to their mark

    const [request] = documentCalls(calls);
    expect(new URL(request.url).search).toBe("?include=text");
  });

  it("keeps the selected row in the address, so a reload or a shared link lands on the same highlight", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    await user.click(row(/shipped services in Quillscript/));
    expect(new URL(window.location.href).searchParams.get("req")).toBe("12");
    await user.click(row(/shipped services in Quillscript/)); // pressing it again clears it
    expect(new URL(window.location.href).searchParams.has("req")).toBe(false);
  });

  it("opens with the row the address names selected", async () => {
    nav.search = "req=12";
    const { container } = renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    expect(row(/shipped services in Quillscript/)).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(marks(container)).toEqual(["shipped services in Quillscript", "Built Quillscript services on PostgreSQL."]));
    expect(announced("Job sentence and resume line 3 highlighted.")).not.toBeNull();
  });

  it.each(["999", "abc", "-1", "12abc", "1e1", ""])("ignores ?req=%j when it names no requirement of this match", (value) => {
    nav.search = `req=${value}`;
    const { container } = renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    expect(screen.getAllByRole("row").filter((item) => item.getAttribute("aria-selected") === "true")).toHaveLength(0);
    expect(marks(container)).toEqual([]);
  });

  it("marks only the job sentence for a requirement with no evidence, and says the record has nothing", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderWithApi(<MatchDetail match={matchData()} />, resumeRoutes);
    await user.click(row(/Kubernetes in production/));
    expect(marks(container)).toEqual(["Kubernetes in production"]);
    expect(announced("Job sentence highlighted.")).not.toBeNull();
    expect(screen.getByText("No evidence in your record for this requirement.")).toBeInTheDocument();
    expect(documentCalls(calls)).toHaveLength(0);
  });

  it("shows a fact the person wrote with no resume line, and asks for no resume", async () => {
    const user = userEvent.setup();
    const match = matchData();
    match.evidence[0].record_citations = [{ ...match.evidence[0].record_citations[0], source: null }];
    const { container, calls } = renderWithApi(<MatchDetail match={match} />, resumeRoutes);
    await user.click(row(/3\+ years of backend experience/));
    expect(marks(container)).toEqual(["3+ years of backend experience"]);
    expect(screen.getByText("This evidence was added by you, so it has no line in a resume.")).toBeInTheDocument();
    expect(documentCalls(calls)).toHaveLength(0);
  });

  it("does not mark a posting that has changed, shows the stored quote, and still marks the resume line", async () => {
    const user = userEvent.setup();
    const match = matchData({ description: "A rewritten posting that no longer says any of this." });
    const { container } = renderWithApi(<MatchDetail match={match} />, resumeRoutes);
    await user.click(row(/3\+ years of backend experience/));
    await waitFor(() => expect(marks(container)).toEqual(["Backend engineer at Initech Demo, 2019 to 2024."]));
    expect(screen.getByText(/This posting has changed since/)).toHaveTextContent("“3+ years of backend experience”");
    expect(announced("Resume line 2 highlighted.")).not.toBeNull();
  });

  it("withholds a citation whose resume text has changed, and never marks a nearby guess", async () => {
    const user = userEvent.setup();
    const { container } = renderWithApi(<MatchDetail match={matchData()} />, {
      [DOCUMENT_ROUTE]: ok(resumeDocument({ text: RESUME.replace("Backend engineer", "Frontend engineer") })),
    });
    await user.click(row(/3\+ years of backend experience/));
    expect(await screen.findByText("Withheld: citation did not resolve")).toBeInTheDocument();
    expect(marks(container)).toEqual(["3+ years of backend experience"]);
    expect(announced("Job sentence highlighted.")).not.toBeNull();
  });

  it("counts code points, so an emoji before the cited words does not move the mark", async () => {
    const user = userEvent.setup();
    const description = `😀😀 ${POSTING}`;
    const match = matchData({ description });
    for (const item of match.evidence) {
      item.requirement.job_citation.span = spanIn(description, item.requirement.text);
    }
    const { container } = renderWithApi(<MatchDetail match={match} />, resumeRoutes);
    await user.click(row(/shipped services in Quillscript/));
    await waitFor(() => expect(marks(container)[0]).toBe("shipped services in Quillscript"));
  });

  it("says what failed, with Retry, when the resume text cannot be loaded", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchDetail match={matchData()} />, { [DOCUMENT_ROUTE]: fail(503, "dependency_unavailable") });
    await user.click(row(/3\+ years of backend experience/));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load your resume text.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders posting text as text, never as markup (threat T-04-31)", () => {
    const description = `<script>alert(1)</script><img src=x onerror="alert(2)"> ${POSTING}`;
    const { container } = renderWithApi(<MatchDetail match={matchData({ description })} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
  });

  it("tells a closed job from an open one, and says what changed in one shown again (MAT-07)", () => {
    const base = matchData();
    renderWithApi(
      <MatchDetail
        match={matchData({
          job: { ...base.job, status: "closed" },
          reason_codes: ["top_ranked", "material_change"],
          change_note: "Pay range added.",
        })}
      />,
    );
    expect(screen.getByText("This job has closed. It is kept here for your records.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View posting/ })).not.toBeInTheDocument();
    expect(screen.getByText("Changed since you saw it")).toBeInTheDocument();
    expect(screen.getByText("Pay range added.")).toBeInTheDocument();
  });

  it("lists each hard constraint with Satisfied or Not stated (unverified)", () => {
    renderWithApi(<MatchDetail match={matchData()} />);
    const table = screen.getByRole("table", { name: "Your hard constraints and whether this job meets them" });
    expect(within(table).getByRole("row", { name: /Location/ })).toHaveTextContent("Satisfied");
    expect(within(table).getByRole("row", { name: /Salary floor/ })).toHaveTextContent("Not stated (unverified)");
  });
});

describe("save and dismiss (MAT-08)", () => {
  it("saves at once, keeps the answer, and unsaves", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<MatchDetail match={matchData()} />, {
      [FEEDBACK_ROUTE]: ok({ is_saved: true, is_dismissed: false, proposal_event_id: null }, 201),
    });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("button", { name: "Unsave" })).toBeInTheDocument();
    const [request] = calls.filter((call) => call.method === "POST");
    await expect(request.clone().json()).resolves.toEqual({ kind: "save" });
    expect(request.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("goes back and says so when the server refuses, so a refused save is never shown as done", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchDetail match={matchData()} />, { [FEEDBACK_ROUTE]: fail(503, "dependency_unavailable") });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A service this needs is unavailable. Try again shortly. Your choice was not saved.");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("opens with the saved state the match already has", () => {
    renderWithApi(<MatchDetail match={matchData({ feedback: { is_saved: true, is_dismissed: false } })} />);
    expect(screen.getByRole("button", { name: "Unsave" })).toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("asks for a reason before it dismisses, then sends it with the optional comment", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<MatchDetail match={matchData()} />, {
      [FEEDBACK_ROUTE]: ok({ is_saved: false, is_dismissed: true, proposal_event_id: null }, 201),
    });
    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    const dialog = await screen.findByRole("dialog", { name: "Dismiss this job?" });
    expect(within(dialog).getByRole("button", { name: "Dismiss job" })).toBeDisabled(); // no reason yet

    await user.click(within(dialog).getByRole("radio", { name: "Wrong location" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Anything to add? (optional)" }), "Onsite only");
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));

    expect(await screen.findByText("You dismissed this job. Save it to take that back.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const [request] = calls.filter((call) => call.method === "POST");
    await expect(request.clone().json()).resolves.toEqual({ kind: "dismiss", reason: "location", comment: "Onsite only" });
  });

  it("says a suggested record change changes nothing until it is confirmed, and links to where to confirm it", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchDetail match={matchData()} />, {
      [FEEDBACK_ROUTE]: ok({ is_saved: false, is_dismissed: true, proposal_event_id: "e1" }, 201),
    });
    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: "Not this company" }));
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));

    const notice = await screen.findByText(/We suggested a change to your record/);
    expect(notice).toHaveTextContent("It changes nothing until you confirm it");
    expect(within(notice).getByRole("link", { name: "your record" })).toHaveAttribute("href", "/app/record");
  });

  it("keeps the dialog open, with what was typed, and says so when the dismissal fails", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchDetail match={matchData()} />, { [FEEDBACK_ROUTE]: fail(503, "dependency_unavailable") });
    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: "Other" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Anything to add? (optional)" }), "Not for me");
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Nothing was dismissed.");
    expect(within(dialog).getByRole("textbox", { name: "Anything to add? (optional)" })).toHaveValue("Not for me");
    expect(screen.queryByText("You dismissed this job. Save it to take that back.")).not.toBeInTheDocument();
  });
});
