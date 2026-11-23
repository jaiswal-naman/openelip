import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { AccountMenu } from "@/features/auth/AccountMenu";
import { SignInForm } from "@/features/auth/SignInForm";
import { messageFor } from "@/lib/labels";
import { ApiFailure } from "@/lib/api/errors";
import { clock, invitation, me, ORG } from "./testing";
import { CreateOrganization } from "./CreateOrganization";
import { HiringShell } from "./HiringShell";
import { Invitations } from "./Invitations";
import { RolesList } from "./RolesList";
import { Team } from "./Team";

const nav = vi.hoisted(() => ({ push: vi.fn(), refresh: vi.fn(), replace: vi.fn(), pathname: "/hire/roles" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: nav.push, refresh: nav.refresh, replace: nav.replace }),
}));

beforeEach(() => {
  Object.values(nav).forEach((fn) => typeof fn === "function" && fn.mockReset?.());
});

describe("the entry points", () => {
  it("shows Hiring desk in the account menu only to an organization member", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithApi(<AccountMenu />, { "GET /v1/me": ok(me()) });
    await user.click(await screen.findByRole("button", { name: /Account:/ }));
    const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Hiring desk" });
    expect(item).toHaveAttribute("href", "/hire/roles");
    unmount();

    renderWithApi(<AccountMenu />, { "GET /v1/me": ok(me({ organizations: [] })) });
    await user.click(await screen.findByRole("button", { name: /Account:/ }));
    expect(within(await screen.findByRole("menu")).queryByRole("menuitem", { name: "Hiring desk" })).not.toBeInTheDocument();
  });

  async function signIn(next: string, routes: Record<string, Response>) {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next={next} />, {
      "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }),
      "POST /v1/auth/sessions": ok(me()),
      ...routes,
    });
    await user.type(screen.getByRole("textbox", { name: /email/i }), "owner@example.test");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await user.type(await screen.findByRole("textbox", { name: /code/i }), "123456");
    await user.click(screen.getByRole("button", { name: /sign in|continue|verify/i }));
  }

  it("sends a member with no confirmed record to /hire/roles", async () => {
    await signIn("/jobs", { "GET /v1/me/record": ok({ facts: [], constraints: [], preferences: [], unconfirmed_facts: 0, open_proposals: 0, documents: [], record_version: 1 }) });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/hire/roles"));
  });

  it("lets a deep link win", async () => {
    await signIn("/hire/answers", {});
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/hire/answers"));
  });

  it("keeps the usual landing for a member who has a record", async () => {
    await signIn("/jobs", { "GET /v1/me/record": ok({ facts: [{}], constraints: [], preferences: [], unconfirmed_facts: 0, open_proposals: 0, documents: [], record_version: 1 }) });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/jobs"));
  });
});

describe("the hiring shell", () => {
  it("names the organization with the demo pill and links the three sections", async () => {
    renderWithApi(<HiringShell>content</HiringShell>, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}/answer-clock`]: ok(clock()) });
    expect(await screen.findByText("Halden Demo")).toBeInTheDocument();
    expect(screen.getByText("Demo (invented)")).toBeInTheDocument();
    const links = within(screen.getByRole("navigation", { name: "Hiring desk" })).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Roles", "Answers", "Team"]);
    expect(screen.queryByText("Paused: answers overdue")).not.toBeInTheDocument();
  });

  it("prints the pause badge with the overdue count and an Answer now link", async () => {
    const overdue = clock({
      is_asking_paused: true,
      owed: [{ ...clock().owed[0]!, is_overdue: true, business_days_left: 0 }],
    });
    renderWithApi(<HiringShell>content</HiringShell>, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}/answer-clock`]: ok(overdue) });
    expect(await screen.findByText("Paused: answers overdue")).toBeInTheDocument();
    expect(screen.getByText("New introductions are paused for the whole organization until you answer 1 overdue.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Answer now" })).toHaveAttribute("href", "/hire/answers");
  });
});

describe("/hire/roles with no organization", () => {
  it("lists invitations with a Join button each, then the create form", async () => {
    renderWithApi(<RolesList />, { "GET /v1/me": ok(me({ organizations: [], invitations: [invitation] })) });
    expect(await screen.findByRole("button", { name: "Join Halden Demo" })).toBeInTheDocument();
    expect(screen.getByText("Create your organization to post roles.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Organization name/ })).toBeInTheDocument();
  });

  it("joins an invitation by organization id", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<Invitations invitations={[invitation]} />, { [`POST /v1/orgs/${ORG}/join`]: ok({}) });
    await user.click(screen.getByRole("button", { name: "Join Halden Demo" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]!.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(nav.refresh).toHaveBeenCalled());
  });

  it("creates an organization and moves on to the team page", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<CreateOrganization />, { "POST /v1/orgs": ok({ id: ORG }, 201) });
    await user.type(screen.getByRole("textbox", { name: /Organization name/ }), "Halden Demo");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/hire/team"));
    const body = (await calls[0]!.json()) as { name: string; timezone: string };
    expect(body.name).toBe("Halden Demo");
    expect(body.timezone).toBeTruthy();
  });
});

