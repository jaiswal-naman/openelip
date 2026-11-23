import { describe, expect, it } from "vitest";
import { annualAmount, draftFromTyped, emptyDraft, formatSalary, joinOr, problemWith, restatement, splitLines, toBody } from "./constraints";

const draft = (over: Partial<ReturnType<typeof emptyDraft>>) => ({ ...emptyDraft(), ...over });

describe("restating a hard constraint from its typed value", () => {
  it("reads a salary floor back with the currency code, and groups INR the Indian way", () => {
    expect(restatement("salary_floor", draft({ amount: "3000000", currency: "INR" }), "en-US")).toBe(
      "You will never see jobs that pay less than INR 30,00,000 a year.",
    );
  });

  it("groups any other currency in the viewer's own locale", () => {
    expect(restatement("salary_floor", draft({ amount: "120000", currency: "usd" }), "en-US")).toBe(
      "You will never see jobs that pay less than USD 120,000 a year.",
    );
    expect(formatSalary(3000000, "EUR", "de-DE")).toBe("EUR 3.000.000");
  });

  it("turns a monthly figure into the yearly one the API stores, and says the yearly one", () => {
    const monthly = draft({ amount: "2,50,000", currency: "INR", period: "month" });
    expect(annualAmount(monthly)).toBe(3000000);
    expect(restatement("salary_floor", monthly, "en-US")).toBe("You will never see jobs that pay less than INR 30,00,000 a year.");
  });

  it("reads each of the other kinds back in plain words", () => {
    expect(restatement("location", draft({ countryCodes: ["IN"], cities: "Bengaluru\nPune" }), "en-US")).toBe("You will only see jobs in Bengaluru or Pune, India.");
    expect(restatement("location", draft({ countryCodes: ["IN", "AE"] }), "en-US")).toBe("You will only see jobs in India or United Arab Emirates.");
    expect(restatement("work_authorization", draft({ countryCodes: ["IN"] }), "en-US")).toBe("You will only see jobs in India, where you are allowed to work.");
    expect(restatement("remote_policy", draft({ arrangements: ["hybrid", "remote"] }), "en-US")).toBe("You will only see hybrid or remote jobs.");
    expect(restatement("employment_type", draft({ employmentTypes: ["contract", "full_time"] }), "en-US")).toBe("You will only see full-time or contract jobs.");
    expect(restatement("seniority", draft({ seniorities: ["senior", "staff", "principal"] }), "en-US")).toBe("You will only see jobs at senior, staff or principal level.");
    expect(restatement("excluded_company", draft({ companies: "Initech Sample Co\n\nGlobex Example Ltd" }), "en-US")).toBe(
      "You will never see jobs from Initech Sample Co or Globex Example Ltd.",
    );
  });

  it("keeps the options in their own order, not the order they were ticked in", () => {
    expect(restatement("remote_policy", draft({ arrangements: ["remote", "onsite"] }), "en-US")).toBe("You will only see on-site or remote jobs.");
  });

  it("says nothing while the value is incomplete", () => {
    expect(restatement("salary_floor", draft({ amount: "3000000" }), "en-US")).toBeNull();
    expect(restatement("location", draft({}), "en-US")).toBeNull();
  });
});

describe("what stops a draft being saved", () => {
  it("names the missing value of each kind in plain words", () => {
    expect(problemWith("location", draft({}))).toEqual({ field: "country", message: "Choose at least one country." });
    expect(problemWith("remote_policy", draft({}))?.field).toBe("choice");
    expect(problemWith("excluded_company", draft({ companies: " \n " }))?.field).toBe("companies");
  });

  it("refuses a salary that is not a whole positive number, one too large for the API, and a currency that is not three letters", () => {
    expect(problemWith("salary_floor", draft({ amount: "30.5", currency: "INR" }))?.field).toBe("amount");
    expect(problemWith("salary_floor", draft({ amount: "0", currency: "INR" }))?.field).toBe("amount");
    expect(problemWith("salary_floor", draft({ amount: "999999999999", currency: "INR" }))?.field).toBe("amount");
    expect(problemWith("salary_floor", draft({ amount: "300000000", currency: "INR", period: "month" }))?.field).toBe("amount"); // x12 does not fit
    expect(problemWith("salary_floor", draft({ amount: "3000000", currency: "RUPEES" }))).toEqual({
      field: "currency",
      message: "Enter the currency as three letters, for example INR.",
    });
    expect(problemWith("salary_floor", draft({ amount: "3000000", currency: "INR" }))).toBeNull();
  });
});

describe("the request body", () => {
  it("sends each kind only its own typed fields, with the restatement as the statement", () => {
    expect(toBody("salary_floor", draft({ amount: "3000000", currency: "inr" }), "en-US")).toEqual({
      body: {
        kind: "salary_floor",
        statement: "You will never see jobs that pay less than INR 30,00,000 a year.",
        salary_floor: { amount: 3000000, currency: "INR" },
      },
    });
    expect(toBody("location", draft({ countryCodes: ["IN"], cities: "Bengaluru" }), "en-US")).toEqual({
      body: { kind: "location", statement: "You will only see jobs in Bengaluru, India.", country_codes: ["IN"], cities: ["Bengaluru"] },
    });
    expect(toBody("location", draft({ countryCodes: ["IN"] }), "en-US")).toEqual({
      body: { kind: "location", statement: "You will only see jobs in India.", country_codes: ["IN"] },
    });
    expect(toBody("remote_policy", draft({ arrangements: ["remote", "hybrid"] }), "en-US")).toEqual({
      body: { kind: "remote_policy", statement: "You will only see hybrid or remote jobs.", arrangements: ["remote", "hybrid"] },
    });
    expect(toBody("excluded_company", draft({ companies: "Initech Sample Co" }), "en-US")).toEqual({
      body: { kind: "excluded_company", statement: "You will never see jobs from Initech Sample Co.", company_names: ["Initech Sample Co"] },
    });
  });

  it("returns the problem instead of a body for an incomplete draft", () => {
    expect(toBody("seniority", draft({}), "en-US")).toEqual({ problem: { field: "choice", message: "Choose at least one level you accept." } });
  });
});

describe("a saved constraint as a draft", () => {
  it("round-trips the typed values, losing no country", () => {
    const saved = draftFromTyped({ country_codes: ["IN", "AE"], cities: ["Pune"], salary_floor: { amount: 3000000, currency: "INR" }, company_names: ["Initech Sample Co"] });
    expect(saved.countryCodes).toEqual(["IN", "AE"]);
    expect(saved.cities).toBe("Pune");
    expect(saved.amount).toBe("3000000");
    expect(saved.currency).toBe("INR");
    expect(saved.companies).toBe("Initech Sample Co");
  });
});

describe("small text helpers", () => {
  it("joins with a comma and a final or", () => {
    expect(joinOr([])).toBe("");
    expect(joinOr(["a"])).toBe("a");
    expect(joinOr(["a", "b"])).toBe("a or b");
    expect(joinOr(["a", "b", "c"])).toBe("a, b or c");
  });

  it("splits a field into trimmed lines and drops the blank ones", () => {
    expect(splitLines("  Acme Co \r\n\n Globex Example Ltd ")).toEqual(["Acme Co", "Globex Example Ltd"]);
  });
});
