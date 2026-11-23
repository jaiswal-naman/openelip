import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { CampaignView } from "./CampaignView";
import { detail } from "./testing";

const replace = vi.fn();
let search = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/app/campaigns/c1",
  useSearchParams: () => new URLSearchParams(search),
}));

const routes = (campaign = detail(), extra: Record<string, unknown> = {}) => ({
  "GET /v1/campaigns/c1": ok(campaign),
  "GET /v1/applications": ok([]),
  "GET /v1/control/recorder": ok([]),
  ...extra,
});

describe("CampaignView header and controls", () => {
  it("shows company, title, stage and the cost against the budget", async () => {
    search = "";
    renderWithApi(<CampaignView id="c1" />, routes() as never);
    expect(await screen.findByRole("heading", { name: "Platform Engineer", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Globex Demo")).toBeInTheDocument();
    expect(screen.getByText("Cost so far USD 0.12 of USD 0.75 budget")).toBeInTheDocument();
  });

  it("pauses a running campaign, and resumes a paused one", async () => {
    search = "";
    const user = userEvent.setup();
    const paused = vi.fn(() => ok({ id: "c1" }));
    renderWithApi(<CampaignView id="c1" />, routes(detail(), { "POST /v1/campaigns/c1/pause": paused }) as never);
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(paused).toHaveBeenCalled());
  });

  it("lists what stops in the cancel confirmation and cancels only after the button", async () => {
    search = "";
    const user = userEvent.setup();
    const cancelled = vi.fn(async (request: Request) => {
      expect(await request.text()).toBe("");
      return ok({ id: "c1", stage: "closed", closed_reason: "cancelled" });
    });
    renderWithApi(<CampaignView id="c1" />, routes(detail(), { "POST /v1/campaigns/c1/cancel": cancelled }) as never);
    await user.click(await screen.findByRole("button", { name: "Cancel..." }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Every approval waiting for you expires/)).toBeInTheDocument();
    expect(within(dialog).getByText(/cannot be undone/)).toBeInTheDocument();
    expect(cancelled).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel the campaign" }));
    await waitFor(() => expect(cancelled).toHaveBeenCalledTimes(1));
  });

  it("asks for a reason only at the offer stage", async () => {
    search = "";
    const user = userEvent.setup();
    renderWithApi(<CampaignView id="c1" />, routes(detail({ stage: "offer" })) as never);
    await user.click(await screen.findByRole("button", { name: "Cancel..." }));
    expect(await screen.findByText("What happened to the offer?")).toBeInTheDocument();
  });

  it("explains a budget pause, raises the budget with PATCH and then resumes", async () => {
    search = "";
    const user = userEvent.setup();
    const order: string[] = [];
    renderWithApi(
      <CampaignView id="c1" />,
      routes(detail({ paused_at: "2026-11-23T09:00:00Z", pause_reason: "budget_exhausted", spent_usd: "0.750000" }), {
        "PATCH /v1/campaigns/c1": async (request: Request) => {
          order.push(`patch ${JSON.stringify(await request.json())}`);
          return ok({ id: "c1" });
        },
        "POST /v1/campaigns/c1/resume": () => {
          order.push("resume");
          return ok({ id: "c1" });
        },
      }) as never,
    );
    expect(await screen.findByText(/Paused: this campaign reached its USD 0.75 budget\. Raise the budget and resume, or cancel\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Raise budget and resume" }));
    await waitFor(() => expect(order).toHaveLength(2));
    expect(order[0]).toMatch(/^patch \{"budget_usd":"1\.25"\}$/);
    expect(order[1]).toBe("resume");
  });

  it("shows the message of a refused resume", async () => {
    search = "";
    const user = userEvent.setup();
    renderWithApi(
      <CampaignView id="c1" />,
      routes(detail({ paused_at: "2026-11-23T09:00:00Z", pause_reason: "candidate", pause_text: "You paused this campaign." }), { "POST /v1/campaigns/c1/resume": fail(409, "budget_exhausted") }) as never,
    );
    await user.click(await screen.findByRole("button", { name: "Resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This campaign reached its budget");
  });

  it("quotes the scam signals as sentences with Resume anyway and Cancel", async () => {
    search = "";
    renderWithApi(
      <CampaignView id="c1" />,
      routes(detail({ paused_at: "2026-11-23T09:00:00Z", pause_reason: "scam_suspected", scam_signals: [{ kind: "fee_request", quote: "Pay a small fee to start." }] })) as never,
    );
    expect(await screen.findByText(/It asks you to pay a fee or buy something\./)).toHaveTextContent("Pay a small fee to start.");
    expect(screen.getByRole("button", { name: "Resume anyway" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel..." })).toBeInTheDocument();
  });

  it("hides the Packet and Follow-ups tabs for an introduction campaign and shows the employer's clock", async () => {
    search = "";
    renderWithApi(
      <CampaignView id="c1" />,
      routes(detail({ from_introduction: true, stage: "applied", introduction: { id: "i1", employer_answer: null, answer_due_at: "2026-11-28T09:00:00Z" } })) as never,
    );
    await screen.findByRole("heading", { name: "Platform Engineer", level: 1 });
    expect(screen.queryByRole("tab", { name: "Packet" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Follow-ups" })).not.toBeInTheDocument();
    expect(screen.getByText(/The employer answers by/)).toBeInTheDocument();
  });

  it("says the campaign does not exist on a 404", async () => {
    search = "";
    renderWithApi(<CampaignView id="c1" />, { "GET /v1/campaigns/c1": fail(404, "not_found"), "GET /v1/applications": ok([]) });
    expect(await screen.findByText(/does not exist, or it is not yours/)).toBeInTheDocument();
  });
});

describe("CampaignView tabs", () => {
  it("shows the dossier as text with citation chips that only link to the stored source", async () => {
    search = "tab=dossier";
    const claims = [
      {
        text: '<img src=x onerror=alert(1)> Globex builds <b>ledgers</b>',
        topic: "product",
        sources: [{ url: "https://www.globex.example/about", quote: "Globex builds ledgers", retrieved_at: "2026-11-23T09:00:00Z" }],
      },
      { text: "Bad link claim", topic: "size", sources: [{ url: "javascript:alert(1)", quote: "q", retrieved_at: "2026-11-23T09:00:00Z" }] },
    ];
    renderWithApi(<CampaignView id="c1" />, routes(detail({ dossier: { id: "d", version: 1, incomplete: true, claims, missing_topics: ["funding"] } })) as never);
    const chip = await screen.findByRole("link", { name: /Source 1: open the source page/ });
    expect(chip).toHaveAttribute("href", "https://www.globex.example/about");
    expect(chip).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
    expect(screen.queryByRole("link", { name: /Source 2/ })).not.toBeInTheDocument();
    expect(screen.getByText(/This dossier is incomplete/)).toBeInTheDocument();
    expect(screen.getByText(/Not found on the company's pages: Funding/)).toBeInTheDocument();
  });

  it("shows crew steps with role, status, the wait date and the to-do", async () => {
    search = "tab=crew";
    renderWithApi(
      <CampaignView id="c1" />,
      routes(
        detail({
          steps: [
            { step_key: "research", agent_role: "researcher", status: "done", due_at: null, error_code: null, todo: null, text: "Company dossier ready." },
            { step_key: "follow_up:1", agent_role: "chaser", status: "waiting", due_at: "2026-12-01T09:00:00Z", error_code: null, todo: "add_follow_up_address", text: "Add the hiring contact's email to send a follow-up." },
          ],
        }),
      ) as never,
    );
    const list = await screen.findByRole("list", { name: "Crew steps" });
    expect(within(list).getByText("Researcher")).toBeInTheDocument();
    expect(within(list).getByText("Done")).toBeInTheDocument();
    expect(within(list).getByText(/Waiting until/)).toBeInTheDocument();
    expect(within(list).getByText(/To do:/)).toBeInTheDocument();
  });

  it("shows prep questions with the gap framing, Ask them and the removed count", async () => {
    search = "tab=prep";
    renderWithApi(
      <CampaignView id="c1" />,
      routes(
        detail({
          prep: {
            questions: [{ text: "Tell us about Rust.", requirement_id: 1, fact_ids: [], gap: true, framing: "You have no Rust yet; say what you would learn first." }],
            ask_them: [{ text: "How is the team split?", claim_indexes: [0] }],
            removed_by_reason: { not_entailed: 2 },
          },
        }),
      ) as never,
    );
    expect(await screen.findByText("Tell us about Rust.")).toBeInTheDocument();
    expect(screen.getByText(/You have no Rust yet/)).toBeInTheDocument();
    expect(screen.getByText("How is the team split?")).toBeInTheDocument();
    expect(screen.getByText(/2 items were removed/)).toBeInTheDocument();
  });

  it("lists the timeline newest first, with Blocked rows and their reasons", async () => {
    search = "tab=timeline";
    const rows = [
      { id: 1, run_id: "r", seq: 1, created_at: "2026-11-23T09:00:00Z", agent_role: "researcher", kind: "tool_call", action_type: null, status: "recorded", summary: "Read the careers page", outcome_code: null, cost_usd: "0.000000", evidence_count: 0, checks: [], approval: null, decided_at: null, executed_at: null },
      { id: 2, run_id: "r", seq: 2, created_at: "2026-11-23T10:00:00Z", agent_role: "chaser", kind: "outside_action", action_type: "send_follow_up", status: "blocked", summary: "Follow-up 2", outcome_code: "dial_never", cost_usd: "0.000000", evidence_count: 0, checks: [], approval: null, decided_at: null, executed_at: null },
    ];
    renderWithApi(<CampaignView id="c1" />, routes(detail(), { "GET /v1/control/recorder": ok(rows) }) as never);
    const list = await screen.findByRole("list", { name: "Timeline, newest first" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Blocked");
    expect(items[0]).toHaveTextContent("You set this kind of action to never");
    expect(items[1]).toHaveTextContent("Read the careers page");
    expect(within(items[0]).getByRole("link")).toHaveAttribute("href", "/control/recorder?campaign_id=c1&step=2");
  });
});
