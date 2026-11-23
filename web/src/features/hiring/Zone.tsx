import type { ReactNode } from "react";
import styles from "./hiring.module.css";

/** One numbered zone of a form sheet: its number and title at the left, its controls at the right. */
export function Zone({ id, number, title, children }: { id: string; number: number; title: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className={styles.zone}>
      <div className={styles.zoneHead}>
        <span className={styles.zoneNo} aria-hidden="true">
          {number}
        </span>
        <h2 id={id} className={styles.zoneTitle}>
          {title}
        </h2>
      </div>
      <div className={styles.zoneBody}>{children}</div>
    </section>
  );
}
