import type { CampaignDetail, Prep } from "../queries";
import { DISCARD_REASON_LABELS } from "@/lib/labels";
import styles from "../campaigns.module.css";

/** Interview preparation (CMP-05): questions tied to a requirement, a gap framed honestly, and what to ask them. Text only. */
export function PrepTab({ prep }: { prep: CampaignDetail["prep"] }) {
  if (prep === null) return <p className="muted">Preparation starts when you record an interview.</p>;
  const pack = prep as unknown as Prep;
  const removed = Object.values(pack.removed_by_reason ?? {}).reduce((sum, n) => sum + n, 0);
  return (
    <div className={styles.stack}>
      <section aria-labelledby="prep-questions" className={styles.stack}>
        <h2 id="prep-questions" className="t-section">
          Questions they may ask
        </h2>
        {pack.questions.length === 0 ? (
          <p className="muted">No supported questions were written.</p>
        ) : (
          <ul className={styles.rows}>
            {pack.questions.map((question, index) => (
              <li key={`${index}:${question.text}`} className={styles.item}>
                <p className="t-card">{question.text}</p>
                {question.requirement_id != null && <p className="t-small muted">Tests a requirement of the job.</p>}
                {question.gap && <p className={styles.notice}>A gap in your record: {question.framing ?? "be honest about it and say what you would do."}</p>}
                {!question.gap && question.framing && <p>{question.framing}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="prep-ask" className={styles.stack}>
        <h2 id="prep-ask" className="t-section">
          Ask them
        </h2>
        {pack.ask_them.length === 0 ? (
          <p className="muted">Nothing to ask is supported by the company&apos;s pages.</p>
        ) : (
          <ul className={styles.rows}>
            {pack.ask_them.map((item, index) => (
              <li key={`${index}:${item.text}`} className={styles.item}>
                {item.text}
              </li>
            ))}
          </ul>
        )}
      </section>
      <p className="t-small muted">
        {removed === 0
          ? "Nothing was removed."
          : `${removed} ${removed === 1 ? "item was" : "items were"} removed because your record or the dossier does not support ${removed === 1 ? "it" : "them"}.`}{" "}
        {Object.entries(pack.removed_by_reason ?? {})
          .map(([code, count]) => `${DISCARD_REASON_LABELS[code] ?? code}: ${count}`)
          .join("; ")}
      </p>
    </div>
  );
}
