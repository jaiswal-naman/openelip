import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { Answers } from "./Answers";
import { Shortlist } from "./Shortlist";
import { clock, INTRO_A, INTRO_B, me, ORG, preview, role, ROLE, shortlistItem } from "./testing";

vi.mock("next/navigation", () => ({
  usePathname: () => "/hire/shortlist",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn(), replace: vi.fn() }),
}));

const quota = { sent: [], received: [], quota: { used: 1, limit: 3, resets_at: "2026-11-30T00:00:00Z" } };
const base = (extra: Record<string, Response | (() => Response | Promise<Response>)> = {}) => ({
  "GET /v1/me": ok(me()),
  [`GET /v1/roles/${ROLE}`]: ok(role({ status: "open" })),
  [`GET /v1/roles/${ROLE}/preview`]: ok(preview),
  [`GET /v1/orgs/${ORG}/answer-clock`]: ok(clock()),
  "GET /v1/signals": ok(quota),
  ...extra,
});

describe("the shortlist", () => {
  it("shows the header with what is owed and each card with name, headline, badge and 'Meets X of Y required'", async () => {
    renderWithApi(<Shortlist roleId={ROLE} />, base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([shortlistItem()]) }));
    expect(await screen.findByRole("heading", { name: "Shortlist, Senior Backend Engineer" })).toBeInTheDocument();
    expect(await screen.findByText("You owe 1 answer")).toBeInTheDocument();
    const card = await screen.findByRole("article", { name: "Candidate Demo Candidate A" });
    expect(within(card).getByText("Backend engineer, payments")).toBeInTheDocument();
    expect(within(card).getByText("accepted 20 Nov")).toBeInTheDocument();
    expect(within(card).getByText("Serious interest from candidate")).toBeInTheDocument();
    expect(within(card).getByText("Meets 1 of 2 required.")).toBeInTheDocument();
    expect(await within(card).findByText("3 days")).toBeInTheDocument();
    expect(within(card).queryByText(/@/)).not.toBeInTheDocument(); // no email before a yes
  });

  it("expands Shared details to the evidence table with verdicts and quotes, and the roles and education facts", async () => {
    const user = userEvent.setup();
    renderWithApi(<Shortlist roleId={ROLE} />, base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([shortlistItem()]) }));
    const card = await screen.findByRole("article", { name: "Candidate Demo Candidate A" });
    await user.click(within(card).getByRole("button", { name: "Shared details" }));
    const table = await within(card).findByRole("grid", { name: "Shared evidence of Demo Candidate A" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]!).getByText("Met")).toBeInTheDocument();
    expect(within(rows[1]!).getByText('"Built and ran Python services"')).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Partly met")).toBeInTheDocument();
    expect(within(rows[3]!).getByText("No evidence")).toBeInTheDocument();
    expect(within(card).getByText("Backend Engineer, Globex, 2021-03 to 2024-06")).toBeInTheDocument();
    expect(within(card).getByText("B.Tech, Example Institute")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Twin/ })).not.toBeInTheDocument();
  });

  it("answers yes with a reason, then shows the email the server returns", async () => {
    const user = userEvent.setup();
    let answered = false;
    const { calls } = renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({
        [`GET /v1/roles/${ROLE}/shortlist`]: () =>
          ok([shortlistItem(answered ? { employer_answer: "yes", employer_answer_reason: "Good fit", email: "candidate@example.test" } : {})]),
        [`POST /v1/introductions/${INTRO_A}/answer`]: () => {
          answered = true;
          return ok({ introduction: {}, candidate_sees: "x", email: "candidate@example.test" });
        },
      }),
    );
    await user.click(await screen.findByRole("button", { name: /Yes, let's talk/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Good fit");
    await user.click(within(dialog).getByRole("button", { name: "Send yes" }));
    expect(await screen.findByText("Email: candidate@example.test")).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await post.json()).toEqual({ answer: "yes", reason: "Good fit" });
    expect(screen.getByText("Answered yes.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Yes, let's talk/ })).not.toBeInTheDocument();
  });

  it("reloads the card on 409 already_resolved", async () => {
    const user = userEvent.setup();
    let resolved = false;
    renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({
        [`GET /v1/roles/${ROLE}/shortlist`]: () => ok([shortlistItem(resolved ? { employer_answer: "no", employer_answer_reason: "Role filled" } : {})]),
        [`POST /v1/introductions/${INTRO_A}/answer`]: () => {
          resolved = true;
          return fail(409, "already_resolved", { resolution: "no" });
        },
      }),
    );
    await user.click(await screen.findByRole("button", { name: /No, with reason/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Skills gap");
    await user.click(within(dialog).getByRole("button", { name: "Send no" }));
    expect(await screen.findByText("Answered no.")).toBeInTheDocument();
    expect(screen.getByText("Role filled")).toBeInTheDocument();
  });

  it("shows the signal button with what is left this week and sends once", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([shortlistItem()]), "POST /v1/signals": ok({ signal: {}, quota: {} }, 201) }),
    );
    const button = await screen.findByRole("button", { name: "Send serious interest (2 of 3 left this week)" });
    await user.click(button);
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(await calls.find((c) => c.method === "POST")!.json()).toEqual({ introduction_id: INTRO_A });
    expect(await screen.findByText("You sent serious interest for this candidate.")).toBeInTheDocument();
  });

  it("disables the signal with the reset date when no quota is left, and after a quota_exhausted answer", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([shortlistItem()]), "GET /v1/signals": ok({ ...quota, quota: { ...quota.quota, used: 3 } }) }),
    );
    const none = await screen.findByRole("button", { name: "Send serious interest (0 of 3 left this week)" });
    expect(none).toBeDisabled();
    expect(screen.getByText("No serious-interest signals left this week. They reset on 30 Nov (UTC).")).toBeInTheDocument();
    unmount();

    renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([shortlistItem()]), "POST /v1/signals": fail(422, "quota_exhausted", { resets_at: "2026-11-30T00:00:00Z" }) }),
    );
    await user.click(await screen.findByRole("button", { name: /Send serious interest/ }));
    expect(await screen.findByText("No serious-interest signals left this week. They reset on 30 Nov (UTC).")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Send serious interest/ })).toBeDisabled();
  });

  it("says nobody has accepted yet, with the number asked from the counts", async () => {
    renderWithApi(
      <Shortlist roleId={ROLE} />,
      base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([]), [`GET /v1/roles/${ROLE}/preview`]: ok({ ...preview, waiting_for_reply: 9, said_yes: 0, said_no_or_expired: 5 }) }),
    );
    expect(await screen.findByText("Nobody has accepted yet. 14 people were asked; each has 7 days to reply.")).toBeInTheDocument();
  });

  it("never invents the number when a count is 'fewer than 5'", async () => {
    renderWithApi(<Shortlist roleId={ROLE} />, base({ [`GET /v1/roles/${ROLE}/shortlist`]: ok([]) }));
    expect(await screen.findByText("Nobody has accepted yet. Some people were asked; each has 7 days to reply.")).toBeInTheDocument();
  });
});

