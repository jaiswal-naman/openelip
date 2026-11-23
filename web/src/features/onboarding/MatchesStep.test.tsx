import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { matchView, todayMatches } from "@/features/matches/testing";
import { account } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { MatchesStep } from "./MatchesStep";

const withModel = account({}, { capabilities: ["match.evidence.score"] });
const ZERO = "Nothing in the current index meets your constraints well enough yet. Your first brief arrives tomorrow by 07:00.";
const callsTo = (calls: Request[], path: string) => calls.filter((call) => new URL(call.url).pathname === path);

/** The two calls of this step: saving the time zone, and asking for today's matches. */
function routes(over: Record<string, Response | (() => Response)> = {}) {
  return {
    "GET /v1/me": ok(withModel),
    "PATCH /v1/me": ok(withModel),
    "GET /v1/matches": ok(todayMatches()),
    ...over,
  };
}

describe("onboarding step 6", () => {
  it("says when the brief arrives and in which time zone, detected from the browser, and asks before it looks for anything", async () => {
    const { calls } = renderWithApi(<MatchesStep />, routes());
    expect(await screen.findByText(/Your brief arrives by 07:00 in/)).toHaveTextContent("Your brief arrives by 07:00 in UTC."); // the test runner's zone
    expect(screen.getByRole("button", { name: "Change" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: "Confirm and look for my first matches" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your first matches" })).not.toBeInTheDocument();
    expect(callsTo(calls, "/v1/matches")).toHaveLength(0); // nothing is asked for until the zone is confirmed
  });

  it("lets the person change the zone, saves the chosen one on confirm, and then shows the first matches", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<MatchesStep />, routes());
    await user.click(await screen.findByRole("button", { name: "Change" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Time zone" }), "Europe/Paris");
    expect(screen.getByText(/Your brief arrives by 07:00 in/)).toHaveTextContent("in Europe/Paris.");
    await user.click(screen.getByRole("button", { name: "Confirm and look for my first matches" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Your first matches" })).toBeInTheDocument();
    const patch = calls.find((call) => call.method === "PATCH")!;
    await expect(patch.clone().json()).resolves.toEqual({ timezone: "Europe/Paris" });
    expect(patch.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findAllByRole("article")).toHaveLength(1);
  });

  it("shows the same cards as Today, linking to each match's evidence, and leads on to Today", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchesStep />, routes());
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    const card = await screen.findByRole("article", { name: "Demo posting (invented): Quillscript Platform Engineer" });
    expect(within(card).getByRole("link", { name: /Quillscript Platform Engineer/ })).toHaveAttribute("href", expect.stringMatching(/^\/app\/matches\/[0-9a-f-]{36}$/));
    expect(within(card).getByText("Meets 2 of 2 required. 1 preferred item has no evidence.")).toBeInTheDocument();
    // the cards sit under the step's own "Your first matches" heading, so their titles are one level below it
    expect(within(card).getByRole("heading", { level: 3 })).toHaveTextContent("Demo posting (invented): Quillscript Platform Engineer");
    expect(screen.getByRole("link", { name: "Go to Today" })).toHaveAttribute("href", "/app/today");
  });

  it("shows at most five, the most a day can hold", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 7 }, (_, index) =>
      matchView({ decision_id: `7a1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c${String(index).padStart(2, "0")}`, rank_position: index + 1 }),
    );
    renderWithApi(<MatchesStep />, routes({ "GET /v1/matches": ok(todayMatches({ matches: many })) }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    expect(await screen.findAllByRole("article")).toHaveLength(5);
  });

  it("says zero plainly, in the words of the design, when nothing qualifies", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchesStep />, routes({ "GET /v1/matches": ok(todayMatches({ matches: [] })) }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    expect(await screen.findByText(ZERO)).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("says requirement tables need a model key, not that nothing qualified, when this server has no model", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchesStep />, routes({ "GET /v1/me": ok(account()), "PATCH /v1/me": ok(account()), "GET /v1/matches": ok(todayMatches({ matches: [] })) }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    expect(await screen.findByText(/Requirement tables need a model key/)).toBeInTheDocument();
    expect(screen.queryByText(ZERO)).not.toBeInTheDocument();
  });

  it("says nothing was saved, and does not look for matches, when the time zone cannot be saved", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<MatchesStep />, routes({ "PATCH /v1/me": fail(503, "dependency_unavailable") }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your time zone was not saved.");
    expect(callsTo(calls, "/v1/matches")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Confirm and look for my first matches" })).toBeInTheDocument();
  });

  it("says what failed, with Retry, when the matches cannot be loaded", async () => {
    const user = userEvent.setup();
    renderWithApi(<MatchesStep />, routes({ "GET /v1/matches": fail(500, "internal_error") }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load your first matches.")).toBeInTheDocument();
  });
});

describe("looking for the first run", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks again every few seconds until a run has finished for the day, then shows what it found", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    let asked = 0;
    renderWithApi(
      <MatchesStep />,
      routes({
        "GET /v1/matches": (() => {
          asked += 1;
          return asked < 3 ? ok(todayMatches({ matches: [], run: null })) : ok(todayMatches());
        }) as unknown as Response,
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    expect(await screen.findByText("Looking for your first matches. This can take a minute.")).toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(5_000);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await screen.findByRole("article", { name: /Quillscript Platform Engineer/ })).toBeInTheDocument();
    expect(screen.queryByText("Looking for your first matches. This can take a minute.")).not.toBeInTheDocument();
    expect(asked).toBe(3);

    await vi.advanceTimersByTimeAsync(30_000); // a finished run is not asked about again
    expect(asked).toBe(3);
  });

  it("stops looking after two minutes and says where the answer will be", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    renderWithApi(<MatchesStep />, routes({ "GET /v1/matches": (() => ok(todayMatches({ matches: [], run: null }))) as unknown as Response }));
    await user.click(await screen.findByRole("button", { name: "Confirm and look for my first matches" }));
    for (let step = 0; step < 25; step += 1) await vi.advanceTimersByTimeAsync(5_000);
    await waitFor(() => expect(screen.getByText(/Your first run has not finished yet/)).toBeInTheDocument());
    expect(screen.queryByText("Looking for your first matches. This can take a minute.")).not.toBeInTheDocument();
  });
});
