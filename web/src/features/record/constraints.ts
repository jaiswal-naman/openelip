import { countryName } from "@/features/jobs/countries";
import type { NewConstraint, RecordConstraint } from "./api";

/**
 * Hard constraints as the intake asks for them (FRONTEND_UI 3.2 step 4): the seven kinds the matcher can check, in the
 * order the cards come, and the plain restatement each one reads back. The restatement is built here from the typed
 * value, with no model, and is also the statement stored with the constraint, so the record says what the person confirmed.
 */
export const CONSTRAINT_ORDER = [
  "location",
  "remote_policy",
  "work_authorization",
  "salary_floor",
  "employment_type",
  "seniority",
  "excluded_company",
] as const;
export type ConstraintKind = (typeof CONSTRAINT_ORDER)[number];

type Option = { value: string; label: string };
export const ARRANGEMENTS: Option[] = [
  { value: "onsite", label: "on-site" },
  { value: "hybrid", label: "hybrid" },
  { value: "remote", label: "remote" },
];
export const EMPLOYMENT_TYPES: Option[] = [
  { value: "full_time", label: "full-time" },
  { value: "part_time", label: "part-time" },
  { value: "contract", label: "contract" },
  { value: "internship", label: "internship" },
  { value: "temporary", label: "temporary" },
];
export const SENIORITIES: Option[] = [
  { value: "intern", label: "intern" },
  { value: "entry", label: "entry" },
  { value: "mid", label: "mid" },
  { value: "senior", label: "senior" },
  { value: "staff", label: "staff" },
  { value: "principal", label: "principal" },
  { value: "manager", label: "manager" },
  { value: "director", label: "director" },
  { value: "executive", label: "executive" },
];

const MAX_INT4 = 2_147_483_647; // the API stores a salary floor in a 32-bit integer

/** What the editor holds while it is open: every kind's fields, of which a kind uses its own. */
export type Draft = {
  countryCodes: string[];
  cities: string; // one per line: a city name can hold a comma
  arrangements: string[];
  employmentTypes: string[];
  seniorities: string[];
  amount: string;
  currency: string;
  period: "year" | "month";
  companies: string; // one per line
};

export const emptyDraft = (): Draft => ({
  countryCodes: [],
  cities: "",
  arrangements: [],
  employmentTypes: [],
  seniorities: [],
  amount: "",
  currency: "",
  period: "year",
  companies: "",
});

type Typed = Pick<
  NewConstraint,
  "country_codes" | "cities" | "arrangements" | "employment_types" | "seniorities" | "salary_floor" | "company_names"
>;

/** The editor's starting point: what is saved, or what a dealbreaker was suggested to mean. */
export function draftFromTyped(typed: Typed | RecordConstraint): Draft {
  return {
    ...emptyDraft(),
    countryCodes: typed.country_codes ?? [],
    cities: (typed.cities ?? []).join("\n"),
    arrangements: typed.arrangements ?? [],
    employmentTypes: typed.employment_types ?? [],
    seniorities: typed.seniorities ?? [],
    amount: typed.salary_floor ? String(typed.salary_floor.amount) : "",
    currency: typed.salary_floor?.currency ?? "",
    companies: (typed.company_names ?? []).join("\n"),
  };
}

export const splitLines = (text: string): string[] =>
  text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");

/** The salary floor in whole currency units per year, or null when the amount is not a whole positive number that fits. */
export function annualAmount(draft: Draft): number | null {
  const digits = draft.amount.replace(/[,\s]/g, "");
  if (!/^\d+$/.test(digits)) return null;
  const yearly = Number(digits) * (draft.period === "month" ? 12 : 1);
  return yearly > 0 && yearly <= MAX_INT4 ? yearly : null;
}

const labelsOf = (options: Option[], values: string[]) => options.filter((option) => values.includes(option.value)).map((option) => option.label);

/** "A", "A or B", "A, B or C" */
export function joinOr(items: string[]): string {
  return items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
}

/** "INR 30,00,000": INR groups the Indian way (30,00,000), anything else in the viewer's own locale (FRONTEND_UI 6.5). */
export function formatSalary(amount: number, currency: string, locale: string): string {
  return `${currency} ${new Intl.NumberFormat(currency === "INR" ? "en-IN" : locale, { maximumFractionDigits: 0 }).format(amount)}`;
}

