import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { AccountMenu } from "./AccountMenu";
import { authKeys } from "./queries";

const nav = vi.hoisted(() => ({ push: vi.fn(), refresh: vi.fn(), pathname: "/jobs", search: "" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(nav.search),
  useRouter: () => ({ push: nav.push, refresh: nav.refresh, replace: vi.fn() }),
}));

// An invented account
const me = { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "reader@example.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" };
const signedIn = { "GET /v1/me": ok(me) };

beforeEach(() => {
  nav.push.mockReset();
  nav.refresh.mockReset();
  nav.pathname = "/jobs";
  nav.search = "";
});

describe("when nobody is signed in", () => {
  it("offers sign-in and brings you back to this page afterwards", async () => {
    nav.pathname = "/jobs";
    nav.search = "q=backend+python&country=IN";
    renderWithApi(<AccountMenu />, { "GET /v1/me": fail(401, "unauthenticated") });
    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", `/sign-in?next=${encodeURIComponent("/jobs?q=backend+python&country=IN")}`);
  });

  it("does not send you back to the sign-in page you are already on", async () => {
    nav.pathname = "/sign-in";
    renderWithApi(<AccountMenu />, { "GET /v1/me": fail(401, "unauthenticated") });
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });

  it("offers sign-in while the answer is not known, and when the API cannot answer", async () => {
    renderWithApi(<AccountMenu />, { "GET /v1/me": () => new Promise<Response>(() => {}) });
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("when someone is signed in", () => {
  it("shows their email, in the button's name as well as on screen", async () => {
    renderWithApi(<AccountMenu />, signedIn);
    const button = await screen.findByRole("button", { name: "Account: reader@example.test" });
    expect(button).toHaveTextContent("reader@example.test");
    expect(screen.queryByRole("link", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("opens a menu that says who you are and offers Sign out", async () => {
    const user = userEvent.setup();
    renderWithApi(<AccountMenu />, signedIn);
    await user.click(await screen.findByRole("button", { name: "Account: reader@example.test" }));
    const account = await screen.findByRole("menu", { name: "Account: reader@example.test" });
    expect(within(account).getByText(/Signed in as/)).toHaveTextContent("Signed in as reader@example.test");
    expect(within(account).getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });
});

describe("signing out", () => {
  it("calls DELETE with an Idempotency-Key, forgets the account and what it read, and returns to /", async () => {
    const user = userEvent.setup();
    const { calls, queryClient } = renderWithApi(<AccountMenu />, {
      ...signedIn,
      "DELETE /v1/auth/sessions/current": new Response(null, { status: 204 }),
    });
    queryClient.setQueryData(["control", "costs", "month"], { rows: [] }); // something this person read
    await user.click(await screen.findByRole("button", { name: "Account: reader@example.test" }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/"));
    expect(nav.refresh).toHaveBeenCalled();
    const request = calls.find((call) => call.method === "DELETE")!;
    expect(new URL(request.url).pathname).toBe("/v1/auth/sessions/current");
    expect(request.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(queryClient.getQueryData(authKeys.me)).toBeNull();
    expect(queryClient.getQueryData(["control", "costs", "month"])).toBeUndefined();
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("works from the keyboard alone", async () => {
    const user = userEvent.setup();
    renderWithApi(<AccountMenu />, { ...signedIn, "DELETE /v1/auth/sessions/current": new Response(null, { status: 204 }) });
    (await screen.findByRole("button", { name: "Account: reader@example.test" })).focus();
    await user.keyboard("{Enter}");
    await user.keyboard("{Enter}"); // the first item, Sign out, has focus
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/"));
  });

  it("treats a session that was already gone as signed out", async () => {
    const user = userEvent.setup();
    renderWithApi(<AccountMenu />, { ...signedIn, "DELETE /v1/auth/sessions/current": fail(401, "unauthenticated") });
    await user.click(await screen.findByRole("button", { name: "Account: reader@example.test" }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/"));
  });

  it("says so when it failed, stays signed in, and does not leave the page", async () => {
    const user = userEvent.setup();
    renderWithApi(<AccountMenu />, { ...signedIn, "DELETE /v1/auth/sessions/current": fail(500, "internal_error") });
    await user.click(await screen.findByRole("button", { name: "Account: reader@example.test" }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not sign out. You are still signed in. Try again.");
    expect(screen.getByRole("button", { name: "Account: reader@example.test" })).toBeInTheDocument();
    expect(nav.push).not.toHaveBeenCalled();
  });
});
