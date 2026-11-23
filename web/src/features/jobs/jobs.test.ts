import { describe, expect, it } from "vitest";
import { ApiFailure } from "@/lib/api/errors";
import {
  arrangementsText,
  hasFilters,
  locationText,
  parseOpenJobCount,
  parseSearch,
  payText,
  safeHttpUrl,
  searchToQueryString,
  sourceLabel,
  unplacedWords,
} from "./jobs";

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
  it("shows the posting's own words when the place was read", () => {
    expect(locationText({ location_raw: "Gurugram, India", city: "Gurugram", country: "IN" })).toBe("Gurugram, India");
    expect(locationText({ location_raw: "India", city: null, country: "IN" })).toBe("India");
  });

  it("falls back to the parsed city and country when there are no such words", () => {
    expect(locationText({ location_raw: null, city: "Pune", country: "IN" })).toBe("Pune, India");
    expect(locationText({ location_raw: null, city: null, country: "IN" })).toBe("India");
  });

  it("says Location: unknown when neither a country nor a city was read, whatever the posting wrote (IDX-06)", () => {
    expect(locationText({ location_raw: null, city: null, country: null })).toBe("Location: unknown");
    expect(locationText({ location_raw: "Middle East", city: null, country: null })).toBe("Location: unknown");
  });
});

describe("unplacedWords", () => {
  it("keeps what the posting wrote beside an unknown place, so nothing the board said is hidden", () => {
    expect(unplacedWords({ location_raw: "  Middle East ", city: null, country: null })).toBe("Middle East");
  });

  it("has nothing to add when the place was read, or when the posting wrote nothing", () => {
    expect(unplacedWords({ location_raw: "Pune, India", city: "Pune", country: "IN" })).toBeNull();
    expect(unplacedWords({ location_raw: "   ", city: null, country: null })).toBeNull();
    expect(unplacedWords({ location_raw: null, city: null, country: null })).toBeNull();
  });
});

describe("arrangementsText", () => {
  it("says Remote: not stated when the posting does not say, never a guess", () => {
    expect(arrangementsText([])).toBe("Remote: not stated");
  });

  it("names what the posting allows", () => {
    expect(arrangementsText(["remote"])).toBe("Remote");
    expect(arrangementsText(["onsite"])).toBe("On-site");
    expect(arrangementsText(["hybrid"])).toBe("Hybrid");
  });

  it("joins several as alternatives, because the list is what the posting permits", () => {
    expect(arrangementsText(["onsite", "hybrid"])).toBe("On-site or Hybrid");
    expect(arrangementsText(["onsite", "hybrid", "remote"])).toMatch(/^On-site, Hybrid,? or Remote$/);
  });
});

describe("payText", () => {
  const plain = (text: string) => text.replaceAll(" ", " "); // Intl puts a no-break space after the currency code

  it("says Not stated in the posting when there is no pay", () => {
    expect(payText(null)).toBe("Not stated in the posting");
  });

  it("states the range with its currency code and the period (FRONTEND_UI 6.5)", () => {
    expect(plain(payText({ min: 120000, max: 150000, currency: "USD" }, "en-US"))).toBe("USD 120,000 to USD 150,000 a year");
  });

  it("says From or Up to when only one end is stated, and groups digits the viewer's way", () => {
    expect(plain(payText({ min: 3000000, max: null, currency: "INR" }, "en-IN"))).toBe("From INR 30,00,000 a year");
    expect(plain(payText({ min: null, max: 150000, currency: "USD" }, "en-US"))).toBe("Up to USD 150,000 a year");
  });

  it("shows a currency code Intl will not format as it came, instead of failing the page", () => {
    expect(payText({ min: 100, max: null, currency: "US$" }, "en-US")).toBe("From US$ 100 a year");
  });
});

describe("sourceLabel", () => {
  it("names the three boards", () => {
    expect(sourceLabel("greenhouse")).toBe("Greenhouse");
    expect(sourceLabel("lever")).toBe("Lever");
    expect(sourceLabel("ashby")).toBe("Ashby");
  });

  it("shows a source this page has no name for as it came", () => {
    expect(sourceLabel("workday")).toBe("workday");
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

  it("never reads a cursor from the address: the cursor belongs to one search and lives only in memory", () => {
    expect(parseSearch({ q: "go", cursor: "abc" })).toEqual({ q: "go", country: null, remote: null });
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
