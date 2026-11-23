import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { RecorderView } from "./RecorderView";

const replace = vi.fn();
let search = "step=2";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/control/recorder",
  useSearchParams: () => new URLSearchParams(search),
}));

const row = (id: number, over: Record<string, unknown> = {}) => ({
  id, run_id: "r", seq: id, created_at: "2026-11-23T09:00:00Z", agent_role: "chaser", kind: "outside_action", action_type: "send_follow_up",
  status: "scheduled", summary: `Step ${id}`, outcome_code: null, cost_usd: "0.001000", evidence_count: 1, checks: [], approval: null, decided_at: null, executed_at: null, ...over,
});
const entry = {
  id: 2, run_id: "r", seq: 2, created_at: "2026-11-23T09:00:00Z", agent_role: "chaser", campaign_id: "c1", kind: "outside_action", action_type: "send_follow_up", status: "blocked",
  summary: "Follow-up 2", inputs: {}, evidence: [], payload: { to: "hiring@globex.example", subject: "Hello", body: "Draft body." }, policy_decision: "deny", policy_version: "p1",
  undo_until: null, executed_at: null, outcome_code: "dial_never", cost_usd: "0.002000",
  checks: [{ id: 1, check: "scope", passed: false, reason_code: "dial_never", details: {}, checked_at: "2026-11-23T09:00:00Z" }, { id: 2, check: "budget", passed: true, reason_code: null, details: {}, checked_at: "2026-11-23T09:00:00Z" }],
  approval: null, model_calls: [{ id: 1, task: "chaser.draft", prompt_version: "v1", outcome: "ok", cost_microusd: 2000 }],
};

describe("RecorderView", () => {
  it("lists steps with a filled Blocked pill and shows the selected step in full", async () => {
    search = "step=2";
    renderWithApi(<RecorderView campaignId="c1" />, {
      "GET /v1/control/recorder": ok([row(1, { kind: "model_call", agent_role: "researcher" }), row(2, { status: "blocked", outcome_code: "dial_never" }), row(3)]),
      "GET /v1/control/recorder/2": ok(entry),
    });
    const list = await screen.findByRole("navigation", { name: "Steps" });
    expect(within(list).getAllByText("Blocked")).toHaveLength(1);
    expect(within(list).getByText(/You set this kind of action to never/)).toBeInTheDocument();
    const selected = await screen.findByRole("article", { name: "Selected step" });
    expect(within(selected).getByText("hiring@globex.example (you typed)")).toBeInTheDocument();
    expect(within(selected).getByText("Not allowed")).toBeInTheDocument();
    expect(within(selected).getByText("USD 0.002000")).toBeInTheDocument();
    // The Guardian's checks are a small table: the scope check really covers the recipient, the employer and restricted data
    const checks = within(selected).getByRole("table", { name: "Guardian checks and their verdicts" });
    expect(within(checks).getByRole("row", { name: /Recipient, employer and data limits/ })).toHaveTextContent("Blocked");
    expect(within(checks).getByRole("row", { name: /Budget/ })).toHaveTextContent("Passed");
    expect(within(selected).getByText("Show the draft")).toBeInTheDocument();
  });

  it("moves with Next, Previous, First and Last in the address", async () => {
    search = "step=2";
    const user = userEvent.setup();
    renderWithApi(<RecorderView campaignId="c1" />, {
      "GET /v1/control/recorder": ok([row(1), row(2), row(3)]),
      "GET /v1/control/recorder/2": ok(entry),
    });
    await screen.findByText("Step 2 of 3");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(replace).toHaveBeenLastCalledWith("/control/recorder?step=3", { scroll: false });
    await user.click(screen.getByRole("button", { name: "First" }));
    expect(replace).toHaveBeenLastCalledWith("/control/recorder?step=1", { scroll: false });
  });
});
