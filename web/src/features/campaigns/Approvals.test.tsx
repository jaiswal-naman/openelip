import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { CampaignView } from "./CampaignView";
import { detail } from "./testing";

let search = "tab=follow-ups";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/app/campaigns/c1",
  useSearchParams: () => new URLSearchParams(search),
}));
afterEach(() => vi.useRealTimers());

const followUp = (over: Record<string, unknown> = {}) => ({
  step_key: "follow_up:1",
  action_id: 7,
  approval_id: "ap1",
  status: "awaiting_approval",
  delivery: null,
  to: "hiring@globex.example",
  subject: "Following up on Platform Engineer",
  body: "Hello, I applied on 23 November and remain interested.",
  undo_until: null,
  outcome_code: null,
  outcome_text: null,
  ...over,
});
const approval = { id: "ap1", status: "pending", payload: {}, payload_hash: "hash-1", editable_fields: ["subject", "body"], expires_at: null };
const base = (extra: Record<string, unknown> = {}, follow = followUp()) =>
  ({
    "GET /v1/campaigns/c1": ok(detail({ stage: "applied", follow_up_email: "hiring@globex.example", follow_ups: [follow] as never, pending_approvals: [{ id: "ap1", kind: "send_follow_up" }] })),
    "GET /v1/applications": ok([]),
    "GET /v1/approvals/ap1": ok(approval),
    ...extra,
  }) as never;

describe("Follow-ups tab", () => {
  it("shows the draft with To (you typed), and approves with the hash it displayed", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    const approved = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ payload_hash: "hash-1", edits: null });
      return ok({ approval: { id: "ap1", status: "approved" }, action: { id: 7, type: "send_follow_up", status: "awaiting_approval" }, release: "pending" });
    });
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/approvals/ap1/approve": approved }));
    expect(await screen.findByText("hiring@globex.example (you typed)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approved).toHaveBeenCalledTimes(1));
  });

  it("sends only subject and body as edits", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    const approved = vi.fn(async (request: Request) => {
      const body = (await request.json()) as { edits: Record<string, string> };
      expect(Object.keys(body.edits).sort()).toEqual(["body", "subject"]);
      expect(body.edits.subject).toBe("A better subject");
      return ok({ approval: { id: "ap1", status: "approved" }, action: { id: 8, type: "send_follow_up", status: "proposed" }, release: "pending" });
    });
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/approvals/ap1/approve": approved }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const subject = screen.getByLabelText("Subject");
    await user.clear(subject);
    await user.type(subject, "A better subject");
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve with my edits" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve with my edits" }));
    await waitFor(() => expect(approved).toHaveBeenCalledTimes(1));
  });

  it("skips with a rejection note", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    const rejected = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ note: "Skipped by you" });
      return ok({ approval: { id: "ap1", status: "rejected" }, action: { id: 7, type: "send_follow_up", status: "rejected" } });
    });
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/approvals/ap1/reject": rejected }));
    await user.click(await screen.findByRole("button", { name: "Skip" }));
    await waitFor(() => expect(rejected).toHaveBeenCalledTimes(1));
  });

  it("reloads and says the draft changed on 409 preview_changed", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/approvals/ap1/approve": fail(409, "preview_changed") }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/This changed since you opened it/)).toBeInTheDocument();
  });

  it("types the follow-up address with PATCH and says Saved", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    const patched = vi.fn(async (request: Request) => {
      expect(await request.json()).toEqual({ follow_up_email: "recruiter@globex.example", follow_up_after_days: 7 });
      return ok({ id: "c1" });
    });
    renderWithApi(
      <CampaignView id="c1" />,
      {
        "GET /v1/campaigns/c1": ok(detail({ stage: "applied" })),
        "GET /v1/applications": ok([]),
        "PATCH /v1/campaigns/c1": patched,
      } as never,
    );
    await user.type(await screen.findByLabelText("Follow-up address"), "recruiter@globex.example");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(patched).toHaveBeenCalledTimes(1);
  });
});

