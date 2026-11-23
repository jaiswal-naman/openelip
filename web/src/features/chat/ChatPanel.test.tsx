import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { ChatPanel } from "./ChatPanel";
import { setChatContext } from "./context";

/** A stand-in for the browser's EventSource that the test drives by hand. */
class FakeEventSource {
  static all: FakeEventSource[] = [];
  static CLOSED = 2;
  readyState = 1;
  closed = false;
  listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  constructor(
    public url: string,
    public init?: EventSourceInit,
  ) {
    FakeEventSource.all.push(this);
  }
  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(name: string, data: unknown) {
    act(() => {
      for (const listener of this.listeners.get(name) ?? []) listener(new MessageEvent(name, { data: JSON.stringify(data) }));
    });
  }
}

const accepted = { user_message_id: "u1", message_id: "m1", status: "running", stream_url: "/v1/chat/sessions/s1/messages/m1/stream" };
const routes = {
  "POST /v1/chat/sessions": ok({ id: "s1", kind: "open", created_at: "2026-11-23T00:00:00Z", last_message_at: null }, 201),
  "POST /v1/chat/sessions/s1/messages": ok(accepted, 202),
};

async function openAndSend(extra: Record<string, never> | Record<string, (r: Request) => Response> = {}, text = "Why was the excluded job ruled out?") {
  const user = userEvent.setup();
  const view = renderWithApi(<ChatPanel />, { ...routes, ...extra });
  await user.click(screen.getByRole("button", { name: "Ask the agent" }));
  const dialog = await screen.findByRole("dialog", { name: "Ask the agent" });
  await user.type(within(dialog).getByRole("textbox", { name: "Your message" }), text);
  await user.click(within(dialog).getByRole("button", { name: "Send" }));
  return { user, dialog, ...view };
}

