import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authKeys } from "@/features/auth/queries";
import { account } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { BriefSettings } from "./BriefSettings";
import { BRIEF_EMAIL_TEXT_VERSION } from "./queries";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: nav.replace, push: vi.fn(), refresh: vi.fn() }) }));

beforeEach(() => nav.replace.mockReset());

/** GET /v1/me for a person with this brief schedule and whether the brief email is on. */
function me({ frequency = "daily", weekday = null, email = false, timezone = "Asia/Kolkata" }: { frequency?: string; weekday?: number | null; email?: boolean; timezone?: string } = {}) {
  const base = account();
  const consents = base.consents.map((consent) =>
    consent.purpose === "brief_email" ? { ...consent, granted: email, granted_at: email ? "2026-11-22T00:00:00Z" : null, text_version: email ? BRIEF_EMAIL_TEXT_VERSION : null } : consent,
  );
  return { ...base, timezone, brief: { frequency, weekday }, consents };
}

const patchOf = (calls: Request[]) => calls.find((call) => call.method === "PATCH")!;
const bodyOf = (request: Request) => request.clone().json();

describe("the Brief section of settings", () => {
  it("shows how often the brief comes, its time zone and whether it is emailed", async () => {
    renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me({ frequency: "weekly", weekday: 3, email: true })) });
    expect(await screen.findByRole("heading", { level: 2, name: "Morning brief" })).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "How often" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio").map((radio) => radio.closest("label")?.textContent)).toEqual(["Daily", "Weekly", "Paused"]);
    expect(screen.getByRole("radio", { name: "Weekly" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Day of the week" })).toHaveValue("3");
    expect(screen.getByRole("combobox", { name: "Time zone" })).toHaveValue("Asia/Kolkata");
    expect(screen.getByRole("switch", { name: "Email me the brief" })).toBeChecked();
  });

  it("asks for a weekday only when the brief is weekly", async () => {
    const user = userEvent.setup();
    renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me()) });
    await screen.findByRole("radiogroup", { name: "How often" });
    expect(screen.queryByRole("combobox", { name: "Day of the week" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Weekly" }));
    expect(screen.getByRole("combobox", { name: "Day of the week" })).toHaveValue("1"); // Monday until a day is picked
    await user.click(screen.getByRole("radio", { name: "Paused" }));
    expect(screen.queryByRole("combobox", { name: "Day of the week" })).not.toBeInTheDocument();
    expect(screen.getByText(/While it is paused no brief is made and no email is sent/)).toBeInTheDocument();
  });

  it("saves a weekly brief with its day and its time zone in one request, with an Idempotency-Key, and says it is saved", async () => {
    const user = userEvent.setup();
    const { calls, queryClient } = renderWithApi(<BriefSettings />, {
      "GET /v1/me": ok(me()),
      "PATCH /v1/me": ok(me({ frequency: "weekly", weekday: 3, timezone: "Europe/Paris" })),
    });
    await screen.findByRole("radiogroup", { name: "How often" });
    await user.click(screen.getByRole("radio", { name: "Weekly" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Day of the week" }), "Wednesday");
    await user.selectOptions(screen.getByRole("combobox", { name: "Time zone" }), "Europe/Paris");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    const request = patchOf(calls);
    await expect(bodyOf(request)).resolves.toEqual({ timezone: "Europe/Paris", brief: { frequency: "weekly", weekday: 3 } }); // the email did not change
    expect(request.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(queryClient.getQueryData<{ brief: { frequency: string } }>(authKeys.me)?.brief.frequency).toBe("weekly");
  });

  it("sends no weekday for a daily or a paused brief, which the API would refuse", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me({ frequency: "weekly", weekday: 5 })), "PATCH /v1/me": ok(me({ frequency: "paused" })) });
    await screen.findByRole("radiogroup", { name: "How often" });
    await user.click(screen.getByRole("radio", { name: "Paused" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved.");
    await expect(bodyOf(patchOf(calls))).resolves.toEqual({ timezone: "Asia/Kolkata", brief: { frequency: "paused", weekday: null } });
  });

  it("turns the brief email on with the wording version, and off, and sends the consent only when it changed", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me({ email: false })), "PATCH /v1/me": ok(me({ email: true })) });
    const toggle = await screen.findByRole("switch", { name: "Email me the brief" });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/Off\. Decisions still appear on Today/)).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByText(/On\. Every email has a link that turns this off in one click/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved.");
    await expect(bodyOf(patchOf(calls))).resolves.toMatchObject({ consents: [{ purpose: "brief_email", granted: true, text_version: BRIEF_EMAIL_TEXT_VERSION }] });
  });

  it("withdraws the brief email without a wording version", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me({ email: true })), "PATCH /v1/me": ok(me({ email: false })) });
    await user.click(await screen.findByRole("switch", { name: "Email me the brief" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved.");
    await expect(bodyOf(patchOf(calls))).resolves.toMatchObject({ consents: [{ purpose: "brief_email", granted: false }] });
  });

  it("takes Saved away as soon as a choice changes again, because it described the old choices", async () => {
    const user = userEvent.setup();
    renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me()), "PATCH /v1/me": ok(me()) });
    await screen.findByRole("radiogroup", { name: "How often" });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Paused" }));
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
  });

  it("says nothing was changed, and keeps the choices, when the save fails", async () => {
    const user = userEvent.setup();
    renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me()), "PATCH /v1/me": fail(503, "dependency_unavailable") });
    await screen.findByRole("radiogroup", { name: "How often" });
    await user.click(screen.getByRole("radio", { name: "Paused" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A service this needs is unavailable. Try again shortly. Nothing was changed.");
    expect(screen.getByRole("radio", { name: "Paused" })).toBeChecked();
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
  });

  it("keeps a time zone the browser's list leaves out among the choices", async () => {
    renderWithApi(<BriefSettings />, { "GET /v1/me": ok(me({ timezone: "Etc/GMT-9" })) });
    const zone = await screen.findByRole("combobox", { name: "Time zone" });
    expect(zone).toHaveValue("Etc/GMT-9");
    expect(within(zone).getByRole("option", { name: "Etc/GMT-9" })).toBeInTheDocument();
  });

  it("sends a signed-out person to sign in and back to the Brief settings", async () => {
    renderWithApi(<BriefSettings />, { "GET /v1/me": fail(401, "unauthenticated") });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/sign-in?next=%2Fapp%2Fsettings%3Fsection%3Dbrief"));
  });

  it("says what failed, with Retry, when the account cannot be loaded", async () => {
    renderWithApi(<BriefSettings />, { "GET /v1/me": fail(503, "dependency_unavailable") });
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Could not load your brief settings.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