describe("Undo countdown", () => {
  const scheduled = (over: Record<string, unknown> = {}) => followUp({ status: "scheduled", approval_id: null, delivery: "pending", undo_until: "2026-11-23T10:10:00Z", ...over });

  it("counts down on the server's clock, even when this computer's clock is wrong", async () => {
    search = "tab=follow-ups";
    // This computer thinks it is a day later than the server (server_time says 10:00): the window still has ten minutes.
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"], now: new Date("2026-11-24T10:00:00Z") });
    renderWithApi(<CampaignView id="c1" />, base({}, scheduled()));
    expect(await screen.findByText(/Scheduled, sends at/)).toBeInTheDocument();
    expect(screen.getByText("Undo within 10:00")).toBeInTheDocument();
    expect(screen.getByText("Closing this tab does not stop the send.")).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(61_000);
    });
    expect(screen.getByText("Undo within 8:59")).toBeInTheDocument();
  });

  it("shows Undone only after the server says cancelled", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    let release: (response: Response) => void = () => undefined;
    const pending = new Promise<Response>((resolve) => (release = resolve));
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/control/actions/7/undo": () => pending }, scheduled()));
    await user.click(await screen.findAllByRole("button", { name: "Undo" }).then((buttons) => buttons[0]));
    expect(screen.queryByText("Undone")).not.toBeInTheDocument();
    release(ok({ action: { id: 7, status: "cancelled" }, outbox_cancelled: 1 }));
    expect(await screen.findByText("Undone")).toBeInTheDocument();
  });

  it("shows the real state when the undo is refused", async () => {
    search = "tab=follow-ups";
    const user = userEvent.setup();
    renderWithApi(<CampaignView id="c1" />, base({ "POST /v1/control/actions/7/undo": fail(409, "conflict", { state: "sending" }) }, scheduled()));
    await user.click((await screen.findAllByRole("button", { name: "Undo" }))[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Sending has started, so this can no longer be undone.");
  });

  it("shows Blocked and Failed with their reasons", async () => {
    search = "tab=follow-ups";
    renderWithApi(<CampaignView id="c1" />, base({}, followUp({ status: "blocked", approval_id: null, outcome_code: "dial_never", outcome_text: "You set this kind of action to never, so it was not attempted." })));
    const item = (await screen.findByRole("list", { name: "Follow-ups" })).querySelector("li") as HTMLElement;
    expect(within(item).getByText(/Blocked: You set this kind of action to never/)).toBeInTheDocument();
  });
});

describe("Packet tab", () => {
  const packet = { id: "pk1", job_id: 42, job_title: "Platform Engineer", company_name: "Globex Demo", status: "ready", tailored_resume_id: null, answers: [
    { question_id: "why_role", question: "Why", status: "drafted", answer: "Because I build ledgers.", fact_ids: [], removed_sentences: 0 },
    { question_id: "expected_pay", question: "Pay", status: "candidate_must_answer", answer: "", fact_ids: [], removed_sentences: 0 },
  ], cover_note: "I would like to apply.", removed_sentences: 1, created_at: "2026-11-23T09:00:00Z" };
  const packetRoutes = (summary: Record<string, unknown>, pending: unknown[], extra: Record<string, unknown> = {}) =>
    ({
      "GET /v1/campaigns/c1": ok(detail({ stage: "needs_you", packet: summary as never, pending_approvals: pending as never })),
      "GET /v1/applications": ok([]),
      "GET /v1/applications/packets/pk1": ok(packet),
      "GET /v1/approvals/ap9": ok({ ...approval, id: "ap9", payload_hash: "hash-9" }),
      ...extra,
    }) as never;

  it("shows You answer this, approves the packet with its hash, and says Checking", async () => {
    search = "tab=packet";
    const user = userEvent.setup();
    const approved = vi.fn(async (request: Request) => {
      expect(((await request.json()) as { payload_hash: string }).payload_hash).toBe("hash-9");
      return ok({ approval: { id: "ap9", status: "approved" }, action: { id: 3, type: "submit_application", status: "awaiting_approval" }, release: "pending" });
    });
    renderWithApi(
      <CampaignView id="c1" />,
      packetRoutes({ id: "pk1", status: "ready", approval_id: "ap9", action_status: "awaiting_approval" }, [{ id: "ap9", kind: "submit_application" }], { "POST /v1/approvals/ap9/approve": approved }),
    );
    expect(await screen.findByText("Because I build ledgers.")).toBeInTheDocument();
    expect(screen.getByText("You answer this")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve packet" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve packet" }));
    await waitFor(() => expect(approved).toHaveBeenCalledTimes(1));
  });

  it("shows the submit kit and that the system never submits once the action is scheduled", async () => {
    search = "tab=packet";
    renderWithApi(<CampaignView id="c1" />, packetRoutes({ id: "pk1", status: "approved", approval_id: "ap9", action_status: "scheduled" }, []));
    expect(await screen.findByText("The system never submits. Record Applied once you have.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download the resume packet" })).toHaveAttribute("href", "/v1/applications/packets/pk1/file?format=pdf");
    expect(screen.getByRole("link", { name: /Open the posting/ })).toHaveAttribute("href", "https://jobs.globex.example/apply/42");
    expect(await screen.findByRole("button", { name: "Copy the answer to: Why are you interested in this role?" })).toBeInTheDocument();
  });

  it("says Checking while the release is pending after a reload", async () => {
    search = "tab=packet";
    renderWithApi(<CampaignView id="c1" />, packetRoutes({ id: "pk1", status: "ready", approval_id: "ap9", action_status: "awaiting_approval" }, []));
    expect(await screen.findByText("Checking...")).toBeInTheDocument();
  });
});
