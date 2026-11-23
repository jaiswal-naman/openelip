import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { CampaignCard } from "./CampaignCard";
import { newReply, reduceReply } from "./reply";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const offer = { job_id: 42, title: "Platform Engineer", company_name: "Globex Demo", campaign_id: null };

describe("chat campaign card", () => {
  it("collects campaign_proposal events once per job", () => {
    let reply = reduceReply(newReply(), "campaign_proposal", offer);
    reply = reduceReply(reply, "campaign_proposal", offer);
    expect(reply.campaignProposals).toEqual([offer]);
    expect(reduceReply(reply, "message.snapshot", { text: "", citations: [], proposals: [], campaign_proposals: [] }).campaignProposals).toEqual([]);
  });

  it("calls nothing before a click, then starts the campaign", async () => {
    const user = userEvent.setup();
    const posted = vi.fn(() => ok({ id: "new", stage: "researching" }, 201));
    const { calls } = renderWithApi(<CampaignCard offer={offer} />, { "POST /v1/campaigns": posted });
    expect(calls).toHaveLength(0);
    expect(screen.getByText("Platform Engineer at Globex Demo")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start campaign" }));
    expect(posted).toHaveBeenCalledTimes(1);
  });

  it("offers Open campaign when one exists", () => {
    renderWithApi(<CampaignCard offer={{ ...offer, campaign_id: "c9" }} />);
    expect(screen.getByRole("link", { name: "Open campaign" })).toHaveAttribute("href", "/app/campaigns/c9");
    expect(screen.queryByRole("button", { name: "Start campaign" })).not.toBeInTheDocument();
  });
});
