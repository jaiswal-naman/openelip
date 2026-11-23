import styles from "./stamp.module.css";

/**
 * The ink APPROVED stamp. `landing` plays the one 180ms press, for the moment the person has just approved in this view;
 * a stamp that was already there when the page opened is simply drawn.
 */
export function ApprovedStamp({ landing = false }: { landing?: boolean }) {
  return <span className={landing ? `${styles.stamp} ${styles.landing}` : styles.stamp}>Approved</span>;
}
