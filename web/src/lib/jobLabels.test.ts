import { describe, expect, it } from "vitest";
import openapi from "../../openapi.json";
import { ATS_LABELS, EMPLOYMENT_TYPE_LABELS, IMPORTANCE_LABELS, NOT_STATED, WORK_ARRANGEMENT_LABELS } from "./labels";

const schemas = openapi.components.schemas;

// FRONTEND_UI 6.2: a code the API can send and labels.ts does not name fails here. The codes are read from the schema the
// API generates (web/openapi.json), not copied, so a new value in the backend fails this test until it has a label.
describe("job labels", () => {
  const cases = [
    ["source", ATS_LABELS, schemas.JobSource.properties.ats.enum],
    ["work arrangement", WORK_ARRANGEMENT_LABELS, schemas.Job.properties.work_arrangements.items.enum],
    ["employment type", EMPLOYMENT_TYPE_LABELS, schemas.Job.properties.employment_type.enum],
    ["requirement importance", IMPORTANCE_LABELS, schemas.Requirement.properties.importance.enum],
  ] as const;

  it.each(cases)("name every %s the API can send, and nothing else", (_name, labels, codes) => {
    expect(Object.keys(labels).sort()).toEqual([...codes].sort());
    for (const label of Object.values(labels)) expect(label.length).toBeGreaterThan(2);
  });

  it("says Not stated in the posting, never a dash or a guess, for an employment type the posting does not give", () => {
    expect(NOT_STATED).toBe("Not stated in the posting");
    expect(EMPLOYMENT_TYPE_LABELS.unknown).toBe(NOT_STATED);
  });
});
