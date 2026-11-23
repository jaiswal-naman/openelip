import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { ApplicationsSection } from "./ApplicationsSection";

const application = {
  id: "a1",
  job_id: 42,
  job_title: "Platform Engineer",
  company_name: "Initech Demo",
  status: "interview",
  status_changed_at: "2026-11-24T09:00:00Z",
  application_packet_id: null,
  events: [
    { status: "applied", occurred_at: "2026-11-23T09:00:00Z", created_at: "2026-11-23T09:00:01Z" },
    { status: "interview", occurred_at: "2026-11-24T09:00:00Z", created_at: "2026-11-24T09:00:01Z" },
  ],
};

describe("ApplicationsSection", () => {
  it("lists each application with its status pill and its history with times", async () => {
    renderWithApi(<ApplicationsSection />, { "GET /v1/applications": ok([application]) });
    const item = (await screen.findByRole("heading", { name: "Platform Engineer" })).closest("li") as HTMLElement;
    expect(within(item).getByText("Interview")).toBeInTheDocument();
    const history = within(item).getByRole("list", { name: "History of Platform Engineer" });
    expect(within(history).getAllByRole("listitem")).toHaveLength(2);
    expect(history.querySelectorAll("time")).toHaveLength(2);
  });

  it("points an empty list at the matches", async () => {
    renderWithApi(<ApplicationsSection />, { "GET /v1/applications": ok([]) });
    expect(await screen.findByText(/You have not recorded any applications/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See your matches" })).toHaveAttribute("href", "/app/today?view=matches");
  });

  it("records a new status from the menu with the chosen time", async () => {
    const user = userEvent.setup();
    const posted = vi.fn(async (request: Request) => {
      const body = (await request.json()) as { status: string; occurred_at: string };
      expect(body.status).toBe("offer");
      expect(new Date(body.occurred_at).toString()).not.toBe("Invalid Date");
      return ok({ ...application, status: "offer" }, 201);
    });
    renderWithApi(<ApplicationsSection />, { "GET /v1/applications": ok([application]), "POST /v1/applications/a1/status": posted });
    await user.click(await screen.findByRole("button", { name: "Record status for Platform Engineer" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Applied", "Interview", "Offer", "Rejected", "No response"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Offer" }));
    const dialog = await screen.findByRole("dialog", { name: "Record: Offer" });
    expect((within(dialog).getByLabelText("When did it happen?") as HTMLInputElement).value).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    await user.click(within(dialog).getByRole("button", { name: "Record" }));
    await waitFor(() => expect(posted).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it.each([
    [409, "conflict", { state: "same_status" }, "already has that status"],
    [422, "validation_failed", {}, "cannot be dated before the current one"],
  ])("shows a %s inline and keeps the dialog open", async (status, code, details, message) => {
    const user = userEvent.setup();
    renderWithApi(<ApplicationsSection />, { "GET /v1/applications": ok([application]), "POST /v1/applications/a1/status": () => fail(status, code, details) });
    await user.click(await screen.findByRole("button", { name: "Record status for Platform Engineer" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rejected" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Record" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
  });
});
