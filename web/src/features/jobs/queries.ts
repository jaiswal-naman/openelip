import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import { parseJobs, parseOpenJobCount, type SearchParams } from "./jobs";

export const jobsKeys = {
  list: (params: SearchParams, limit: number) => ["jobs", "list", params, limit] as const,
  openCount: ["jobs", "open-count"] as const,
};

/** One page of results for a search. The server prefetches the first page with this same function, so the keys match. */
export function jobsQuery(api: Api, params: SearchParams, limit: number) {
  return queryOptions({
    queryKey: jobsKeys.list(params, limit),
    queryFn: async ({ signal }) => {
      const query = { q: params.q || undefined, country: params.country ?? undefined, remote: params.remote ?? undefined, limit };
      return parseJobs(await unwrap(api.GET("/v1/jobs", { params: { query }, signal })));
    },
  });
}

/** How many open jobs the index holds, read from the API at request time. */
export function openCountQuery(api: Api) {
  return queryOptions({
    queryKey: jobsKeys.openCount,
    queryFn: async ({ signal }) => parseOpenJobCount(await unwrap(api.GET("/v1/stats", { signal }))),
  });
}
