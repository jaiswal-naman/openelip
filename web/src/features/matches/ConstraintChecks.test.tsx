import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConstraintChecks } from "./ConstraintChecks";

describe("ConstraintChecks", () => {
  it("shows each constraint the person set with Satisfied, or Not stated (unverified) when the posting is silent", () => {
    render(
      <ConstraintChecks
        checks={[
          { kind: "location", statement: "Only jobs in the United States.", state: "satisfied" },
          { kind: "salary_floor", statement: "Never below USD 120,000 a year.", state: "not_stated" },
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: "Your hard constraints and whether this job meets them" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // the header and two constraints
    expect(rows[1]).toHaveTextContent("Location");
    expect(rows[1]).toHaveTextContent("Only jobs in the United States.");
    expect(rows[1]).toHaveTextContent("Satisfied");
    expect(rows[2]).toHaveTextContent("Salary floor");
    expect(rows[2]).toHaveTextContent("Not stated (unverified)");
  });

  it("says a constraint that was not set is not enforced, never that the job passed it", () => {
    render(<ConstraintChecks checks={[]} />);
    expect(screen.getByText("You have not set a hard constraint. Not set (not enforced).")).toBeInTheDocument();
    expect(screen.queryByText("Satisfied")).not.toBeInTheDocument();
  });
});
