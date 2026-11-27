import { CheckIcon } from "@/components/icons/icons";
import { Button } from "@/components/ui/Button";
import { CrewPlate } from "@/components/ui/CrewPlate";
import { Pill } from "@/components/ui/Pill";
import { LocalTime } from "@/components/ui/Viewer";
import { STEP_STATUS_LABELS } from "@/lib/labels";
import type { CampaignDetail } from "../queries";
import styles from "../campaigns.module.css";

const STEP_TONE: Record<string, "ok" | "warn" | "bad" | "unk" | "stage"> = { done: "ok", running: "stage", waiting: "warn", failed: "bad" };

/** What waits for the person, as the panel's footer says it: the words, and the one action that opens the place to answer. */
export type CrewWaiting = { text: string; action: string; onOpen: () => void };

/**
 * The crew as a parallel-execution panel: one bordered column per agent step with the agent's plate and duty, its state, what
 * it is doing or what it produced (a done step's own words as a check-marked output row), and what it waits for. When the
 * person has an approval waiting, a footer strip says so and carries the one black action. The page refreshes every 15 seconds.
 */
export function CrewTab({ steps, waiting = null }: { steps: CampaignDetail["steps"]; waiting?: CrewWaiting | null }) {
  if (steps.length === 0) return <p className="muted">The crew has not started yet.</p>;
  return (
    <div className={styles.crewPanel} data-footer={waiting === null ? undefined : ""}>
      <div className={styles.crewClip}>
        <ul className={styles.crewGrid} aria-label="Crew steps">
          {steps.map((step) => (
            <li key={step.step_key} className={styles.crewCard}>
              <CrewPlate role={step.agent_role} withDuty />
              <div className={styles.crewText}>
                <Pill tone={STEP_TONE[step.status] ?? "unk"}>{STEP_STATUS_LABELS[step.status] ?? step.status}</Pill>
                {step.todo !== null ? (
                  <p className={styles.todo}>To do: {step.text}</p>
                ) : step.status === "done" ? (
                  <ul className={styles.outputs}>
                    <li className={styles.output}>
                      <CheckIcon />
                      <span>{step.text}</span>
                    </li>
                  </ul>
                ) : (
                  <p>{step.text}</p>
                )}
                {step.status === "waiting" && step.due_at !== null && (
                  <p className="muted">
                    Waiting until <LocalTime iso={step.due_at} />
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
      {waiting !== null && (
        <div className={styles.crewFoot}>
          <p className={styles.crewWaiting}>{waiting.text}</p>
          <Button variant="primary" size="sm" onPress={waiting.onOpen}>
            {waiting.action}
          </Button>
        </div>
      )}
    </div>
  );
}
