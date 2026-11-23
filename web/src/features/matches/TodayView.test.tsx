import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { account } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { TodayView } from "./TodayView";
import { brief, briefDecision, DECISION_ID, decisionView, matchView, OTHER_DECISION_ID, todayMatches } from "./testing";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), search: "" }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/today",
  useSearchParams: () => new URLSearchParams(nav.search),
  useRouter: () => ({ push: nav.push, replace: nav.replace, refresh: vi.fn() }),
}));

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.search = "";
  Element.prototype.scrollIntoView = vi.fn();
});

const BRIEF_ID = brief().id;
const withModel = account({}, { capabilities: ["match.evidence.score"] });

/** A signed-in person with a model, a brief and one match, unless a test says otherwise. */
function routes(over: Record<string, Response> = {}) {
  return {
    "GET /v1/me": ok(withModel),
    "GET /v1/matches": ok(todayMatches()),
    "GET /v1/briefs": ok([{ id: BRIEF_ID, local_date: "2026-11-23", status: "composed", item_count: 1, delivery: brief().delivery }]),
    [`GET /v1/briefs/${BRIEF_ID}`]: ok(brief()),
    "GET /v1/decisions": ok([decisionView()]),
    ...over,
  };
}

describe("Today: the decisions", () => {
  it("opens on the decisions waiting, names the day and how many need the person, and says when the brief was emailed", async () => {
    renderWithApi(<TodayView />, routes());
    expect(await screen.findByRole("heading", { level: 1, name: "Today, Mon 23 Nov" })).toBeInTheDocument();
    expect(screen.getByText("1 decision needs you.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Decisions (1)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Matches (1)" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Why not" })).toBeInTheDocument();
    expect(screen.getByText(/^Brief emailed/)).toBeInTheDocument();
  });

  it("shows each decision as a card with Open, Save and Dismiss, the open link first", async () => {
    renderWithApi(<TodayView />, routes());
    const card = await screen.findByRole("article", { name: "Demo posting (invented): Quillscript Platform Engineer at Initech Demo" });
    expect(within(card).getByText("Meets 2 of 2 required. 1 preferred item has no evidence.")).toBeInTheDocument();
    const open = within(card).getByRole("link", { name: "Open" });
    expect(open).toHaveAttribute("href", `/app/matches/${DECISION_ID}`);
    expect(within(card).getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Dismiss..." })).toBeInTheDocument();
    const controls = within(card).getAllByRole("link").concat(within(card).getAllByRole("button"));
    expect(controls[0]).toBe(open);
  });

  it("lists what was done since the last brief, counted from the match runs", async () => {
    renderWithApi(<TodayView />, routes());
    const section = await screen.findByRole("region", { name: "What was done since your last brief" });
    expect(section).toHaveTextContent("Checked the index on request: 1 shown, 2 held back, 3 filtered by your constraints");
    expect(section).toHaveTextContent("Counted from your match runs.");
  });

  it("says a match nobody has assessed is not assessed, with no verdict and no save or dismiss", async () => {
    const decision = briefDecision({
      title: "Demo posting (invented): Onsite Support Engineer at Initech Demo",
      why: "Not yet assessed: the search ranked it high, but no model has checked it against your record.",
      is_assessed: false,
      actions: [{ action: "open", method: "GET", path: `/v1/decisions/${DECISION_ID}` }],
    });
    renderWithApi(<TodayView />, routes({ [`GET /v1/briefs/${BRIEF_ID}`]: ok(brief({ decisions: [decision] })) }));
    const card = await screen.findByRole("article", { name: /Onsite Support Engineer/ });
    expect(within(card).getByText("Not yet assessed", { selector: "p" })).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "See why" })).toHaveAttribute("href", `/app/matches/${DECISION_ID}`);
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
  });

  it("sends the record changes someone suggested to the record page", async () => {
    const proposals = briefDecision({
      kind: "record_proposals",
      title: "2 changes to your record are waiting for your answer",
      why: "Nothing changes in your record until you confirm it.",
      ref: { count: 2 },
      actions: [{ action: "review", method: "GET", path: "/v1/me/events?status=proposed" }],
    });
    renderWithApi(<TodayView />, routes({ [`GET /v1/briefs/${BRIEF_ID}`]: ok(brief({ decisions: [proposals] })) }));
    const card = await screen.findByRole("article", { name: /2 changes to your record/ });
    expect(within(card).getByRole("link", { name: "Review" })).toHaveAttribute("href", "/app/record");
  });

  it("says plainly that nothing needs the person when the brief holds no decision, and opens on the matches", async () => {
    renderWithApi(<TodayView />, routes({ [`GET /v1/briefs/${BRIEF_ID}`]: ok(brief({ decisions: [] })) }));
    expect(await screen.findByText("Nothing needs you today.", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Matches (1)" })).toHaveAttribute("aria-selected", "true");
  });

  it("says the first brief comes tomorrow when none has been made yet", async () => {
    nav.search = "view=decisions";
    renderWithApi(<TodayView />, routes({ "GET /v1/briefs": ok([]) }));
    expect(await screen.findByText(/Your first brief arrives by 07:00 tomorrow/)).toBeInTheDocument();
  });

  it("marks the decision an email link opened, and brings it into view", async () => {
    nav.search = `decision=${DECISION_ID}`;
    renderWithApi(<TodayView />, routes());
    const card = await screen.findByRole("article", { name: /Quillscript Platform Engineer/ });
    expect(card).toHaveAttribute("aria-current", "true");
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });

  it("offers to resume a paused brief", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/me": ok(account({}, { capabilities: ["match.evidence.score"], brief: { frequency: "paused", weekday: null } })) }));
    const link = await screen.findByRole("link", { name: "Resume brief" });
    expect(link).toHaveAttribute("href", "/app/settings?section=brief");
  });

  it("says what failed, with Retry, when the brief cannot be loaded, and keeps the rest of the page", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/briefs": fail(503, "dependency_unavailable") }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load your brief.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Why not" })).toBeInTheDocument();
  });

  it("sends a signed-out person to sign in and back to Today", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/me": fail(401, "unauthenticated") }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/sign-in?next=%2Fapp%2Ftoday"));
  });
});

