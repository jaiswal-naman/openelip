import { describe, expect, it } from "vitest";
import openapi from "../../openapi.json";
import {
  BRIEF_FREQUENCY_LABELS,
  CONSTRAINT_KIND_LABELS,
  CONSTRAINT_STATE_LABELS,
  DISMISS_REASON_LABELS,
  MATCH_REASON_LABELS,
  OUTCOME_LABELS,
  UNVERIFIED_LABELS,
  VERDICT_LABELS,
} from "./labels";

const schemas = openapi.components.schemas;

/** The codes of a schema that is a union of alternatives, such as an enum or null: every enum value and every constant. */
const codesOf = (alternatives: { enum?: string[]; const?: string }[]) => alternatives.flatMap((alternative) => alternative.enum ?? (alternative.const ? [alternative.const] : []));

// FRONTEND_UI 6.2: a code the API can send and labels.ts does not name fails here. The codes are read from the schema the
// API generates (web/openapi.json), not copied, so a new value in the backend fails this test until it has a label.
describe("match and brief labels", () => {
  const cases = [
    ["verdict", VERDICT_LABELS, schemas.EvidenceItem.properties.verdict.enum],
    // not_evaluated is the on-request check's answer; the four stored outcomes are the other alternative.
    ["decision outcome", OUTCOME_LABELS, codesOf(schemas.DecisionView.properties.outcome.anyOf)],
    ["match reason", MATCH_REASON_LABELS, schemas.MatchView.properties.reason_codes.items.enum],
    ["constraint kind", CONSTRAINT_KIND_LABELS, schemas.ConstraintCheck.properties.kind.enum],
    ["constraint state", CONSTRAINT_STATE_LABELS, schemas.ConstraintCheck.properties.state.enum],
    ["unverified flag", UNVERIFIED_LABELS, schemas.MatchView.properties.unverified.items.enum],
    ["dismiss reason", DISMISS_REASON_LABELS, codesOf(schemas.FeedbackBody.properties.reason.anyOf)],
    ["brief frequency", BRIEF_FREQUENCY_LABELS, schemas.BriefSettings.properties.frequency.enum],
  ] as const;

  it.each(cases)("name every %s the API can send, and nothing else", (_name, labels, codes) => {
    expect(Object.keys(labels).sort()).toEqual([...codes].sort());
    for (const label of Object.values(labels)) expect(label.length).toBeGreaterThan(2);
  });

  it("words the verdicts and the reasons as FRONTEND_UI 6.2 does", () => {
    expect(Object.values(VERDICT_LABELS)).toEqual(["Met", "Partly met", "Not met", "No evidence"]);
    expect(MATCH_REASON_LABELS.hard_constraint).toBe("Filtered by your constraints");
    expect(MATCH_REASON_LABELS.citation_unresolved).toBe("Withheld: a citation did not resolve");
    expect(MATCH_REASON_LABELS.below_daily_cap).toBe("Below today's five");
    expect(MATCH_REASON_LABELS.material_change).toBe("Changed since you saw it");
    expect(CONSTRAINT_STATE_LABELS.not_stated).toBe("Not stated (unverified)");
  });
});
