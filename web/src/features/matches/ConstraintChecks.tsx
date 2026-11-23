import ledger from "@/components/ui/ledger.module.css";
import { Pill } from "@/components/ui/Pill";
import { CONSTRAINT_KIND_LABELS, CONSTRAINT_STATE_LABELS } from "@/lib/labels";
import type { ConstraintCheck } from "./queries";

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
    <table className={ledger.table}>
      <caption className="sr-only">Your hard constraints and whether this job meets them</caption>
      <thead>
        <tr>
          <th scope="col">Constraint</th>
          <th scope="col">What you set</th>
          <th scope="col">This job</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((check) => (
          <tr key={check.kind}>
            <th scope="row">{CONSTRAINT_KIND_LABELS[check.kind]}</th>
            <td data-label="What you set">{check.statement}</td>
            <td data-label="This job">
              <Pill tone={check.state === "satisfied" ? "ok" : "unk"}>{CONSTRAINT_STATE_LABELS[check.state]}</Pill>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
