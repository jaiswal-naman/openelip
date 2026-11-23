import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import { PAGE_SIZE, parseOpenJobCount, type SearchParams } from "./jobs";
import type { Job } from "./types";

export const jobsKeys = {
  list: (params: SearchParams) => ["jobs", "list", params] as const,
  openCount: ["jobs", "open-count"] as const,
};

/**
 * One page of results: its rows, whether meaning was searched as well as keywords, and the cursor for the page after it
 * (`null` on the last). The mode is `null` when the answer does not say, and the page then claims nothing.
 */
export type JobsPage = { rows: Job[]; mode: "hybrid" | "keyword" | null; nextCursor: string | null };

const modeOf = (value: unknown): JobsPage["mode"] => (value === "hybrid" || value === "keyword" ? value : null);
const cursorOf = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

async function fetchPage(api: Api, params: SearchParams, cursor: string | null, signal: AbortSignal): Promise<JobsPage> {
  const query = {
    q: params.q || undefined,
    country: params.country ?? undefined,
    remote: params.remote ?? undefined,
    limit: PAGE_SIZE,
    cursor: cursor ?? undefined,
  };
  const pending = api.GET("/v1/jobs", { params: { query }, signal });
  const rows = await unwrap(pending); // throws an ApiFailure unless the call succeeded
  const meta = (await pending).data?.meta; // `unwrap` returns only the envelope's data; the page needs its meta too
  return { rows, mode: modeOf(meta?.mode), nextCursor: cursorOf(meta?.next_cursor) };
}

/**
 * The pages of one search. The cursor is the API's and belongs to that search, so it lives in the query cache and never
 * in the address. The server prefetches the first page with this same function, so the keys match.
 */
export function jobsQuery(api: Api, params: SearchParams) {
  return infiniteQueryOptions({
    queryKey: jobsKeys.list(params),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchPage(api, params, pageParam, signal),
    getNextPageParam: (lastPage) => lastPage.nextCursor,
  });
}

/** How many open jobs the index holds, read from the API at request time. */
export function openCountQuery(api: Api) {
  return queryOptions({
    queryKey: jobsKeys.openCount,
    queryFn: async ({ signal }) => parseOpenJobCount(await unwrap(api.GET("/v1/stats", { signal }))),
  });
}
