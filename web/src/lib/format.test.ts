import { describe, expect, it } from "vitest";
import { formatCount, formatDateTime, formatLatency, formatMoney, formatMonth, formatPercent } from "./format";

const plain = (text: string) => text.replace(/ /g, " "); // Intl puts a no-break space after the currency code

describe("formatMoney", () => {
  it("shows two decimals from one cent up", () => {
    expect(plain(formatMoney("1.1234", "en-US"))).toBe("USD 1.12");
    expect(plain(formatMoney("0.01", "en-US"))).toBe("USD 0.01");
    expect(plain(formatMoney("0.000000", "en-US"))).toBe("USD 0.00");
  });

  it("shows four decimals under one cent", () => {
    expect(plain(formatMoney("0.004200", "en-US"))).toBe("USD 0.0042");
    expect(plain(formatMoney("0.0040", "en-US"))).toBe("USD 0.0040");
  });

  it("never prints a real cost as zero: below four decimals it shows the ledger's six", () => {
    expect(plain(formatMoney("0.000007", "en-US"))).toBe("USD 0.000007");
  });

  it("groups digits the way the viewer's locale does", () => {
    expect(plain(formatMoney(1234567.5, "en-IN"))).toBe("USD 12,34,567.50");
    expect(plain(formatMoney(1234567.5, "en-US"))).toBe("USD 1,234,567.50");
  });

  it("says Unknown for a value that is not a number, never a zero", () => {
    expect(formatMoney("not a number")).toBe("Unknown");
  });
});

describe("formatCount", () => {
  it("is exact up to 9,999 and compact above", () => {
    expect(formatCount(9996, "en-US")).toBe("9,996");
    expect(formatCount(9999, "en-US")).toBe("9,999");
    expect(formatCount(10000, "en-US")).toBe("10K");
    expect(formatCount(12400, "en-US")).toBe("12.4K");
  });
});

describe("formatLatency", () => {
  it("shows whole milliseconds under one second", () => {
    expect(plain(formatLatency(850, "en-US"))).toBe("850 ms");
    expect(plain(formatLatency(0, "en-US"))).toBe("0 ms");
    expect(plain(formatLatency(999, "en-US"))).toBe("999 ms");
  });

  it("switches to seconds with at most one decimal from one second up", () => {
    expect(plain(formatLatency(1000, "en-US"))).toBe("1 s");
    expect(plain(formatLatency(1234, "en-US"))).toBe("1.2 s");
    expect(plain(formatLatency(41000, "en-US"))).toBe("41 s");
  });

  it("rounds a fraction of a millisecond first, so 999.6 ms reads as 1 s and never as '1,000 ms'", () => {
    expect(plain(formatLatency(999.6, "en-US"))).toBe("1 s");
  });

  it("uses the viewer's decimal mark", () => {
    expect(plain(formatLatency(1234, "de-DE"))).toBe("1,2 s");
  });

  it("keeps the number and its unit on one line", () => {
    expect(formatLatency(850, "en-US")).toContain(" ms");
    expect(formatLatency(1234, "en-US")).toContain(" s");
  });

  it("says Unknown for a value that is not a number, never a zero", () => {
    expect(formatLatency(Number.NaN)).toBe("Unknown");
  });
});

describe("formatPercent", () => {
  it("rounds to whole percents and never shows a real share as 0%", () => {
    expect(formatPercent(0.374, "en-US")).toBe("37%");
    expect(formatPercent(0, "en-US")).toBe("0%");
    expect(formatPercent(0.001, "en-US")).toBe("<1%");
  });
});

describe("formatDateTime", () => {
  const now = new Date("2026-11-22T12:00:00Z");

  it("writes day, short month, 24-hour time and the zone, in the viewer's zone", () => {
    const text = formatDateTime("2026-11-22T00:42:00Z", { locale: "en-GB", timeZone: "Asia/Kolkata" }, now);
    expect(text).toMatch(/^22 Nov 06:12 /); // 00:42 UTC is 06:12 in India
  });

  it("is the same instant in another zone, with that zone's name", () => {
    expect(formatDateTime("2026-11-22T00:42:00Z", { locale: "en-GB", timeZone: "UTC" }, now)).toBe("22 Nov 00:42 UTC");
  });

  it("adds the year only when it is not the current one", () => {
    expect(formatDateTime("2025-03-03T06:12:00Z", { locale: "en-GB", timeZone: "UTC" }, now)).toBe("3 Mar 2025 06:12 UTC");
  });

  it("uses the viewer's day, not the server's, around midnight", () => {
    const text = formatDateTime("2026-11-21T23:30:00Z", { locale: "en-GB", timeZone: "Asia/Kolkata" }, now);
    expect(text).toMatch(/^22 Nov 05:00 /);
  });

  it("says Unknown for a date it cannot read", () => {
    expect(formatDateTime("yesterday", { locale: "en-GB", timeZone: "UTC" }, now)).toBe("Unknown");
  });
});

describe("formatMonth", () => {
  it("turns a ledger period into a month name", () => {
    expect(formatMonth("2026-11", "en-US")).toBe("Nov 2026");
  });

  it("leaves anything else as it came", () => {
    expect(formatMonth("all time", "en-US")).toBe("all time");
  });
});
