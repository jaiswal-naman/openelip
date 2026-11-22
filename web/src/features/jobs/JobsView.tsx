"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { EmptyState, ErrorState, ProgressLine } from "@/components/ui/States";
import { Count } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { hasFilters, LIMIT_STEPS, PAGE_SIZE, type SearchParams } from "./jobs";
import styles from "./jobs.module.css";
import { JobList, JobsSkeleton } from "./JobList";
import { jobsQuery, openCountQuery } from "./queries";
import { SearchForm } from "./SearchForm";

/**
 * The search form and its results. The page remounts this with a new `key` whenever the URL's search changes, so
 * "Show more" starts again at one page for a new search.
 */
export function JobsView({ params }: { params: SearchParams }) {
  const api = useApi();
  const [limit, setLimit] = useState<number>(PAGE_SIZE);
  // The previous rows stay on screen while a longer list loads, under the thin progress line.
  const jobs = useQuery({ ...jobsQuery(api, params, limit), placeholderData: keepPreviousData });
  const openCount = useQuery(openCountQuery(api));

  const rows = jobs.data ?? [];
  const nextLimit = LIMIT_STEPS.find((step) => step > limit);
  const mayHaveMore = rows.length >= limit; // a full page: there could be more
  const filtered = hasFilters(params);

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
        <ProgressLine active={jobs.isFetching} />
        <p role="status" className="sr-only">
          {jobs.isFetching ? "Loading jobs" : jobs.data === undefined ? "" : `${rows.length} jobs shown`}
        </p>

        {jobs.isError && <ErrorState what="Could not load jobs." error={jobs.error} onRetry={() => void jobs.refetch()} />}

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

        {rows.length > 0 && mayHaveMore && (
          <div className={styles.more}>
            {nextLimit === undefined ? (
              <p className="t-small">Showing the first {limit}. Narrow the search to see others.</p>
            ) : (
              <Button isPending={jobs.isFetching} onPress={() => setLimit(nextLimit)}>
                Show more
              </Button>
            )}
          </div>
        )}
      </section>
    </>
  );
}
