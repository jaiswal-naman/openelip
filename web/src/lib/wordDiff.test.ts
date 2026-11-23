import { describe, expect, it } from "vitest";
import { wordDiff } from "./wordDiff";

describe("wordDiff", () => {
  it("keeps identical text as one equal run", () => {
    expect(wordDiff("Built a billing service", "Built a billing service")).toEqual([{ kind: "equal", text: "Built a billing service" }]);
  });

  it("marks an insertion", () => {
    expect(wordDiff("Built a service", "Built a billing service")).toEqual([
      { kind: "equal", text: "Built a" },
      { kind: "added", text: "billing" },
      { kind: "equal", text: "service" },
    ]);
  });

  it("marks a deletion", () => {
    expect(wordDiff("Built a billing service", "Built a service")).toEqual([
      { kind: "equal", text: "Built a" },
      { kind: "removed", text: "billing" },
      { kind: "equal", text: "service" },
    ]);
  });

  it("marks a replacement as the old words removed and the new words added", () => {
    const runs = wordDiff("Led a team of three", "Led a team of four");
    expect(runs.filter((run) => run.kind === "removed")).toEqual([{ kind: "removed", text: "three" }]);
    expect(runs.filter((run) => run.kind === "added")).toEqual([{ kind: "added", text: "four" }]);
    expect(runs[0]).toEqual({ kind: "equal", text: "Led a team of" });
  });

  it("shows a moved word as removed in one place and added in the other", () => {
    const runs = wordDiff("one two three", "two three one");
    expect(runs.map((run) => run.kind)).toContain("removed");
    expect(runs.map((run) => run.kind)).toContain("added");
    const rebuilt = runs.filter((run) => run.kind !== "removed").map((run) => run.text).join(" ");
    expect(rebuilt).toBe("two three one");
  });

  it("returns nothing for two empty texts, and only added or only removed against one empty text", () => {
    expect(wordDiff("", "")).toEqual([]);
    expect(wordDiff("", "new line")).toEqual([{ kind: "added", text: "new line" }]);
    expect(wordDiff("old line", "")).toEqual([{ kind: "removed", text: "old line" }]);
  });

  it("ignores extra spaces", () => {
    expect(wordDiff("a  b", "a b")).toEqual([{ kind: "equal", text: "a b" }]);
  });
});
