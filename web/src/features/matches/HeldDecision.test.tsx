import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HeldDecision } from "./HeldDecision";
import { decisionData } from "./testing";

describe("HeldDecision", () => {
  it("tells a stale link to a filtered job that it no longer meets the constraints, names the rule, and shows no evidence table", () => {
    render(<HeldDecision decision={decisionData()} />);
    expect(screen.getByRole("heading", { level: 1, name: "Demo posting (invented): Onsite Support Engineer" })).toBeInTheDocument();
    expect(screen.getByText("This job no longer meets your constraints.")).toBeInTheDocument();
    expect(screen.getByText("Filtered by your constraints")).toBeInTheDocument();
    expect(screen.getByText(/Your rule: .Only jobs in the United States../)).toHaveTextContent("(Location, Remote policy)");
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See everything held back" })).toHaveAttribute("href", "/app/today?view=why-not");
  });

  it("says a constraint that has since been removed or replaced is gone, instead of guessing which one it was", () => {
    render(<HeldDecision decision={decisionData({ constraint: { id: null, kind: "location", statement: null } })} />);
    expect(screen.getByText(/The constraint that removed it is one you have since removed or changed/)).toBeInTheDocument();
  });

  it("explains a withheld job in plain words and lists where a scored one fell short", () => {
    render(
      <HeldDecision
        decision={decisionData({
          outcome: "withheld",
          reason_codes: ["required_not_met"],
          constraint: null,
          violated_kinds: [],
          gaps: [{ requirement: "Owns on-call rotations", importance: "required", verdict: "not_met" }],
        })}
      />,
    );
    expect(screen.getByText("This job was held back, so there is no match to open.")).toBeInTheDocument();
    const why = screen.getByRole("region", { name: "Why" });
    expect(why).toHaveTextContent("A required item is not met. A requirement the posting calls required is not met by your record.");
    expect(within(why).getByText("Owns on-call rotations")).toBeInTheDocument();
    expect(why).toHaveTextContent("(Required, Not met)");
  });

  it("says withheld: a citation did not resolve, in the words of the design, when that was the reason", () => {
    render(<HeldDecision decision={decisionData({ outcome: "withheld", reason_codes: ["citation_unresolved"], constraint: null, violated_kinds: [] })} />);
    expect(screen.getByText("Withheld: a citation did not resolve")).toBeInTheDocument();
  });
});
