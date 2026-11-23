import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { LocalSession } from "./LocalSession";

const nav = vi.hoisted(() => ({ reloadTo: vi.fn() }));
vi.mock("@/lib/navigate", () => ({ reloadTo: nav.reloadTo }));

// An invented account
const me = { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "owner@example.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" };
const status = (key_set: boolean) => ({
  mode: "local",
  has_owner: true,
  model: { provider: "anthropic", key_set, key_last4: key_set ? "LAST" : null, source: key_set ? "app" : null },
});

beforeEach(() => {
  nav.reloadTo.mockReset();
});

describe("opening the owner's session", () => {
  it("posts to /v1/auth/local with the browser's time zone, then goes where the person was going", async () => {
    const { calls } = renderWithApi(<LocalSession next="/jobs?q=backend" />, {
      "POST /v1/auth/local": ok(me, 201),
      "GET /v1/local/status": ok(status(true)),
    });
    await waitFor(() => expect(nav.reloadTo).toHaveBeenCalledWith("/jobs?q=backend"));
    expect(calls[0].method).toBe("POST");
    expect(await calls[0].clone().json()).toEqual({ timezone: "UTC" });
  });

  it("goes to the setup page first when there is no model key, and still comes back to where it was going", async () => {
    renderWithApi(<LocalSession next="/jobs" />, {
      "POST /v1/auth/local": ok(me, 201),
      "GET /v1/local/status": ok(status(false)),
    });
    await waitFor(() => expect(nav.reloadTo).toHaveBeenCalledWith("/setup?next=%2Fjobs"));
  });

  it("says why it could not, goes nowhere, and tries again when asked", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderWithApi(<LocalSession next="/jobs" />, {
      "POST /v1/auth/local": () => (++calls === 1 ? fail(409, "conflict", { state: "several_accounts" }) : ok(me, 201)),
      "GET /v1/local/status": ok(status(true)),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("This database holds more than one account");
    expect(nav.reloadTo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(nav.reloadTo).toHaveBeenCalledWith("/jobs"));
  });
});
