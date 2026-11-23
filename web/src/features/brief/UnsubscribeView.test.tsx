import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { authKeys } from "@/features/auth/queries";
import { account } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { BRIEF_EMAIL_TEXT_VERSION } from "./queries";
import { UnsubscribeView } from "./UnsubscribeView";

const TOKEN = "dGVzdC10b2tlbg.abcdef";
const UNSUBSCRIBE = "POST /v1/briefs/unsubscribe";
const STOPPED = "You will not receive the morning brief by email. Decisions still appear in the app.";

const signedOut = { "GET /v1/me": fail(401, "unauthenticated") };
const signedIn = { "GET /v1/me": ok(account()) };
const unsubscribeCalls = (calls: Request[]) => calls.filter((call) => new URL(call.url).pathname === "/v1/briefs/unsubscribe");

describe("the unsubscribe page", () => {
  it("sends nothing when it loads, so a mail system that opens the link to scan it unsubscribes no one (threat T-04-32)", async () => {
    const { calls } = renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }) });
    expect(screen.getByRole("heading", { level: 1, name: "Stop the morning brief email" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing has changed yet/)).toBeInTheDocument();
    await waitFor(() => expect(calls.length).toBeGreaterThan(0)); // the page asks who is signed in, nothing more
    expect(unsubscribeCalls(calls)).toHaveLength(0);
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("posts the token and the one-click form value when the button is pressed, with no cookie (threat T-04-33)", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }) });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    expect(await screen.findByText(STOPPED)).toBeInTheDocument();

    const [request] = unsubscribeCalls(calls);
    expect(request.method).toBe("POST");
    expect(new URL(request.url).searchParams.get("t")).toBe(TOKEN);
    expect(request.headers.get("Content-Type")).toMatch(/^application\/x-www-form-urlencoded/);
    expect(await request.clone().text()).toBe("List-Unsubscribe=One-Click");
    expect(request.credentials).toBe("omit");
  });

  it("moves the focus to the result, because the button that was pressed is gone", async () => {
    const user = userEvent.setup();
    renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }) });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Morning brief email stopped" })).toHaveFocus();
  });

  it("offers a signed-out person sign-in, then the Brief settings, for Undo and for weekly", async () => {
    const user = userEvent.setup();
    renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }) });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    await screen.findByText(STOPPED);
    const next = "/sign-in?next=%2Fapp%2Fsettings%3Fsection%3Dbrief";
    expect(screen.getByRole("link", { name: "Undo" })).toHaveAttribute("href", next);
    expect(screen.getByRole("link", { name: "Choose weekly instead" })).toHaveAttribute("href", next);
  });

  it("turns the email back on for a signed-in person who presses Undo", async () => {
    const user = userEvent.setup();
    const { calls, queryClient } = renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedIn, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }), "PATCH /v1/me": ok(account()) });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    await user.click(await screen.findByRole("button", { name: "Undo" }));

    expect(await screen.findByText(/The morning brief email is on again/)).toBeInTheDocument();
    const patch = calls.find((call) => call.method === "PATCH")!;
    await expect(patch.clone().json()).resolves.toEqual({ consents: [{ purpose: "brief_email", granted: true, text_version: BRIEF_EMAIL_TEXT_VERSION }] });
    expect(queryClient.getQueryData(authKeys.me)).not.toBeNull();
  });

  it("chooses a weekly brief, on Mondays, with the email on, for a signed-in person", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedIn, [UNSUBSCRIBE]: ok({ is_brief_email_on: false }), "PATCH /v1/me": ok(account()) });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    await user.click(await screen.findByRole("button", { name: "Choose weekly instead" }));

    expect(await screen.findByText(/once a week, on Mondays/)).toBeInTheDocument();
    const patch = calls.find((call) => call.method === "PATCH")!;
    await expect(patch.clone().json()).resolves.toEqual({
      brief: { frequency: "weekly", weekday: 1 },
      consents: [{ purpose: "brief_email", granted: true, text_version: BRIEF_EMAIL_TEXT_VERSION }],
    });
  });

  it("says the link has expired, and offers the settings, when the server does not know the token", async () => {
    const user = userEvent.setup();
    renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: fail(404, "not_found") });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    expect(await screen.findByRole("heading", { level: 1, name: "This link has expired" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the brief settings" })).toHaveAttribute("href", "/app/settings?section=brief");
    expect(screen.queryByText(STOPPED)).not.toBeInTheDocument();
  });

  it("says the link has expired without asking the server when the address has no token", async () => {
    const { calls } = renderWithApi(<UnsubscribeView token={null} />, signedOut);
    expect(screen.getByRole("heading", { level: 1, name: "This link has expired" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop the morning brief email" })).not.toBeInTheDocument();
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(unsubscribeCalls(calls)).toHaveLength(0);
  });

  it("says what went wrong and keeps the button when the server could not be reached, so it can be tried again", async () => {
    const user = userEvent.setup();
    renderWithApi(<UnsubscribeView token={TOKEN} />, { ...signedOut, [UNSUBSCRIBE]: fail(503, "dependency_unavailable") });
    await user.click(screen.getByRole("button", { name: "Stop the morning brief email" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A service this needs is unavailable. Try again shortly.");
    expect(screen.getByRole("button", { name: "Stop the morning brief email" })).toBeInTheDocument();
    expect(screen.queryByText(STOPPED)).not.toBeInTheDocument();
  });
});
