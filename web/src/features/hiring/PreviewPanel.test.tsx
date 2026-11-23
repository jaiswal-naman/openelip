import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { PreviewPanel } from "./PreviewPanel";
import { preview, ROLE } from "./testing";

describe("PreviewPanel", () => {
  it("shows the counts, 'fewer than 5' verbatim, and the as-of time", async () => {
    renderWithApi(<PreviewPanel role={{ id: ROLE, status: "open" }} />, { [`GET /v1/roles/${ROLE}/preview`]: ok(preview) });
    expect(await screen.findByText("14")).toBeInTheDocument();
    expect(screen.getAllByText("fewer than 5")).toHaveLength(2);
    expect(screen.getByText("Small counts show as 'fewer than 5'.")).toBeInTheDocument();
    expect(screen.getByText(/As of 23 Nov 2026/)).toBeInTheDocument();
  });

  it("never shows a name, email or user id, whatever the response holds", async () => {
    const leaky = { ...preview, name: "Demo Candidate A", email: "candidate@example.test", user_id: "11111111-1111-4111-8111-111111111111", candidates: [{ name: "Demo Candidate A" }] };
    const { container } = renderWithApi(<PreviewPanel role={{ id: ROLE, status: "open" }} />, { [`GET /v1/roles/${ROLE}/preview`]: ok(leaky) });
    await screen.findByText("14");
    const text = container.textContent ?? "";
    expect(text).not.toContain("Demo Candidate A");
    expect(text).not.toContain("candidate@example.test");
    expect(text).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-/);
  });

  it("says counts appear after publishing, and does not ask the server, for a draft", () => {
    const { calls } = renderWithApi(<PreviewPanel role={{ id: ROLE, status: "draft" }} />);
    expect(screen.getByText("Counts appear after publishing.")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("says no one has been checked before the first run", async () => {
    renderWithApi(<PreviewPanel role={{ id: ROLE, status: "open" }} />, { [`GET /v1/roles/${ROLE}/preview`]: ok({ ...preview, as_of: null }) });
    expect(await screen.findByText("No candidates have been checked yet.")).toBeInTheDocument();
  });
});
