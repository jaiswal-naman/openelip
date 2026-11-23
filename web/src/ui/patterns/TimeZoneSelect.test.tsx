import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { TimeZoneSelect, timeZoneOptions } from "./TimeZoneSelect";

function Harness({ initial }: { initial: string }) {
  const [zone, setZone] = useState(initial);
  return <TimeZoneSelect value={zone} onChange={setZone} />;
}

describe("timeZoneOptions", () => {
  it("lists the zones the browser knows", () => {
    const zones = timeZoneOptions("Asia/Kolkata");
    expect(zones).toContain("Asia/Kolkata");
    expect(zones).toContain("America/New_York");
    expect(zones.length).toBeGreaterThan(100);
  });

  it("adds the zone a person already has when the browser's list leaves it out, such as UTC, and puts it first", () => {
    const zones = timeZoneOptions("Not/AKnownZone");
    expect(zones[0]).toBe("Not/AKnownZone");
    expect(new Set(zones).size).toBe(zones.length);
  });
});

describe("TimeZoneSelect", () => {
  it("is a labelled select showing the current zone", () => {
    render(<Harness initial="Asia/Kolkata" />);
    expect(screen.getByRole("combobox", { name: "Time zone" })).toHaveValue("Asia/Kolkata");
  });

  it("keeps the zone it opened with in the list after another one is chosen", async () => {
    const user = userEvent.setup();
    render(<Harness initial="Not/AKnownZone" />);
    await user.selectOptions(screen.getByRole("combobox", { name: "Time zone" }), "Europe/Paris");
    expect(screen.getByRole("combobox", { name: "Time zone" })).toHaveValue("Europe/Paris");
    expect(screen.getByRole("option", { name: "Not/AKnownZone" })).toBeInTheDocument();
  });
});
