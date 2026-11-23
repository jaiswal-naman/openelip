import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { AutonomySection } from "./AutonomySection";

const autonomy = (over: Record<string, unknown> = {}) => ({
  version: "v1hash",
  actions: {
    submit_application: { level: "ask_first", allowed_levels: ["ask_first"] },
    send_follow_up: { level: "ask_first", allowed_levels: ["ask_first", "never"] },
    twin_answer: { level: "ask_first", allowed_levels: ["ask_first", "tell_me_after", "never"] },
  },
  profile_sharing: "Only when you accept an introduction.",
  undo_window_minutes: 10,
  campaign_budget_default_usd: "0.75",
  ...over,
});

describe("AutonomySection", () => {
  it("renders locked rows as text with the reason, and offers only allowed levels", async () => {
    renderWithApi(<AutonomySection />, { "GET /v1/autonomy": ok(autonomy()) });
    expect(await screen.findByText(/The system never submits an application for you/)).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Submit an application" })).not.toBeInTheDocument();
    const followUps = screen.getByRole("radiogroup", { name: "Send a follow-up" });
    expect(followUps.querySelectorAll('input[type="radio"]')).toHaveLength(2);
    expect(screen.getByText(/Tell me after is not offered/)).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Answer an employer's question" }).querySelectorAll('input[type="radio"]')).toHaveLength(3);
    expect(screen.getByText("Only when you accept an introduction")).toBeInTheDocument();
    expect(screen.getByText(/Default budget for a new campaign: USD 0.75/)).toBeInTheDocument();
  });

  it("saves a change at once with If-Match and says it applies to new actions", async () => {
    const user = userEvent.setup();
    const put = vi.fn(async (request: Request) => {
      expect(request.headers.get("If-Match")).toBe('"v1hash"');
      expect(await request.json()).toEqual({ actions: { send_follow_up: { level: "never" } }, undo_window_minutes: null });
      return ok(autonomy({ version: "v2", actions: { ...autonomy().actions, send_follow_up: { level: "never", allowed_levels: ["ask_first", "never"] } } }));
    });
    renderWithApi(<AutonomySection />, { "GET /v1/autonomy": ok(autonomy()), "PUT /v1/autonomy": put });
    await user.click(within(await screen.findByRole("radiogroup", { name: "Send a follow-up" })).getByRole("radio", { name: "Never" }));
    expect(await screen.findByText("Saved. Applies to new actions from now.")).toBeInTheDocument();
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("puts the row back and prints the reason on a 422", async () => {
    const user = userEvent.setup();
    renderWithApi(<AutonomySection />, {
      "GET /v1/autonomy": ok(autonomy()),
      "PUT /v1/autonomy": fail(422, "autonomy_not_allowed", { action_type: "send_follow_up", requested_level: "never", allowed_levels: ["ask_first"] }),
    });
    await user.click(within(await screen.findByRole("radiogroup", { name: "Send a follow-up" })).getByRole("radio", { name: "Never" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That level is not allowed for this kind of action. Your choice was put back.");
    expect(within(screen.getByRole("radiogroup", { name: "Send a follow-up" })).getByRole("radio", { name: "Ask me first", checked: true })).toBeInTheDocument();
  });

  it("reloads the latest on a 412", async () => {
    const user = userEvent.setup();
    const reads = vi.fn(() => ok(autonomy()));
    renderWithApi(<AutonomySection />, { "GET /v1/autonomy": reads, "PUT /v1/autonomy": fail(412, "version_conflict") });
    await user.click(within(await screen.findByRole("radiogroup", { name: "Send a follow-up" })).getByRole("radio", { name: "Never" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed in another tab");
    await waitFor(() => expect(reads.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("saves the undo window", async () => {
    const user = userEvent.setup();
    const put = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ actions: null, undo_window_minutes: 30 });
      return ok(autonomy({ undo_window_minutes: 30 }));
    });
    renderWithApi(<AutonomySection />, { "GET /v1/autonomy": ok(autonomy()), "PUT /v1/autonomy": put });
    const field = await screen.findByLabelText(/Undo window for follow-ups/);
    await user.clear(field);
    await user.type(field, "30");
    await user.click(screen.getByRole("button", { name: "Save undo window" }));
    expect(await screen.findByText("Saved. Applies to new actions from now.")).toBeInTheDocument();
  });
});
