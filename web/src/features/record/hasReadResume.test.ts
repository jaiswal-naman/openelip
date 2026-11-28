import { describe, expect, it } from "vitest";
import { hasReadResume } from "./hasReadResume";

const document = (status: string) => ({ documents: [{ status }] }) as Parameters<typeof hasReadResume>[0];

describe("whether a resume was read", () => {
  it("is false for a record with no document, and for one still being read or that failed", () => {
    expect(hasReadResume({ documents: [] })).toBe(false);
    for (const status of ["queued", "running", "failed"]) expect(hasReadResume(document(status))).toBe(false);
  });

  it("is true once a document succeeded, whatever else is on the record", () => {
    expect(hasReadResume({ documents: [{ status: "failed" }, { status: "succeeded" }] } as Parameters<typeof hasReadResume>[0])).toBe(true);
  });
});
