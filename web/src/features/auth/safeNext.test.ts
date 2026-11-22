import { describe, expect, it } from "vitest";
import { safeNextPath } from "./safeNext";

describe("safeNextPath (threat T-01-21: no open redirect after sign-in)", () => {
  it.each(["/", "/jobs", "/jobs?q=backend+python&country=IN", "/control/costs", "/trust#top", "/a/b%20c"])(
    "accepts a path on this site: %s",
    (path) => {
      expect(safeNextPath(path)).toBe(path);
    },
  );

  it.each([
    ["another host", "https://evil.example/path"],
    ["a protocol-relative URL", "//evil.example"],
    ["a backslash read as a slash", "/\\evil.example"],
    ["a doubled backslash", "\\\\evil.example"],
    ["a tab a browser deletes before reading", "/\t/evil.example"],
    ["a newline a browser deletes before reading", "/\n/evil.example"],
    ["a carriage return", "/\r/evil.example"],
    ["a null byte", "/\u0000/evil.example"],
    ["a javascript scheme", "javascript:alert(1)"],
    ["a data scheme", "data:text/html,<script>alert(1)</script>"],
    ["a relative path with no leading slash", "jobs"],
    ["an empty string", ""],
  ])("refuses %s", (_name, value) => {
    expect(safeNextPath(value)).toBe("/jobs");
  });

  it("goes to /jobs when there is nothing to go back to", () => {
    expect(safeNextPath(undefined)).toBe("/jobs");
    expect(safeNextPath(null)).toBe("/jobs");
  });

  it("uses the first value when the parameter is repeated, and still checks it", () => {
    expect(safeNextPath(["/trust", "https://evil.example"])).toBe("/trust");
    expect(safeNextPath(["//evil.example", "/trust"])).toBe("/jobs");
  });

  it("does not send someone back to the sign-in page they are on", () => {
    expect(safeNextPath("/sign-in")).toBe("/jobs");
    expect(safeNextPath("/sign-in?next=/trust")).toBe("/jobs");
  });
});