describe("Today: the matches", () => {
  beforeEach(() => {
    nav.search = "view=matches";
  });

  it("shows each of today's matches in rank order with its summary, source and the unverified fields as unverified", async () => {
    const second = matchView({
      decision_id: OTHER_DECISION_ID,
      rank_position: 2,
      summary: "Meets 1 of 2 required.",
      unverified: ["salary_not_stated"],
      job: { ...matchView().job, id: 502, title: "Demo posting (invented): Data Reliability Engineer" },
    });
    renderWithApi(<TodayView />, routes({ "GET /v1/matches": ok(todayMatches({ matches: [matchView(), second] })) }));
    const cards = await screen.findAllByRole("article");
    expect(cards.map((card) => within(card).getByRole("heading").textContent)).toEqual([
      "Demo posting (invented): Quillscript Platform Engineer",
      "Demo posting (invented): Data Reliability Engineer",
    ]);
    expect(within(cards[0]).getByRole("heading", { level: 2 })).toBeInTheDocument(); // Today has no heading of its own between the page title and the cards
    expect(within(cards[0]).getByRole("link", { name: /Quillscript Platform Engineer/ })).toHaveAttribute("href", `/app/matches/${DECISION_ID}`);
    expect(within(cards[0]).getByText("Source: Lever")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Pay: not stated (unverified)")).toBeInTheDocument();
    expect(within(cards[0]).queryByRole("list", { name: "Not stated in the posting" })).not.toBeInTheDocument();
  });

  it("says what changed in a job shown again (MAT-07)", async () => {
    const changed = matchView({ reason_codes: ["top_ranked", "material_change"], change_note: "Pay range added." });
    renderWithApi(<TodayView />, routes({ "GET /v1/matches": ok(todayMatches({ matches: [changed] })) }));
    const card = await screen.findByRole("article");
    expect(within(card).getByText("Changed since you saw it")).toBeInTheDocument();
    expect(within(card).getByText("Pay range added.")).toBeInTheDocument();
  });

  it("says why there are none, with what the run did, when zero jobs qualify", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/matches": ok(todayMatches({ matches: [] })) }));
    expect(await screen.findByText(/Nothing in the current index meets your constraints well enough today/)).toHaveTextContent(
      "Checked the index: 1 shown, 2 held back, 3 filtered by your constraints.",
    );
    expect(screen.getByRole("link", { name: "See what was held back" })).toHaveAttribute("href", "/app/today?view=why-not");
  });

  it("says no run has finished yet when none has", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/matches": ok(todayMatches({ matches: [], run: null })) }));
    expect(await screen.findByText(/No match run has finished for this day yet/)).toBeInTheDocument();
  });

  it("says requirement tables need a model key when this server has none, and does not pretend the day was empty", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/me": ok(account()), "GET /v1/matches": ok(todayMatches({ matches: [], run: null })) }));
    expect(await screen.findByText(/Requirement tables need a model key/)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing in the current index meets/)).not.toBeInTheDocument();
  });

  it("says what failed, with Retry, when today's matches cannot be loaded", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/matches": fail(500, "internal_error") }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load today's matches.")).toBeInTheDocument();
  });
});

