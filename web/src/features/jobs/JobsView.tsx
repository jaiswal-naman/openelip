"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { LinkButton } from "@/components/ui/LinkButton";
import { EmptyState, ErrorState, ProgressLine } from "@/components/ui/States";
import { Count } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { hasFilters, type SearchParams } from "./jobs";
import styles from "./jobs.module.css";
import { JobList, JobsSkeleton } from "./JobList";
import { LoadMore } from "./LoadMore";
import { jobsQuery, openCountQuery } from "./queries";
import { SearchForm } from "./SearchForm";

/**
 * The search form and its results. The page remounts this with a new `key` whenever the URL's search changes, so a new
 * search starts again at its first page. "Load more" adds the next page by the API's cursor, which stays out of the URL.
 */
export function JobsView({ params }: { params: SearchParams }) {
  const api = useApi();
  const jobs = useInfiniteQuery(jobsQuery(api, params));
  const openCount = useQuery(openCountQuery(api));

  const rows = jobs.data?.pages.flatMap((page) => page.rows) ?? [];
  const keywordOnly = jobs.data?.pages[0]?.mode === "keyword";
  const filtered = hasFilters(params);
  const nextPageFailed = jobs.isFetchNextPageError;

  return (
    <>
      <SearchForm params={params} />

      <section className={styles.results} aria-label="Results" aria-busy={jobs.isFetching}>
        <div className={`t-data ${styles.toolbar}`}>
          <span>{jobs.data === undefined ? "" : `Showing ${rows.length} ${rows.length === 1 ? "job" : "jobs"}`}</span>
          <span>
            {openCount.isError ? (
              "Index size not available"
            ) : openCount.data === undefined ? (
              ""
            ) : (
              <>
                <Count value={openCount.data} /> open jobs in the index
              </>
            )}
          </span>
        </div>
        {keywordOnly && <p className="t-small muted">Keyword results only right now.</p>}
        <ProgressLine active={jobs.isFetching} />
        <p role="status" className="sr-only">
          {jobs.isFetching ? "Loading jobs" : jobs.data === undefined ? "" : `${rows.length} jobs shown`}
        </p>

        {jobs.isError && !nextPageFailed && (
          <ErrorState what="Could not load jobs." error={jobs.error} onRetry={() => void jobs.refetch()} />
        )}

        {jobs.data === undefined && jobs.isPending && <JobsSkeleton />}

        {jobs.data !== undefined && rows.length === 0 && !jobs.isError && (
          <EmptyState
            action={
              filtered ? (
                <LinkButton href="/jobs" size="sm">
                  Clear the search
                </LinkButton>
              ) : undefined
            }
          >
            {filtered
              ? "No open jobs match this search. Try fewer words, or clear the filters."
              : "The job index has no open jobs yet. The person who runs this server can fill it with python -m app.cli ingest run."}
          </EmptyState>
        )}

        {rows.length > 0 && <JobList jobs={rows} />}

        {rows.length > 0 && jobs.hasNextPage && (
          <LoadMore
            isPending={jobs.isFetchingNextPage}
            error={nextPageFailed ? jobs.error : null}
            onLoad={() => void jobs.fetchNextPage()}
          />
        )}
      </section>
    </>
  );
}
