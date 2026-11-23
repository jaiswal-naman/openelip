"use client";

import { ErrorState } from "@/components/ui/States";
import { type Preview, type RoleView, usePreview } from "./api";
import styles from "./hiring.module.css";

const COUNTS: { key: keyof Pick<Preview, "eligible_candidates" | "selected_to_ask" | "waiting_for_reply" | "said_yes" | "said_no_or_expired">; label: string }[] = [
  { key: "eligible_candidates", label: "Eligible" },
  { key: "selected_to_ask", label: "Selected to ask" },
  { key: "waiting_for_reply", label: "Waiting for reply" },
  { key: "said_yes", label: "Said yes" },
  { key: "said_no_or_expired", label: "Said no or expired" },
];

const asOf = (iso: string) => new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }) + " UTC";

/**
 * The anonymous preview (HIR-03): the count fields the API returns and nothing else, so no name, email or id can appear here
 * whatever the response holds. Small groups arrive as "fewer than 5" and are shown as sent. A draft has no counts, so the
 * route is not called for it.
 */
export function PreviewPanel({ role }: { role: Pick<RoleView, "id" | "status"> }) {
  const preview = usePreview(role);
  return (
    <section aria-labelledby="preview-heading" className={styles.card}>
      <h2 id="preview-heading" className="t-label">
        Anonymous preview (counts only, no identities)
      </h2>
      {role.status === "draft" ? (
        <p>Counts appear after publishing.</p>
      ) : preview.isError ? (
        <ErrorState what="Could not load the preview." error={preview.error} onRetry={() => void preview.refetch()} />
      ) : preview.data === undefined ? (
        <p role="status" className="muted">Loading the preview.</p>
      ) : (
        <>
          <dl className={styles.counts}>
            {COUNTS.map(({ key, label }) => (
              <div key={key} className={styles.count}>
                <dt className={styles.muted}>{label}</dt>
                <dd className={styles.countValue}>{String(preview.data[key])}</dd>
              </div>
            ))}
          </dl>
          <p className={`t-small ${styles.muted}`}>Small counts show as &apos;fewer than 5&apos;.</p>
          <p className={`t-small ${styles.muted}`}>{preview.data.as_of ? `As of ${asOf(preview.data.as_of)}` : "No candidates have been checked yet."}</p>
        </>
      )}
    </section>
  );
}
