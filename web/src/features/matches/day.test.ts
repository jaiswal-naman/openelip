import { describe, expect, it } from "vitest";
import { dayLabel } from "./day";

describe("dayLabel", () => {
  it("writes a local date as the weekday, the day and the month", () => {
    expect(dayLabel("2026-11-23", "en-US")).toBe("Mon 23 Nov");
    expect(dayLabel("2026-01-01", "en-US")).toBe("Thu 1 Jan");
  });

  it("does not move the day with the machine's time zone", () => {
    // The test runner is set to UTC; the date must not depend on that. A date at midnight UTC would drift in the Americas.
    expect(dayLabel("2026-03-08", "en-US")).toBe("Sun 8 Mar");
  });

  it("shows a value that is not a date as it came", () => {
    expect(dayLabel("not a date")).toBe("not a date");
  });
});
