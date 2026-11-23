import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { BriefCampaignItems, campaignItems, matchDecisions } from "./BriefCampaignItems";

const approval = { kind: "approval" as const, campaign_id: "c1", text: "Globex Demo: your application packet is ready for review.", link: "/app/campaigns/c1", status: "pending", approval_id: "ap1", agent_action_id: null };
const activity = { kind: "activity" as const, campaign_id: "c1", text: "Globex Demo: a follow-up was sent.", link: "/app/campaigns/c1", status: "executed", agent_action_id: 5 };

describe("BriefCampaignItems", () => {
  it("renders approval and activity items with a link to the campaign", () => {
    renderWithApi(<BriefCampaignItems items={[approval, activity]} undoable={[]} />);
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/app/campaigns/c1");
    expect(screen.getByText("Globex Demo: a follow-up was sent.")).toBeInTheDocument();
  });

  it("renders a receipt line with Undo for a scheduled follow-up", async () => {
    const user = userEvent.setup();
    const undone = vi.fn(() => ok({ action: { id: 9, status: "cancelled" }, outbox_cancelled: 1 }));
    const soon = new Date(Date.now() + 5 * 60_000).toISOString();
    renderWithApi(<BriefCampaignItems items={[]} undoable={[{ action_id: 9, campaign_id: "c1", company_name: "Globex Demo", undo_until: soon }]} />, { "POST /v1/control/actions/9/undo": undone });
    expect(screen.getByText(/A follow-up to Globex Demo is scheduled and sends at/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(undone).toHaveBeenCalled());
    expect(await screen.findByText(/Undone: the follow-up to Globex Demo will not be sent/)).toBeInTheDocument();
  });

  it("splits the brief's decisions by kind", () => {
    const intro = { kind: "introduction" as const, introduction_id: "i", organization_name: "o", role_title: "r", expires_at: "2026-11-30T00:00:00Z" };
    const decisions = [intro, approval, activity] as never;
    expect(campaignItems(decisions)).toHaveLength(2);
    expect(matchDecisions(decisions)).toHaveLength(0);
  });
});
