import { describe, expect, it } from "vitest";
import openapi from "../../openapi.json";
import { CAMPAIGN_STAGE_LABELS, ERROR_MESSAGES, messageFor, PAUSE_LABELS, reasonText, UNKNOWN_REASON } from "./labels";

type Schema = { properties: Record<string, { enum?: string[]; anyOf?: { enum?: string[] }[] }> };
const schemas = openapi.components.schemas as unknown as Record<string, Schema>;
const failure = (code: string, details: Record<string, unknown> = {}) => ({ code, requestId: null, retryAfterSeconds: null, details });

describe("campaign labels", () => {
  it("has a label for every stage the API can return", () => {
    expect(Object.keys(CAMPAIGN_STAGE_LABELS).sort()).toEqual([...(schemas.Column.properties.stage.enum ?? [])].sort());
  });

  it("has a label for every pause reason", () => {
    const reasons = schemas.Card.properties.pause_reason.anyOf?.find((item) => item.enum)?.enum ?? [];
    expect(Object.keys(PAUSE_LABELS).sort()).toEqual([...reasons].sort());
  });

  it.each(["budget_exhausted", "job_closed", "violates_constraint", "autonomy_not_allowed", "version_conflict", "preview_changed", "already_resolved"])("has a message for %s", (code) => {
    expect(Object.keys(ERROR_MESSAGES)).toContain(code);
    expect(messageFor(failure(code)).length).toBeGreaterThan(10);
  });

  it("names the real state on a 409 and the existing campaign on a duplicate", () => {
    expect(messageFor(failure("conflict", { state: "sending" }))).toMatch(/no longer be undone/);
    expect(messageFor(failure("conflict", { campaign_id: "x" }))).toMatch(/already have a campaign/);
  });

  it("gives a code with no template the plain fallback", () => {
    expect(reasonText("no_such_code")).toBe(UNKNOWN_REASON);
    expect(reasonText("dial_never")).toMatch(/set this kind of action to never/);
  });
});