describe("/hire/roles", () => {
  const roles = [
    { id: "x1", title: "Senior Backend Engineer", status: "open", said_yes: 2, answers_owed: 2, answers_overdue: 1, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-22T00:00:00Z" },
    { id: "x2", title: "Data Engineer", status: "draft", said_yes: 0, answers_owed: 0, answers_overdue: 0, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-20T00:00:00Z" },
  ];
  it("lists roles with status, said yes, answers owed with the overdue count, and a New role button", async () => {
    renderWithApi(<RolesList />, { "GET /v1/me": ok(me()), "GET /v1/roles": ok(roles) });
    const table = await screen.findByRole("grid", { name: "Roles" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]!).getByText("Open")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("2 (1 overdue)")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("22 Nov")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Draft")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New role" })).toHaveAttribute("href", "/hire/roles/new");
    expect(screen.getByRole("list", { name: "Roles" })).toBeInTheDocument(); // the phone layout, hidden by CSS on a wide screen
  });
});

describe("/hire/team", () => {
  const org = (role: "owner" | "admin" | "member", members: unknown[]) => ({ id: ORG, name: "Halden Demo", timezone: "UTC", is_synthetic: true, role, members });
  const m = (id: string, role: string, extra: Record<string, unknown> = {}) => ({ member_id: id, user_id: id, display_name: `Demo ${id}`, email: `${id}@example.test`, role, status: "active", ...extra });

  it("lets an owner invite, and the last owner has no Remove", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<Team />, {
      "GET /v1/me": ok(me()),
      [`GET /v1/orgs/${ORG}`]: ok(org("owner", [m("o1", "owner"), m("m1", "member"), m("p1", "member", { user_id: null, status: "invited", display_name: null })])),
      [`POST /v1/orgs/${ORG}/members`]: ok({}, 201),
    });
    const table = await screen.findByRole("grid", { name: "Members" });
    expect(within(table).queryByRole("button", { name: "Remove o1@example.test" })).not.toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Remove m1@example.test" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Revoke invitation for p1@example.test" })).toBeInTheDocument();
    expect(within(table).getByText("Invited (pending)")).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "Email address" }), "new@example.test");
    await user.click(screen.getByRole("button", { name: "Send invitation" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(await calls.find((c) => c.method === "POST")!.json()).toEqual({ email: "new@example.test", role: "member" });
  });

  it("asks before removing a member", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<Team />, {
      "GET /v1/me": ok(me()),
      [`GET /v1/orgs/${ORG}`]: ok(org("owner", [m("o1", "owner"), m("m1", "member")])),
      [`DELETE /v1/orgs/${ORG}/members/m1`]: new Response(null, { status: 204 }),
    });
    await user.click(await screen.findByRole("button", { name: "Remove m1@example.test" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove this member?" });
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
  });

  it("hides management from a plain member", async () => {
    renderWithApi(<Team />, { "GET /v1/me": ok(me()), [`GET /v1/orgs/${ORG}`]: ok(org("member", [m("o1", "owner"), m("m1", "member")])) });
    await screen.findByRole("grid", { name: "Members" });
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send invitation" })).not.toBeInTheDocument();
  });

  it("shows Claim ownership to an admin of an ownerless organization", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<Team />, {
      "GET /v1/me": ok(me()),
      [`GET /v1/orgs/${ORG}`]: ok(org("admin", [m("a1", "admin")])),
      [`POST /v1/orgs/${ORG}/claim-ownership`]: ok(org("owner", [m("a1", "owner")])),
    });
    await user.click(await screen.findByRole("button", { name: "Claim ownership" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("claim-ownership"))).toBe(true));
  });
});

describe("error texts", () => {
  const failure = (code: string, details: Record<string, unknown> = {}) => new ApiFailure({ status: 409, code, details });
  it("words the hiring errors as FRONTEND_UI 6.4 does", () => {
    expect(messageFor(failure("conflict", { state: "answers_overdue" }))).toBe("New introductions are paused until overdue answers are given.");
    expect(messageFor(failure("preview_changed"))).toBe("What would be shared changed. Review it again.");
    expect(messageFor(failure("pay_range_required"))).toMatch(/pay range/);
    expect(messageFor(failure("requirements_unconfirmed"))).toMatch(/confirmed/);
    expect(messageFor(failure("already_resolved"))).toMatch(/changed in another tab/);
    expect(messageFor(failure("quota_exhausted", { resets_at: "2026-11-30T00:00:00Z" }))).toBe("No serious-interest signals left this week. They reset on 30 Nov (UTC).");
  });
  it("keeps a plain conflict message for any other state", () => {
    expect(messageFor(failure("conflict", { state: "open" }))).toMatch(/changed in another tab/);
  });
});

