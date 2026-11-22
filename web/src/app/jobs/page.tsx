import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { JobsView } from "@/features/jobs/JobsView";
import { PAGE_SIZE, parseSearch, searchToQueryString } from "@/features/jobs/jobs";
import { jobsQuery, openCountQuery } from "@/features/jobs/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Open jobs" };

/**
 * The first page of results is fetched here, on the server, and handed to the client's cache, so a visit shows jobs
 * without a loading state. A failed fetch is not an error here: nothing is dehydrated and the client asks again,
 * showing its own error state with Retry if that fails too.
 */
export default async function JobsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = parseSearch(await searchParams);
  const queryClient = makeQueryClient();
  const api = await serverApi();
  await Promise.all([
    queryClient.prefetchQuery(jobsQuery(api, params, PAGE_SIZE)),
    queryClient.prefetchQuery(openCountQuery(api)),
  ]);

  return (
    <div className="container page">
      <header>
        <h1 className="t-page">Open jobs</h1>
        <p className="muted">Keyword search over open jobs from public company boards. No account needed.</p>
      </header>
      <HydrationBoundary state={dehydrate(queryClient)}>
        <JobsView key={searchToQueryString(params)} params={params} />
      </HydrationBoundary>
    </div>
  );
}
