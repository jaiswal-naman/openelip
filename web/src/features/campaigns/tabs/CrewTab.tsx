import { LocalTime } from "@/components/ui/Viewer";
import { AGENT_ROLE_LABELS, STEP_STATUS_LABELS } from "@/lib/labels";
import type { CampaignDetail } from "../queries";
import styles from "../campaigns.module.css";

/** Each agent step: who, what state, what it waits for, and what it needs from the candidate. The page refreshes every 15 seconds. */
export function CrewTab({ steps }: { steps: CampaignDetail["steps"] }) {
  if (steps.length === 0) return <p className="muted">The crew has not started yet.</p>;
  return (
    <ul className={styles.list} aria-label="Crew steps">
      {steps.map((step) => (
        <li key={step.step_key} className={styles.item}>
          <div className={styles.row}>
            <span className="t-card">{AGENT_ROLE_LABELS[step.agent_role] ?? step.agent_role}</span>
            <span>{STEP_STATUS_LABELS[step.status] ?? step.status}</span>
          </div>
          <p>{step.text}</p>
          {step.status === "waiting" && step.due_at !== null && (
            <p className="muted">
              Waiting until <LocalTime iso={step.due_at} />
            </p>
          )}
          {step.todo !== null && <p className={styles.notice}>To do: {step.text}</p>}
        </li>
      ))}
    </ul>
  );
}