beforeEach(() => {
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => {
  setChatContext(null);
  vi.unstubAllGlobals();
});

describe("ChatPanel", () => {
  it("opens as a dialog with a labelled composer, and Escape closes it and returns focus to the opener", async () => {
    const user = userEvent.setup();
    renderWithApi(<ChatPanel />);
    const opener = screen.getByRole("button", { name: "Ask the agent" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Ask the agent" });
    expect(within(dialog).getByRole("textbox", { name: "Your message" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("posts the message with an Idempotency-Key and streams the reply in order, announcing it once at the end", async () => {
    const { dialog, calls } = await openAndSend();
    await waitFor(() => expect(FakeEventSource.all).toHaveLength(1));
    const post = calls.find((call) => call.url.endsWith("/messages") && call.method === "POST");
    expect(post?.headers.get("Idempotency-Key")).toBeTruthy();
    const stream = FakeEventSource.all[0];
    expect(stream.url).toBe(accepted.stream_url);
    expect(stream.init?.withCredentials).toBe(true);

    stream.emit("message.start", { message_id: "m1", session_id: "s1" });
    stream.emit("tool.start", { call_id: "c1", tool: "list_matches" });
    expect(await within(dialog).findByText("Looking at your matches...")).toBeInTheDocument();
    stream.emit("tool.result", { call_id: "c1", tool: "list_matches", refs: {} });
    stream.emit("text.delta", { text: "It was ruled out " });
    stream.emit("text.delta", { text: "by your US-only constraint [[d1]]." });
    stream.emit("citation", { alias: "d1", kind: "decision", decision_id: "6f1d2c3e-aaaa-4bbb-8ccc-0123456789ab" });

    const announce = within(dialog).getAllByRole("status").find((el) => el.className.includes("sr-only"));
    expect(announce).toHaveTextContent("");
    expect(within(dialog).getByRole("log")).toHaveTextContent("It was ruled out by your US-only constraint");
    expect(within(dialog).getByRole("link", { name: "Decision 1" })).toHaveAttribute("href", "/app/matches/6f1d2c3e-aaaa-4bbb-8ccc-0123456789ab");

    stream.emit("message.end", { message_id: "m1", finish_reason: "complete", usage: null, cost_usd: null });
    await waitFor(() => expect(announce).toHaveTextContent("It was ruled out by your US-only constraint [[d1]]."));
    expect(stream.closed).toBe(true);
  });

  it("shows a proposal as a card, changes nothing until Confirm, then calls the confirm route", async () => {
    const confirm = vi.fn(() => ok({ entity: "fact", entity_id: "f1", record_version: 3 }));
    const { user, dialog, calls } = await openAndSend({ "POST /v1/me/events/e1/confirm": confirm } as never, "I also know Rust.");
    await waitFor(() => expect(FakeEventSource.all).toHaveLength(1));
    FakeEventSource.all[0].emit("proposal", { event_id: "e1", entity: "fact", operation: "create", before: null, after: { statement: "Skills: Rust." }, statement: "Add the skill Rust" });
    const card = await within(dialog).findByRole("region", { name: "Suggested change to your record" });
    expect(card).toHaveTextContent("Add the skill Rust");
    expect(card).toHaveTextContent("Skills: Rust.");
    expect(card).toHaveTextContent("Source: what you said just now");
    expect(confirm).not.toHaveBeenCalled();
    expect(calls.some((call) => call.url.includes("/me/events/"))).toBe(false);

    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    expect(await within(card).findByText("Confirmed")).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it("calls the decline route on Decline", async () => {
    const decline = vi.fn(() => ok({ event_id: "e1" }));
    const { user, dialog } = await openAndSend({ "POST /v1/me/events/e1/decline": decline } as never);
    await waitFor(() => expect(FakeEventSource.all).toHaveLength(1));
    FakeEventSource.all[0].emit("proposal", { event_id: "e1", entity: "fact", operation: "create", before: null, after: { statement: "x" }, statement: "Add x" });
    await user.click(await within(dialog).findByRole("button", { name: "Decline" }));
    expect(await within(dialog).findByText("Declined")).toBeInTheDocument();
    expect(decline).toHaveBeenCalledTimes(1);
  });

  it("says why a reply ended early, and the snapshot after a reconnect replaces the partial text", async () => {
    const { dialog } = await openAndSend();
    await waitFor(() => expect(FakeEventSource.all).toHaveLength(1));
    const stream = FakeEventSource.all[0];
    stream.emit("text.delta", { text: "partial" });
    stream.emit("message.snapshot", { text: "partial and more", citations: [], proposals: [] });
    stream.emit("message.end", { message_id: "m1", finish_reason: "length", usage: null, cost_usd: null });
    expect(await within(dialog).findByText(/stopped early because it reached a limit/)).toBeInTheDocument();
    expect(within(dialog).getByRole("log")).toHaveTextContent("partial and more");
    expect(within(dialog).getByRole("log")).not.toHaveTextContent("partialpartial");
  });

  it("shows context: company, sends the job with the message, and clears on request", async () => {
    setChatContext({ jobId: 77, company: "Acme" });
    const seen: unknown[] = [];
    const user = userEvent.setup();
    renderWithApi(<ChatPanel />, {
      ...routes,
      "POST /v1/chat/sessions/s1/messages": async (request: Request) => {
        seen.push(await request.json());
        return ok(accepted, 202);
      },
    });
    await user.click(screen.getByRole("button", { name: "Ask the agent" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("context: Acme")).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Your message" }), "Is this a fit?");
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(seen).toEqual([{ text: "Is this a fit?", context_job_id: 77 }]));
    await user.click(within(dialog).getByRole("button", { name: "Clear the job context" }));
    expect(within(dialog).queryByText("context: Acme")).not.toBeInTheDocument();
  });

  it("calls the stop route while a reply is running", async () => {
    const stop = vi.fn(() => ok({ message_id: "m1", finish_reason: null }));
    const { user, dialog } = await openAndSend({ "POST /v1/chat/sessions/s1/messages/m1/stop": stop } as never);
    await user.click(await within(dialog).findByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["ai_disabled", 503, {}, /AI features are not set up on this server/],
    ["consent_required", 403, {}, /agreement to AI processing/],
    ["rate_limited", 429, { retry_after_seconds: 600 }, /Try again in 10 minutes/],
  ])("explains a refusal: %s", async (code, status, details, message) => {
    await openAndSend({ "POST /v1/chat/sessions/s1/messages": () => fail(status, code, details) } as never);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    if (code === "ai_disabled") expect(screen.getByRole("link", { name: "search jobs" })).toHaveAttribute("href", "/jobs");
    if (code === "consent_required") expect(screen.getByRole("link", { name: "Review and agree" })).toBeInTheDocument();
  });
});
