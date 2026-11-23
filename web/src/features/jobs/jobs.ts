import { ApiFailure } from "@/lib/api/errors";
import { ATS_LABELS, LOCATION_UNKNOWN, NOT_STATED, REMOTE_NOT_STATED, WORK_ARRANGEMENT_LABELS } from "@/lib/labels";
import { countryName, isCountryCode } from "./countries";
import type { Job } from "./types";

/**
 * `/v1/jobs` has a response model, so its rows are typed from the generated schema (types.ts). `/v1/stats` has none,
 * so `parseOpenJobCount` reads it with checks instead of casting it. A value the API does not state stays `null` or
 * empty, and the words below say so: unknown stays unknown (FRONTEND_UI 6.3).
 */

const unreadable = () => new ApiFailure({ status: 200, code: "unreadable_response" });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/** Only http and https links are shown. A job board's text could hold `javascript:` or another scheme (threats T-01-23, T-02-35). */
export function safeHttpUrl(value: unknown): string | null {
  const candidate = text(value);
  if (candidate === null) return null;
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** The number of open, de-duplicated jobs in the index (`open_unique_jobs`). */
export function parseOpenJobCount(data: unknown): number {
  if (!isRecord(data) || typeof data.open_unique_jobs !== "number") throw unreadable();
  return data.open_unique_jobs;
}

type Place = Pick<Job, "location_raw" | "city" | "country">;

/**
 * Where the job is. When the parser read a country or a city, the posting's own words win, because they are exactly
 * what the board stated, and the parsed city and country are used only when there are no such words. When it read
 * neither, the place is unknown (IDX-06): nothing is guessed, and "Stockholm" with no parsed country stays that way.
 */
export function locationText(job: Place): string {
  if (job.country === null && job.city === null) return LOCATION_UNKNOWN;
  const words = text(job.location_raw);
  if (words !== null) return words;
  return [job.city, job.country === null ? null : countryName(job.country)].filter((part) => part !== null).join(", ");
}

/** What the posting wrote for a place the parser could not read, shown beside "Location: unknown" so nothing the board said is hidden. */
export function unplacedWords(job: Place): string | null {
  return job.country === null && job.city === null ? text(job.location_raw) : null;
}

const alternatives = new Intl.ListFormat("en", { type: "disjunction" });

/** What the posting allows. The list is "what the posting permits", so several are alternatives: "On-site or Hybrid". */
export function arrangementsText(arrangements: Job["work_arrangements"]): string {
  if (arrangements.length === 0) return REMOTE_NOT_STATED;
  return alternatives.format(arrangements.map((arrangement) => WORK_ARRANGEMENT_LABELS[arrangement] ?? arrangement));
}

/** "USD 120,000": the currency code and whole units, in the viewer's digit grouping (FRONTEND_UI 6.5). */
function money(value: number, currency: string, locale?: string): string {
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency, currencyDisplay: "code", maximumFractionDigits: 0 }).format(value);
  } catch {
    // Intl refuses a code that is not three letters ("US$"). It is shown as it came, so one odd posting cannot break its page.
    return `${currency} ${new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(value)}`;
  }
}

/** Annual pay as the posting states it, with its period. No pay is "Not stated in the posting", never a zero. */
export function payText(pay: Job["pay"], locale?: string): string {
  if (pay === null) return NOT_STATED;
  const amount = (value: number) => money(value, pay.currency, locale);
  if (pay.min !== null && pay.max !== null) {
    return pay.min === pay.max ? `${amount(pay.min)} a year` : `${amount(pay.min)} to ${amount(pay.max)} a year`;
  }
  if (pay.min !== null) return `From ${amount(pay.min)} a year`;
  if (pay.max !== null) return `Up to ${amount(pay.max)} a year`;
  return NOT_STATED;
}

/** "Greenhouse" for "greenhouse". A source this page has no name for is shown as it came. */
export function sourceLabel(ats: string): string {
  return (ATS_LABELS as Record<string, string>)[ats] ?? ats;
}

export type SearchParams = { q: string; country: string | null; remote: boolean | null };

/** The three states of the remote filter, as they appear in the URL. */
export type RemoteFilter = "any" | "true" | "false";

export const PAGE_SIZE = 20; // rows per page, for the first page and for every Load more
export const MAX_QUERY_LENGTH = 200; // the API refuses longer

type RawParams = Record<string, string | string[] | undefined>;

const first = (value: string | string[] | undefined): string | undefined => (Array.isArray(value) ? value[0] : value);

/**
 * What the URL says, cleaned: an unknown country or remote value is dropped instead of reaching the API as a 422. A
 * cursor is never read from the address: it belongs to one search and lives only in memory.
 */
export function parseSearch(raw: RawParams): SearchParams {
  const country = first(raw.country)?.trim().toUpperCase() ?? "";
  const remote = first(raw.remote);
  return {
    q: (first(raw.q) ?? "").trim().slice(0, MAX_QUERY_LENGTH),
    country: isCountryCode(country) ? country : null,
    remote: remote === "true" ? true : remote === "false" ? false : null,
  };
}

export function remoteFilterOf(remote: boolean | null): RemoteFilter {
  return remote === null ? "any" : remote ? "true" : "false";
}

/** The query string for the page URL; empty when nothing is set, so a bare search stays `/jobs`. */
export function searchToQueryString(params: SearchParams): string {
  const query = new URLSearchParams();
  if (params.q !== "") query.set("q", params.q);
  if (params.country !== null) query.set("country", params.country);
  if (params.remote !== null) query.set("remote", String(params.remote));
  return query.toString();
}

export const hasFilters = (params: SearchParams): boolean => searchToQueryString(params) !== "";
