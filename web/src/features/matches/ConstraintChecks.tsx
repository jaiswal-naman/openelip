import { Pill } from "@/components/ui/Pill";
import { CONSTRAINT_KIND_LABELS, CONSTRAINT_STATE_LABELS } from "@/lib/labels";
import type { ConstraintCheck } from "./queries";
import styles from "./match.module.css";

/**
 * Each hard constraint the person set, and whether this job meets it (FRONTEND_UI 3.4). A job that breaks one never reaches
 * this page, so the answer is "Satisfied" or, when the posting does not state the field, "Not stated (unverified)": an
 * unknown value passes a constraint and is shown as unknown, never as a tick.
 */
export function ConstraintChecks({ checks }: { checks: ConstraintCheck[] }) {
  if (checks.length === 0) {
    return <p>You have not set a hard constraint. Not set (not enforced).</p>;
  }
  return (
    <div className={styles.tableWrap}>
      <table className={styles.plainTable}>
        <caption className="sr-only">Your hard constraints and whether this job meets them</caption>
        <thead>
          <tr>
            <th scope="col" className={`t-label ${styles.column}`}>
              Constraint
            </th>
            <th scope="col" className={`t-label ${styles.column}`}>
              What you set
            </th>
            <th scope="col" className={`t-label ${styles.column}`}>
              This job
            </th>
          </tr>
        </thead>
        <tbody>
          {checks.map((check) => (
            <tr key={check.kind}>
              <th scope="row" className={styles.cell}>
                {CONSTRAINT_KIND_LABELS[check.kind]}
              </th>
              <td className={styles.cell}>{check.statement}</td>
              <td className={styles.cell}>
                <Pill tone={check.state === "satisfied" ? "ok" : "unk"}>{CONSTRAINT_STATE_LABELS[check.state]}</Pill>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