export type Problem = { field: "country" | "amount" | "currency" | "choice" | "companies"; message: string };

/** What is missing or wrong, in plain words, or null when the draft can be saved. The API still has the last word. */
export function problemWith(kind: ConstraintKind, draft: Draft): Problem | null {
  switch (kind) {
    case "location":
    case "work_authorization":
      return draft.countryCodes.length === 0 ? { field: "country", message: "Choose at least one country." } : null;
    case "remote_policy":
      return draft.arrangements.length === 0 ? { field: "choice", message: "Choose at least one way of working you accept." } : null;
    case "employment_type":
      return draft.employmentTypes.length === 0 ? { field: "choice", message: "Choose at least one type of employment you accept." } : null;
    case "seniority":
      return draft.seniorities.length === 0 ? { field: "choice", message: "Choose at least one level you accept." } : null;
    case "excluded_company":
      return splitLines(draft.companies).length === 0 ? { field: "companies", message: "Write at least one company name." } : null;
    case "salary_floor":
      if (annualAmount(draft) === null) return { field: "amount", message: "Enter the amount as a whole number, for example 3000000." };
      if (!/^[A-Za-z]{3}$/.test(draft.currency.trim())) return { field: "currency", message: "Enter the currency as three letters, for example INR." };
      return null;
  }
}

/** What the agent reads back, in the second person: "You will never see jobs that pay less than INR 30,00,000 a year." Null while the draft is incomplete. */
export function restatement(kind: ConstraintKind, draft: Draft, locale: string): string | null {
  if (problemWith(kind, draft) !== null) return null;
  switch (kind) {
    case "location": {
      const cities = splitLines(draft.cities);
      const countries = joinOr(draft.countryCodes.map(countryName));
      return cities.length > 0 ? `You will only see jobs in ${joinOr(cities)}, ${countries}.` : `You will only see jobs in ${countries}.`;
    }
    case "work_authorization":
      return `You will only see jobs in ${joinOr(draft.countryCodes.map(countryName))}, where you are allowed to work.`;
    case "remote_policy":
      return `You will only see ${joinOr(labelsOf(ARRANGEMENTS, draft.arrangements))} jobs.`;
    case "employment_type":
      return `You will only see ${joinOr(labelsOf(EMPLOYMENT_TYPES, draft.employmentTypes))} jobs.`;
    case "seniority":
      return `You will only see jobs at ${joinOr(labelsOf(SENIORITIES, draft.seniorities))} level.`;
    case "excluded_company":
      return `You will never see jobs from ${joinOr(splitLines(draft.companies))}.`;
    case "salary_floor":
      return `You will never see jobs that pay less than ${formatSalary(annualAmount(draft) ?? 0, draft.currency.trim().toUpperCase(), locale)} a year.`;
  }
}

/** The request body for a draft, or the problem that stops it. A kind sends only its own typed fields. */
export function toBody(kind: ConstraintKind, draft: Draft, locale: string): { body: NewConstraint } | { problem: Problem } {
  const problem = problemWith(kind, draft);
  const statement = restatement(kind, draft, locale);
  if (problem || statement === null) return { problem: problem ?? { field: "choice", message: "Something is missing." } };
  const typed: Typed = {};
  if (kind === "location") {
    typed.country_codes = draft.countryCodes;
    if (splitLines(draft.cities).length > 0) typed.cities = splitLines(draft.cities);
  } else if (kind === "work_authorization") typed.country_codes = draft.countryCodes;
  else if (kind === "remote_policy") typed.arrangements = draft.arrangements as NonNullable<Typed["arrangements"]>;
  else if (kind === "employment_type") typed.employment_types = draft.employmentTypes as NonNullable<Typed["employment_types"]>;
  else if (kind === "seniority") typed.seniorities = draft.seniorities as NonNullable<Typed["seniorities"]>;
  else if (kind === "excluded_company") typed.company_names = splitLines(draft.companies);
  else typed.salary_floor = { amount: annualAmount(draft) ?? 0, currency: draft.currency.trim().toUpperCase() };
  return { body: { kind, statement, ...typed } };
}
