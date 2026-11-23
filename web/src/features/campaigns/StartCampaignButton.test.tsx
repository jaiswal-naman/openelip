import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { StartCampaignButton } from "./StartCampaignButton";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

describe("StartCampaignButton", () => {
  it("calls nothing until pressed, then posts the job with an Idempotency-Key and opens the campaign", async () => {
    const user = userEvent.setup();
    const posted = vi.fn(async (request: Request) => {
      expect(request.headers.get("Idempotency-Key")).toBeTruthy();
      expect(await request.json()).toEqual({ job_id: 42 });
      return ok({ id: "new-campaign", stage: "researching" }, 201);
    });
    const { calls } = renderWithApi(<StartCampaignButton jobId={42} />, { "POST /v1/campaigns": posted });
    expect(calls).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Start campaign" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/app/campaigns/new-campaign"));
  });

  it("links to the campaign that already exists on a 409", async () => {
    const user = userEvent.setup();
    renderWithApi(<StartCampaignButton jobId={42} />, { "POST /v1/campaigns": fail(409, "conflict", { campaign_id: "old-one" }) });
    await user.click(screen.getByRole("button", { name: "Start campaign" }));
    expect(await screen.findByRole("link", { name: "Open it" })).toHaveAttribute("href", "/app/campaigns/old-one");
  });

  it.each([
    [410, "job_closed", "This job has closed"],
    [422, "violates_constraint", "breaks one of your hard constraints"],
  ])("says what is wrong on a %s", async (status, code, words) => {
    const user = userEvent.setup();
    renderWithApi(<StartCampaignButton jobId={42} />, { "POST /v1/campaigns": fail(status, code) });
    await user.click(screen.getByRole("button", { name: "Start campaign" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(words);
  });
});
