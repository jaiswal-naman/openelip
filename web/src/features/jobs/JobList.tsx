import { JobCard } from "./JobCard";
import styles from "./jobs.module.css";
import type { Job } from "./types";

/** A ledger of results. */
export function JobList({ jobs }: { jobs: Job[] }) {
  return (
    <ul className={styles.list}>
      {jobs.map((job) => (
        <JobCard key={job.id} job={job} />
      ))}
    </ul>
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
