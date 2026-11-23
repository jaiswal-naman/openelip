"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import ledger from "@/components/ui/ledger.module.css";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { eventsPage, type RecordEvent, recordKeys } from "./api";
import { constraintSubject, entityLabel, factKindLabel, factStatusLabel, operationLabel, originLabel, sourceLabel } from "./labels";
import styles from "./record.module.css";

type Snapshot = Record<string, unknown> | null;
const text = (value: unknown) => (typeof value === "string" ? value : "");

/** "Changed salary floor", "Added fact": what happened, in words, to which part of the record. */
function whatChanged(event: RecordEvent): string {
  if (event.status === "declined") return "Declined a proposed change";
  if (event.status === "expired") return "A proposed change expired";
  const snapshot: Snapshot = event.after ?? event.before;
  const subject = event.entity === "constraint" ? constraintSubject(text(snapshot?.kind)) : entityLabel(event.entity).toLowerCase();
  return `${operationLabel(event.operation)} ${subject}`;
}

/** One side of a change. A fact that was only confirmed or rejected has the same words before and after, so it shows its status instead. */
function side(event: RecordEvent, which: "before" | "after"): string {
  const snapshot: Snapshot = event[which];
  if (snapshot === null) return which === "before" ? "Did not exist" : "Removed";
  if (event.entity === "fact" && (event.operation === "confirm" || event.operation === "reject")) return factStatusLabel(text(snapshot.status));
  const words = text(snapshot.statement);
  return event.entity === "fact" ? `${factKindLabel(text(snapshot.kind))}: ${words}` : words;
}

/** Where the change came from: the resume for a fact it read, otherwise who made the change (REC-05). */
function whereFrom(event: RecordEvent): string {
  if (event.status === "declined" || event.status === "expired") return event.origin ? originLabel(event.origin) : "A proposal";
  if (event.entity === "fact" && (event.before?.source === "resume" || event.after?.source === "resume")) return sourceLabel("resume");
  return event.source ? sourceLabel(event.source) : "Unknown";
}

/**
 * Every change to the record, newest first, with what it was before, what it became, where it came from and when
 * (REC-05). It reads the log in pages and refetches after any write on the page, so an edit shows without a reload.
 */
export function ChangeLog() {
  const api = useApi();
  const log = useInfiniteQuery({
    queryKey: recordKeys.events,
    queryFn: ({ pageParam, signal }) => eventsPage(api, pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
  });

  if (log.isError) return <ErrorState what="Could not load the change log." error={log.error} onRetry={() => void log.refetch()} />;
  if (!log.data) {
    return (
      <p role="status" className="muted">
        Loading the change log.
      </p>
    );
  }
  const events = log.data.pages.flatMap((page) => page.events);
  if (events.length === 0) return <EmptyState>Nothing has changed yet. Every change you make or confirm is listed here.</EmptyState>;

  return (
    <div className={styles.group}>
      <table className={ledger.table}>
        <caption className="sr-only">Change log, newest first</caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">What changed</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
            <th scope="col">Source</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id}>
              <th scope="row" className={styles.when}>
                <LocalTime iso={event.created_at} />
              </th>
              <td data-label="What changed">{whatChanged(event)}</td>
              <td data-label="Before" className={styles.words}>
                {side(event, "before")}
              </td>
              <td data-label="After" className={styles.words}>
                {side(event, "after")}
              </td>
              <td data-label="Source">{whereFrom(event)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {log.hasNextPage && (
        <div>
          <Button isPending={log.isFetchingNextPage} onPress={() => void log.fetchNextPage()}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
