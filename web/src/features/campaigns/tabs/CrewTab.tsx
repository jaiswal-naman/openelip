import { CrewPlate } from "@/components/ui/CrewPlate";
import { Pill } from "@/components/ui/Pill";
import { LocalTime } from "@/components/ui/Viewer";
import { STEP_STATUS_LABELS } from "@/lib/labels";
import type { CampaignDetail } from "../queries";
import styles from "../campaigns.module.css";

const STEP_TONE: Record<string, "ok" | "warn" | "bad" | "unk"> = { done: "ok", running: "warn", waiting: "warn", failed: "bad" };

/** Each agent step: the crew member's plate with its duty, what state it is in, what it waits for, and what it needs from the candidate. The page refreshes every 15 seconds. */
export function CrewTab({ steps }: { steps: CampaignDetail["steps"] }) {
  if (steps.length === 0) return <p className="muted">The crew has not started yet.</p>;
  return (
    <ul className={styles.rows} aria-label="Crew steps">
      {steps.map((step) => (
        <li key={step.step_key} className={styles.crewRow}>
          <CrewPlate role={step.agent_role} withDuty />
          <div className={styles.crewText}>
            {step.todo === null ? <p>{step.text}</p> : <p className={styles.todo}>To do: {step.text}</p>}
            {step.status === "waiting" && step.due_at !== null && (
              <p className="muted">
                Waiting until <LocalTime iso={step.due_at} />
              </p>
            )}
          </div>
          <Pill tone={STEP_TONE[step.status] ?? "unk"}>{STEP_STATUS_LABELS[step.status] ?? step.status}</Pill>
        </li>
      ))}
    </ul>
  );
}
