import { describe, expect, it } from "vitest";
import { newReply, reduceReply } from "./reply";

const apply = (events: [string, Record<string, unknown>][]) => events.reduce((reply, [name, data]) => reduceReply(reply, name, data), newReply());

describe("reduceReply", () => {
  it("appends text deltas in order and ends on message.end", () => {
    const reply = apply([
      ["text.delta", { text: "Hello " }],
      ["text.delta", { text: "there." }],
      ["message.end", { finish_reason: "complete" }],
    ]);
    expect(reply).toMatchObject({ text: "Hello there.", done: true, finishReason: "complete" });
  });

  it("shows a status line while a tool runs and notes that constraints were applied after a job search", () => {
    const running = apply([["tool.start", { tool: "list_matches" }]]);
    expect(running.status).toBe("Looking at your matches...");
    const searched = reduceReply(reduceReply(newReply(), "tool.start", { tool: "search_jobs" }), "tool.result", { tool: "search_jobs" });
    expect(searched.status).toBeNull();
    expect(searched.constraintsApplied).toBe(true);
    expect(reduceReply(newReply(), "tool.result", { tool: "list_matches" }).constraintsApplied).toBe(false);
  });

  it("keeps citations by alias and one card per proposal", () => {
    const proposal = { event_id: "e1", entity: "fact", operation: "create", before: null, after: { statement: "Rust" }, statement: "Add Rust" };
    const reply = apply([
      ["citation", { alias: "d1", kind: "decision", decision_id: "x" }],
      ["proposal", proposal],
      ["proposal", proposal],
    ]);
    expect(Object.keys(reply.citations)).toEqual(["d1"]);
    expect(reply.proposals).toHaveLength(1);
  });

  it("replaces the partial text with a snapshot", () => {
    const reply = apply([
      ["text.delta", { text: "partial" }],
      ["message.snapshot", { text: "the whole text so far", citations: [{ alias: "d1", kind: "decision" }], proposals: [] }],
    ]);
    expect(reply.text).toBe("the whole text so far");
    expect(reply.citations.d1).toBeDefined();
  });

  it("records an error, then the interrupted end that always follows it", () => {
    const reply = apply([
      ["error", { code: "ai_disabled", is_retryable: false }],
      ["message.end", { finish_reason: "interrupted" }],
    ]);
    expect(reply.error).toEqual({ code: "ai_disabled", retryable: false });
    expect(reply).toMatchObject({ done: true, finishReason: "interrupted" });
  });
});
