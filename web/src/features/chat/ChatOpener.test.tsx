import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { ChatOpener } from "./ChatOpener";

const me = { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "reader@example.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" };

describe("ChatOpener", () => {
  it("offers Ask the agent to a signed-in candidate", async () => {
    renderWithApi(<ChatOpener />, { "GET /v1/me": ok(me) });
    expect(await screen.findByRole("button", { name: "Ask the agent" })).toBeInTheDocument();
  });

  it("offers nothing when nobody is signed in", async () => {
    const view = renderWithApi(<ChatOpener />, { "GET /v1/me": fail(401, "unauthenticated") });
    await waitFor(() => expect(view.calls.length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "Ask the agent" })).not.toBeInTheDocument();
  });
});
