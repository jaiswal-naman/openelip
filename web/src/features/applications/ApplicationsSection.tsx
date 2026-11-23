"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Pill } from "@/components/ui/Pill";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { APPLICATION_STATUS_LABELS } from "@/lib/labels";
import { applicationsQuery, type ApplicationStatus } from "./queries";
import { RecordStatusMenu } from "./RecordStatusMenu";
import styles from "./applications.module.css";

const TONE: Record<ApplicationStatus, "ok" | "warn" | "bad" | "unk"> = {
  applied: "unk",
  interview: "warn",
  offer: "ok",
  rejected: "bad",
  no_response: "unk",
};

/** Every application as a ruled row: the job, the history behind it, and its status stamp with the menu to record the next one (APP-01). Tailoring and packets are on the match page. */
export function ApplicationsSection() {
  const api = useApi();
  const applications = useQuery(applicationsQuery(api));

  return (
    <section aria-labelledby="applications-heading" className={styles.section}>
      <h2 id="applications-heading" className="t-section">
        Applications
      </h2>
      {applications.isPending ? (
        <p role="status">Loading...</p>
      ) : applications.isError ? (
        <ErrorState what="Could not load your applications." error={applications.error} onRetry={() => void applications.refetch()} />
      ) : applications.data.length === 0 ? (
        <EmptyState action={<Link href="/app/today?view=matches">See your matches</Link>}>
          You have not recorded any applications. Open a match, tailor your resume and record when you apply.
        </EmptyState>
      ) : (
        <ul className={styles.list}>
          {applications.data.map((application) => (
            <li key={application.id} className={styles.item}>
              <div className={styles.main}>
                <h3 className="t-card">{application.job_title}</h3>
                <p className="muted">{application.company_name.trim() === "" ? "Company not stated" : application.company_name}</p>
              </div>
              <ol className={styles.history} aria-label={`History of ${application.job_title}`}>
                {application.events.map((event, index) => (
                  <li key={`${event.status}-${index}`}>
                    {APPLICATION_STATUS_LABELS[event.status]}: <LocalTime iso={event.occurred_at} />
                  </li>
                ))}
              </ol>
              <div className={styles.side}>
                <Pill tone={TONE[application.status]}>{APPLICATION_STATUS_LABELS[application.status]}</Pill>
                <RecordStatusMenu applicationId={application.id} title={application.job_title} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
