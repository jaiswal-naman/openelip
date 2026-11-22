import { describe, expect, it } from "vitest";
import { COUNTRIES, countryName, isCountryCode } from "./countries";

describe("the ISO country list", () => {
  it("holds the 249 assigned alpha-2 codes, each once, each with its own name", () => {
    expect(COUNTRIES).toHaveLength(249);
    expect(new Set(COUNTRIES.map((country) => country.code)).size).toBe(249);
    expect(new Set(COUNTRIES.map((country) => country.name)).size).toBe(249);
    expect(COUNTRIES.every((country) => /^[A-Z]{2}$/.test(country.code))).toBe(true);
    expect(COUNTRIES.every((country) => country.name.trim() !== "")).toBe(true);
  });

  it("uses only real region codes, so a typo in a code shows up here", () => {
    // Intl.DisplayNames gives back the code itself for a region it does not know. It is used only to check the codes:
    // the names shown come from the list, because the server's and the browser's ICU data name some regions differently.
    const intl = new Intl.DisplayNames(["en"], { type: "region" });
    const unknown = COUNTRIES.filter((country) => intl.of(country.code) === country.code);
    expect(unknown).toEqual([]);
  });

  it("names the countries the index has most jobs in", () => {
    expect(countryName("IN")).toBe("India");
    expect(countryName("US")).toBe("United States");
    expect(countryName("GB")).toBe("United Kingdom");
  });

  it("uses one fixed name per region, whatever the engine's ICU data says", () => {
    // These four are named differently by Node's and Chrome's ICU data; that disagreement broke hydration once.
    expect(countryName("HK")).toBe("Hong Kong");
    expect(countryName("MO")).toBe("Macao");
    expect(countryName("PS")).toBe("Palestine");
    expect(countryName("FK")).toBe("Falkland Islands");
  });

  it("shows a code it does not know as it came, never a guess", () => {
    expect(countryName("ZZ")).toBe("ZZ");
    expect(isCountryCode("ZZ")).toBe(false);
    expect(isCountryCode("IN")).toBe(true);
  });
});
