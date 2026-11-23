import Link from "next/link";
import { useId } from "react";
import { LocalTime } from "@/components/ui/Viewer";
import { arrangementsText, locationText, sourceLabel, unplacedWords } from "@/features/jobs/jobs";
import { MATCH_REASON_LABELS, UNVERIFIED_LABELS } from "@/lib/labels";
import type { MatchView } from "./queries";
import styles from "./today.module.css";

/**
 * One of today's matches (MAT-02, FRONTEND_UI 3.3): the job, what it meets, what the posting leaves unsaid about the
 * constraints, where it came from and when it was last seen open. The words come from the stored match; none of it is
 * generated here. A job shown again because its posting changed says what changed (MAT-07). Everything from the posting is
 * rendered as text, never as markup (threat T-04-31). The title is a level 2 heading on Today; a page that puts the cards
 * under a heading of its own (onboarding step 6) asks for level 3, so the outline stays in order.
 */
export function MatchCard({ match, headingLevel = 2 }: { match: MatchView; headingLevel?: 2 | 3 }) {
  const headingId = useId();
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const { job } = match;
  const words = unplacedWords(job);
  return (
    <article className={styles.card} aria-labelledby={headingId}>
      <div className={styles.cardMain}>
        <Heading id={headingId} className={`t-card ${styles.cardTitle}`}>
          <Link href={`/app/matches/${match.decision_id}`}>{job.title}</Link>
        </Heading>
        <p className={styles.company}>{job.company_name.trim() === "" ? "Company not stated" : job.company_name}</p>
        <p className={styles.meta}>
          <span>{locationText(job)}</span>
          {words !== null && <span>Posting says: {words}</span>}
          <span>{arrangementsText(job.work_arrangements)}</span>
        </p>
        <p>{match.summary}</p>
        {match.reason_codes.includes("material_change") && (
          <p className={styles.changed}>
            <span className={`t-label ${styles.changedLabel}`}>{MATCH_REASON_LABELS.material_change}</span>
            <span>{match.change_note ?? "The posting changed."}</span>
          </p>
        )}
        {match.unverified.length > 0 && (
          <ul className={styles.notes} aria-label="Not stated in the posting">
            {match.unverified.map((flag) => (
              <li key={flag}>{UNVERIFIED_LABELS[flag]}</li>
            ))}
          </ul>
        )}
      </div>
      <div className={styles.cardSide}>
        <p className="t-small muted">Source: {sourceLabel(job.source.ats)}</p>
        <p className="t-small muted">
          Verified open <LocalTime iso={job.last_verified_at} />
        </p>
      </div>
    </article>
  );
}
