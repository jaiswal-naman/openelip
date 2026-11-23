import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { Board } from "./Board";

const replace = vi.fn();
let search = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/app/campaigns",
  useSearchParams: () => new URLSearchParams(search),
}));

const card = (over: Record<string, unknown>) => ({
  id: "c1",
  title: "Platform Engineer",
  company_name: "Globex Demo",
  from_introduction: false,
  paused_at: null,
  pause_reason: null,
  closed_reason: null,
  next_step: { step_key: "research", agent_role: "researcher", text: "Building a cited company dossier." },
  pending_approvals: 0,
  last_activity_at: "2026-11-23T09:30:00Z",
  ...over,
});
const column = (stage: string, campaigns: unknown[] = []) => ({ stage, campaigns });
const stages = ["researching", "needs_you", "applied", "talking", "interview", "offer"];
const board = (cards: Record<string, unknown[]> = {}) => ok({ columns: stages.map((stage) => column(stage, cards[stage] ?? [])) });

describe("Board", () => {
  it("shows six stage columns with counts, and the empty state when there is nothing", async () => {
    search = "";
    renderWithApi(<Board />, { "GET /v1/campaigns": board(), "GET /v1/applications": ok([]) });
    expect(await screen.findByText(/No campaigns yet/)).toBeInTheDocument();
  });

  it("marks pause pills, Needs you, introductions and closing reasons", async () => {
    search = "";
    renderWithApi(<Board />, {
      "GET /v1/campaigns": board({
        researching: [card({ id: "a", title: "Paused One", pause_reason: "budget_exhausted", paused_at: "2026-11-23T09:00:00Z" })],
        applied: [card({ id: "b", title: "Waiting One", pending_approvals: 1, from_introduction: true })],
        needs_you: [card({ id: "c", title: "Packet One", pending_approvals: 1 })],
      }),
      "GET /v1/applications": ok([]),
    });
    const paused = (await screen.findByRole("heading", { name: "Paused One" })).closest("article") as HTMLElement;
    expect(within(paused).getByText("Budget reached")).toBeInTheDocument();
    const applied = screen.getByRole("heading", { name: "Waiting One" }).closest("article") as HTMLElement;
    expect(within(applied).getByText("Needs you")).toBeInTheDocument();
    expect(within(applied).getByText("From an introduction")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Researching" })).toBeInTheDocument();
  });

  it("asks for closed campaigns from the address and filters to Needs you", async () => {
    search = "closed=1&filter=needs-you";
    const asked = vi.fn(() => board({ applied: [card({ id: "b", title: "Waiting One", pending_approvals: 1 }), card({ id: "d", title: "Quiet One" })] }));
    renderWithApi(<Board />, { "GET /v1/campaigns": asked, "GET /v1/applications": ok([]) });
    expect(await screen.findByRole("heading", { name: "Waiting One" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Quiet One" })).not.toBeInTheDocument();
    expect(asked.mock.calls[0]).toBeTruthy();
  });

  it("gives a card with one matching application the Record status menu", async () => {
    search = "";
    renderWithApi(<Board />, {
      "GET /v1/campaigns": board({ applied: [card({ id: "b", title: "Platform Engineer" })] }),
      "GET /v1/applications": ok([{ id: "app1", job_id: 42, job_title: "Platform Engineer", company_name: "Globex Demo", status: "applied", status_changed_at: "2026-11-23T09:00:00Z", application_packet_id: null, events: [] }]),
    });
    expect(await screen.findByRole("button", { name: "Record status for Platform Engineer" })).toBeInTheDocument();
  });
});
