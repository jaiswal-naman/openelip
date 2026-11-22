import { describe, expect, it } from "vitest";
import { ApiFailure } from "@/lib/api/errors";
import {
  hasFilters,
  locationText,
  parseJobs,
  parseOpenJobCount,
  parseSearch,
  remoteText,
  safeHttpUrl,
  searchToQueryString,
} from "./jobs";

// Invented rows in the shape /v1/jobs returns
const row = (overrides: Record<string, unknown> = {}) => ({
  id: 7,
  title: "Platform Engineer",
  company_name: "Example Co",
  location_raw: "Pune, India",
  country: "IN",
  city: "Pune",
  is_remote: false,
  apply_url: "https://jobs.example.test/7",
  posted_at: null,
  last_verified_at: "2026-11-22T12:21:54.339375+00:00",
  content_version: 1,
  ...overrides,
});

describe("parseJobs", () => {
  it("reads the fields the screen shows", () => {
    expect(parseJobs([row()])).toEqual([
      {
        id: 7,
        title: "Platform Engineer",
        companyName: "Example Co",
        locationRaw: "Pune, India",
        country: "IN",
        city: "Pune",
        isRemote: false,
        applyUrl: "https://jobs.example.test/7",
        lastVerifiedAt: "2026-11-22T12:21:54.339375+00:00",
      },
    ]);
  });

  it("keeps what the API does not state as null: never an empty string, a zero or a guess", () => {
    const [job] = parseJobs([row({ company_name: null, location_raw: "  ", country: null, city: "", is_remote: null, last_verified_at: null })]);
    expect(job).toMatchObject({ companyName: null, locationRaw: null, country: null, city: null, isRemote: null, lastVerifiedAt: null });
  });

  it("treats anything that is not true or false as an unknown remote state", () => {
    expect(parseJobs([row({ is_remote: "yes" })])[0].isRemote).toBeNull();
    expect(parseJobs([row({ is_remote: 1 })])[0].isRemote).toBeNull();
    expect(parseJobs([row({ is_remote: true })])[0].isRemote).toBe(true);
  });

  it("calls an answer that is not a list of jobs unreadable", () => {
    expect(() => parseJobs({ rows: [] })).toThrow(ApiFailure);
    expect(() => parseJobs([row({ id: "7" })])).toThrow(ApiFailure);
    expect(() => parseJobs([row({ title: "" })])).toThrow(ApiFailure);
    expect(() => parseJobs([null])).toThrow(ApiFailure);
  });
});

describe("safeHttpUrl", () => {
  it("accepts http and https and refuses every other scheme or a relative link (threat T-01-23)", () => {
    expect(safeHttpUrl("https://jobs.example.test/a?b=1")).toBe("https://jobs.example.test/a?b=1");
    expect(safeHttpUrl("http://jobs.example.test/")).toBe("http://jobs.example.test/");
    expect(safeHttpUrl("javascript:alert(document.cookie)")).toBeNull();
    expect(safeHttpUrl("JaVaScRiPt:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeHttpUrl("ftp://files.example.test/x")).toBeNull();
    expect(safeHttpUrl("/relative/path")).toBeNull();
    expect(safeHttpUrl("")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
  });
});

describe("parseOpenJobCount", () => {
  it("reads the number of open unique jobs", () => {
    expect(parseOpenJobCount({ open_postings: 11353, open_unique_jobs: 9996 })).toBe(9996);
  });

  it("refuses an answer without it, instead of showing a zero", () => {
    expect(() => parseOpenJobCount({ open_postings: 1 })).toThrow(ApiFailure);
    expect(() => parseOpenJobCount(null)).toThrow(ApiFailure);
  });
});

describe("locationText", () => {
  it("shows the posting's own words", () => {
    expect(locationText({ locationRaw: "Gurugram, India", city: "Gurugram", country: "IN" })).toBe("Gurugram, India");
  });

  it("does not turn a place the system could not place into a country", () => {
    expect(locationText({ locationRaw: "Stockholm", city: null, country: null })).toBe("Stockholm");
    expect(locationText({ locationRaw: "Middle East", city: null, country: null })).toBe("Middle East");
  });

  it("falls back to the parsed city and country when there are no such words", () => {
    expect(locationText({ locationRaw: null, city: "Pune", country: "IN" })).toBe("Pune, India");
    expect(locationText({ locationRaw: null, city: null, country: "IN" })).toBe("India");
  });

  it("says the location is not stated when nothing is: never a default", () => {
    expect(locationText({ locationRaw: null, city: null, country: null })).toBe("Location not stated");
  });
});

describe("remoteText", () => {
  it("has three honest states", () => {
    expect(remoteText(true)).toBe("Remote");
    expect(remoteText(false)).toBe("Not remote");
    expect(remoteText(null)).toBe("Remote: not stated");
  });
});

describe("the search in the URL", () => {
  it("reads query, country and remote", () => {
    expect(parseSearch({ q: "backend python", country: "in", remote: "true" })).toEqual({ q: "backend python", country: "IN", remote: true });
    expect(parseSearch({ remote: "false" }).remote).toBe(false);
  });

  it("drops what the API would refuse: an unknown country or remote value, and a query over 200 characters", () => {
    expect(parseSearch({ country: "XX", remote: "maybe" })).toEqual({ q: "", country: null, remote: null });
    expect(parseSearch({ q: "a".repeat(300) }).q).toHaveLength(200);
  });

  it("uses the first value when a parameter is repeated, and trims", () => {
    expect(parseSearch({ q: ["  go  ", "rust"] }).q).toBe("go");
  });

  it("writes the URL back without defaults, so a bare search stays /jobs", () => {
    expect(searchToQueryString({ q: "", country: null, remote: null })).toBe("");
    expect(searchToQueryString({ q: "backend python", country: "IN", remote: false })).toBe("q=backend+python&country=IN&remote=false");
  });

  it("knows whether any filter is set", () => {
    expect(hasFilters({ q: "", country: null, remote: null })).toBe(false);
    expect(hasFilters({ q: "", country: "IN", remote: null })).toBe(true);
  });
});
