import { ApiFailure } from "@/lib/api/errors";
import { countryName, isCountryCode } from "./countries";

/**
 * `/v1/jobs` and `/v1/stats` have no response model yet, so the generated types call their data `unknown`. These
 * functions read it with checks instead of casting it: a malformed row is an unreadable answer, and a value the API
 * does not state stays `null` (unknown stays unknown, FRONTEND_UI 6.3).
 */

export type Job = {
  id: number;
  title: string;
  companyName: string | null;
  locationRaw: string | null;
  country: string | null;
  city: string | null;
  isRemote: boolean | null;
  applyUrl: string | null;
  lastVerifiedAt: string | null;
};

const unreadable = () => new ApiFailure({ status: 200, code: "unreadable_response" });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/** Only http and https links are shown. A job board's text could hold `javascript:` or another scheme (threat T-01-23). */
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

function parseJob(row: unknown): Job {
  if (!isRecord(row) || typeof row.id !== "number" || text(row.title) === null) throw unreadable();
  return {
    id: row.id,
    title: text(row.title) ?? "",
    companyName: text(row.company_name),
    locationRaw: text(row.location_raw),
    country: text(row.country),
    city: text(row.city),
    isRemote: typeof row.is_remote === "boolean" ? row.is_remote : null,
    applyUrl: safeHttpUrl(row.apply_url),
    lastVerifiedAt: text(row.last_verified_at),
  };
}

export function parseJobs(data: unknown): Job[] {
  if (!Array.isArray(data)) throw unreadable();
  return data.map(parseJob);
}

/** The number of open, de-duplicated jobs in the index (`open_unique_jobs`). */
export function parseOpenJobCount(data: unknown): number {
  if (!isRecord(data) || typeof data.open_unique_jobs !== "number") throw unreadable();
  return data.open_unique_jobs;
}

/**
 * Where the job is. The posting's own words win, because they are exactly what the board stated; the parsed city and
 * country are used only when there are no such words; with neither, the location is not stated. Nothing is guessed:
 * "Stockholm" with no parsed country stays "Stockholm".
 */
export function locationText(job: Pick<Job, "locationRaw" | "city" | "country">): string {
  if (job.locationRaw !== null) return job.locationRaw;
  const parsed = [job.city, job.country === null ? null : countryName(job.country)].filter((part) => part !== null);
  return parsed.length > 0 ? parsed.join(", ") : "Location not stated";
}

export function remoteText(isRemote: boolean | null): string {
  if (isRemote === null) return "Remote: not stated";
  return isRemote ? "Remote" : "Not remote";
}

/** The three states of the remote filter, as they appear in the URL. */
export type RemoteFilter = "any" | "true" | "false";

export type SearchParams = { q: string; country: string | null; remote: boolean | null };

export const PAGE_SIZE = 20;
export const LIMIT_STEPS = [20, 50, 100] as const; // "Show more" raises the limit; the API allows 100 at most
export const MAX_QUERY_LENGTH = 200; // the API refuses longer

type RawParams = Record<string, string | string[] | undefined>;

const first = (value: string | string[] | undefined): string | undefined => (Array.isArray(value) ? value[0] : value);

/** What the URL says, cleaned: an unknown country or remote value is dropped instead of reaching the API as a 422. */
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
