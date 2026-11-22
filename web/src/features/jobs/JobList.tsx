import { ExternalLinkIcon } from "@/components/icons/icons";
import { buttonClass } from "@/components/ui/buttonStyles";
import { LocalTime } from "@/components/ui/Viewer";
import { type Job, locationText, remoteText } from "./jobs";
import styles from "./jobs.module.css";

/** A ledger of results. Rows carry no fit claim: they say what the posting says, where it is, and when it was last seen open. */
export function JobList({ jobs }: { jobs: Job[] }) {
  return (
    <ul className={styles.list}>
      {jobs.map((job) => (
        <JobRow key={job.id} job={job} />
      ))}
    </ul>
  );
}

function JobRow({ job }: { job: Job }) {
  return (
    <li className={styles.row}>
      <div className={styles.main}>
        <h2 className={`t-card ${styles.title}`}>{job.title}</h2>
        <p>{job.companyName ?? "Company not stated"}</p>
        <p className={styles.meta}>
          <span>{locationText(job)}</span>
          <span>{remoteText(job.isRemote)}</span>
        </p>
      </div>
      <div className={styles.side}>
        <p className="t-data">
          {job.lastVerifiedAt === null ? (
            "Last check not recorded"
          ) : (
            <>
              Verified open <LocalTime iso={job.lastVerifiedAt} />
            </>
          )}
        </p>
        {job.applyUrl === null ? (
          <p className="t-small">No link in the posting</p>
        ) : (
          <a
            href={job.applyUrl}
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

/** Rows the size of the real ones, shown only while the first page loads after first paint. */
export function JobsSkeleton() {
  return (
    <ul className={styles.list} aria-hidden="true">
      {[0, 1, 2].map((index) => (
        <li key={index} className={styles.skeleton}>
          <span className={styles.bar} style={{ width: "55%" }} />
          <span className={styles.bar} style={{ width: "30%" }} />
          <span className={styles.bar} style={{ width: "40%" }} />
        </li>
      ))}
    </ul>
  );
}