describe("/hire/answers", () => {
  const owedTwo = clock({
    is_asking_paused: true,
    owed: [
      { introduction_id: INTRO_B, role: { id: ROLE, title: "Senior Backend Engineer" }, name: "Demo Candidate B", responded_at: "2026-11-21T10:00:00Z", answer_due_at: "2026-11-28T10:00:00Z", business_days_left: 3, is_overdue: false },
      { introduction_id: INTRO_A, role: { id: ROLE, title: "Senior Backend Engineer" }, name: "Demo Candidate A", responded_at: "2026-11-14T10:00:00Z", answer_due_at: "2026-11-21T10:00:00Z", business_days_left: 0, is_overdue: true },
    ],
  });

  it("lists owed answers overdue first with due time and clock, and the 30-day count", async () => {
    renderWithApi(<Answers />, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}/answer-clock`]: ok(owedTwo) });
    expect(await screen.findByRole("heading", { name: "You owe 2 answers" })).toBeInTheDocument();
    const table = screen.getByRole("grid", { name: "Answers owed" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]!).getByText("Demo Candidate A")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Overdue")).toHaveAttribute("data-tone", "overdue");
    expect(within(rows[1]!).getByText("21 Nov, 10:00")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("3 days")).toHaveAttribute("data-tone", "neutral");
    expect(screen.getByText("Answered in the last 30 days: 4")).toBeInTheDocument();
    expect(within(rows[1]!).getByRole("button", { name: /Yes, let's talk/ })).toBeInTheDocument();
  });

  it("answers from the list", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<Answers />, {
      "GET /v1/me": ok(me()),
      [`GET /v1/orgs/${ORG}/answer-clock`]: ok(owedTwo),
      [`POST /v1/introductions/${INTRO_A}/answer`]: ok({ introduction: {}, candidate_sees: "x", email: null }),
    });
    const table = await screen.findByRole("grid", { name: "Answers owed" });
    await user.click(within(within(table).getAllByRole("row")[1]!).getByRole("button", { name: /No, with reason/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Skills gap");
    await user.click(within(dialog).getByRole("button", { name: "Send no" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
  });

  it("says 'You owe no answers.' when nothing is owed", async () => {
    renderWithApi(<Answers />, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}/answer-clock`]: ok(clock({ owed: [] })) });
    expect(await screen.findByRole("heading", { name: "You owe no answers." })).toBeInTheDocument();
  });

  it("gives phone widths a card per answer, overdue first", async () => {
    renderWithApi(<Answers />, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}/answer-clock`]: ok(owedTwo) });
    const cards = within(await screen.findByRole("list", { name: "Answers owed, as cards" })).getAllByRole("listitem");
    expect(cards[0]).toHaveTextContent("Demo Candidate A");
    expect(cards[0]).toHaveTextContent("Overdue");
  });
});
