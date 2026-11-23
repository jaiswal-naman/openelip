import Link from "next/link";
import { ExternalLinkIcon } from "@/components/icons/icons";
import { buttonClass } from "@/components/ui/buttonStyles";
import { LocalTime } from "@/components/ui/Viewer";
import { arrangementsText, locationText, safeHttpUrl, sourceLabel, unplacedWords } from "./jobs";
import styles from "./jobs.module.css";
import type { Job } from "./types";

/**
 * One result, a row of the ledger. It carries no fit claim: it says what the posting says, where it is, which board it
 * came from and when it was last seen open (IDX-05). Every word of posting text is a React text node, never markup.
 */
export function JobCard({ job }: { job: Job }) {
  const applyUrl = safeHttpUrl(job.apply_url);
  const words = unplacedWords(job);
  return (
    <li className={styles.row}>
      <div className={styles.main}>
        <h2 className={`t-card ${styles.title}`}>
          <Link href={`/jobs/${job.id}`}>{job.title}</Link>
        </h2>
        <p>{job.company_name.trim() === "" ? "Company not stated" : job.company_name}</p>
        <p className={styles.meta}>
          <span>{locationText(job)}</span>
          {words !== null && <span>Posting says: {words}</span>}
          <span>{arrangementsText(job.work_arrangements)}</span>
        </p>
      </div>
      <div className={styles.side}>
        <p className="t-data">Source: {sourceLabel(job.source.ats)}</p>
        <p className="t-data">
          Verified open <LocalTime iso={job.last_verified_at} />
        </p>
        {applyUrl === null ? (
          <p className="t-small">No link in the posting</p>
        ) : (
          <a
            href={applyUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={buttonClass("secondary", "sm")}
            aria-label={`View posting for ${job.title} (opens in a new tab)`}
          >
            View posting
            <ExternalLinkIcon />
          </a>
        )}
      </div>
    </li>
  );
}