describe("Today: why not", () => {
  beforeEach(() => {
    nav.search = "view=why-not";
  });

  it("groups what was held back by reason with counts, the constraint that filtered each job, and the gaps of a scored one", async () => {
    const withheld = decisionView({
      decision_id: "9c3e4f50-6172-4c8d-ae9f-1a2b3c4d5e6f",
      outcome: "withheld",
      reason_codes: ["required_not_met"],
      constraint: null,
      gaps: [{ requirement: "Owns on-call rotations", importance: "required", verdict: "not_met" }],
      job: { ...decisionView().job, id: 503, title: "Demo posting (invented): Site Reliability Engineer" },
    });
    renderWithApi(<TodayView />, routes({ "GET /v1/decisions": ok([decisionView(), withheld]) }));

    const filtered = await screen.findByRole("region", { name: "Filtered by your constraints (3)" });
    expect(within(filtered).getByRole("link", { name: "Demo posting (invented): Onsite Support Engineer" })).toHaveAttribute("href", `/app/matches/${OTHER_DECISION_ID}`);
    expect(filtered).toHaveTextContent("Location: “Only jobs in the United States.”");
    expect(filtered).toHaveTextContent("Showing the 1 best-ranked of 3."); // the run counts every violator; the ledger keeps the best few

    const unmet = screen.getByRole("region", { name: "A required item is not met (1)" });
    expect(unmet).toHaveTextContent("Owns on-call rotations (Required, Not met)");
    // filtered comes before the rest
    expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toEqual(["Filtered by your constraints (3)", "A required item is not met (1)"]);
  });

  it("lists a job once even when two runs of the day held it back", async () => {
    renderWithApi(<TodayView />, routes({ "GET /v1/decisions": ok([decisionView(), decisionView({ decision_id: "aa" })]) }));
    const group = await screen.findByRole("region", { name: /Filtered by your constraints/ });
    expect(within(group).getAllByRole("listitem")).toHaveLength(1);
  });

  it("says a constraint the person has since removed is gone, instead of guessing it", async () => {
    const removed = decisionView({ constraint: { id: null, kind: "location", statement: null } });
    renderWithApi(<TodayView />, routes({ "GET /v1/decisions": ok([removed]) }));
    expect(await screen.findByText(/a constraint you have since removed/)).toBeInTheDocument();
  });

  it("says so when nothing was held back, and when no run has finished", async () => {
    const first = renderWithApi(<TodayView />, routes({ "GET /v1/decisions": ok([]) }));
    expect(await screen.findByText("Nothing was held back on this day.")).toBeInTheDocument();
    first.unmount();
    renderWithApi(<TodayView />, routes({ "GET /v1/decisions": ok([]), "GET /v1/matches": ok(todayMatches({ matches: [], run: null })) }));
    expect(await screen.findByText("No match run has finished for this day yet.")).toBeInTheDocument();
  });

  it("says what failed, with Retry, when the ledger or the day it belongs to cannot be loaded, instead of loading for ever", async () => {
    const first = renderWithApi(<TodayView />, routes({ "GET /v1/decisions": fail(503, "dependency_unavailable") }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load what was held back.")).toBeInTheDocument();
    first.unmount();

    renderWithApi(<TodayView />, routes({ "GET /v1/matches": fail(500, "internal_error") }));
    expect(await screen.findByText("Could not load what was held back.")).toBeInTheDocument();
    expect(screen.queryByText("Loading what was held back.")).not.toBeInTheDocument();
  });

  it("asks only for the withheld and filtered decisions of the day shown", async () => {
    const { calls } = renderWithApi(<TodayView />, routes());
    await screen.findByRole("region", { name: /Filtered by your constraints/ });
    const request = calls.find((call) => new URL(call.url).pathname === "/v1/decisions")!;
    const query = new URL(request.url).searchParams;
    expect(query.get("outcome")).toBe("withheld,filtered");
    expect(query.get("from")).toBe("2026-11-23");
    expect(query.get("to")).toBe("2026-11-23");
  });

  it("asks for the ledger only when its tab is the one shown", async () => {
    nav.search = "view=matches";
    const { calls } = renderWithApi(<TodayView />, routes());
    await screen.findByRole("article");
    expect(calls.some((call) => new URL(call.url).pathname === "/v1/decisions")).toBe(false);
  });
});

describe("Today: the tabs are URL state", () => {
  it("writes the chosen tab to the address, so it survives a reload and the back button", async () => {
    const user = userEvent.setup();
    renderWithApi(<TodayView />, routes());
    await user.click(await screen.findByRole("tab", { name: "Why not" }));
    expect(nav.push).toHaveBeenCalledWith("/app/today?view=why-not", { scroll: false });
  });

  it.each(["", "nonsense", "DECISIONS", "matches;"])("falls back to the first useful tab for ?view=%j", async (value) => {
    nav.search = `view=${value}`;
    renderWithApi(<TodayView />, routes());
    expect(await screen.findByRole("tab", { name: "Decisions (1)" })).toHaveAttribute("aria-selected", "true");
  });
});
