"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { type RecorderRow, blockedReasonCode, recorderQuery } from "@/features/recorder/queries";
import { useApi } from "@/lib/api/context";
import { ACTION_STATUS_LABELS, AGENT_ROLE_LABELS, reasonText } from "@/lib/labels";
import styles from "../campaigns.module.css";

/** Rows the candidate cares about first: outside actions, state changes and blocked ones; model and tool calls are still listed. */
function lineOf(row: RecorderRow): string {
  if (row.kind === "outside_action") return `${row.summary} (${ACTION_STATUS_LABELS[row.status] ?? row.status})`;
  return row.summary;
}

/** Everything the agents did on this campaign, newest first, each row a link to its step in the flight recorder (GRD-03). */
export function TimelineTab({ campaignId }: { campaignId: string }) {
  const api = useApi();
  const rows = useQuery(recorderQuery(api, campaignId));
  if (rows.isPending) return <p role="status">Loading the timeline...</p>;
  if (rows.isError) return <ErrorState what="Could not load the timeline." error={rows.error} onRetry={() => void rows.refetch()} />;
  if (rows.data.length === 0) return <p className="muted">Nothing has happened yet.</p>;
  const newest = [...rows.data].reverse();
  return (
    <ol className={styles.list} aria-label="Timeline, newest first">
      {newest.map((row) => {
        const reason = blockedReasonCode(row);
        return (
          <li key={row.id} className={styles.item}>
            <div className={styles.row}>
              <span className="t-data muted">
                <LocalTime iso={row.created_at} />
              </span>
              <span className="t-card">{AGENT_ROLE_LABELS[row.agent_role] ?? row.agent_role}</span>
              {row.status === "blocked" && <span className={styles.blocked}>Blocked</span>}
            </div>
            <p>{lineOf(row)}</p>
            {reason !== null && <p>{reasonText(reason)}</p>}
            <Link href={`/control/recorder?campaign_id=${campaignId}&step=${row.id}`}>Open this step in the recorder</Link>
          </li>
        );
      })}
    </ol>
  );
}
