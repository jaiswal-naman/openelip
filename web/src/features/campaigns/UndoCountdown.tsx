"use client";

import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { useViewer } from "@/lib/viewer";
import { DELIVERY_LABELS, messageFor } from "@/lib/labels";
import { ApiFailure } from "@/lib/api/errors";
import { type FollowUp, useUndo } from "./queries";
import { clockTime, countdownText, useServerNow } from "./time";
import styles from "./campaigns.module.css";

/**
 * A scheduled follow-up's undo window (GRD-04, FRONTEND_UI 4.5). The time left counts down to `undo_until` on the server's
 * clock. "Undone" shows only when the server says the action is cancelled; a refused undo shows the real state (Sending,
 * Sent, May have sent). One interval, and one live region that changes only when the state does, not every second.
 */
export function UndoCountdown({ followUp, offsetMs }: { followUp: FollowUp; offsetMs: number }) {
  const viewer = useViewer();
  const undo = useUndo();
  const until = followUp.undo_until === null ? null : Date.parse(followUp.undo_until);
  const delivery = followUp.delivery;
  const open = followUp.status === "scheduled" && until !== null && (delivery === null || delivery === "pending");
  const now = useServerNow(offsetMs, open);
  const remaining = until === null ? 0 : until - now;
  const undone = followUp.status === "cancelled" || delivery === "cancelled" || undo.data?.action.status === "cancelled";

  let state: string;
  let tone: "ok" | "warn" | "bad" | "unk";
  if (undone) [state, tone] = ["Undone", "unk"];
  else if (followUp.status === "executed" || delivery === "sent") [state, tone] = ["Sent", "ok"];
  else if (delivery === "unknown") [state, tone] = [DELIVERY_LABELS.unknown, "warn"];
  else if (delivery === "failed") [state, tone] = ["Failed", "bad"];
  else if (delivery === "sending" || (open && remaining <= 0)) [state, tone] = ["Sending", "warn"];
  else if (until !== null) [state, tone] = [`Scheduled, sends at ${clockTime(followUp.undo_until ?? "", viewer.locale)}`, "ok"];
  else [state, tone] = ["Scheduled", "ok"];

  const canUndo = open && remaining > 0 && !undone;
  const refusal = undo.error instanceof ApiFailure ? messageFor(undo.error) : undo.error ? "Could not undo. Nothing was changed." : null;
  return (
    <div className={styles.stack}>
      <p role="status">
        <Pill tone={tone}>{state}</Pill>
      </p>
      {canUndo && (
        <>
          <div className={styles.undoLine}>
            <Button size="sm" isPending={undo.isPending} onPress={() => undo.mutate(followUp.action_id)}>
              Undo
            </Button>
            <p className={styles.countdown} aria-hidden="true">
              Undo within {countdownText(remaining)}
            </p>
          </div>
          <span className="t-small muted">Closing this tab does not stop the send.</span>
        </>
      )}
      {refusal !== null && (
        <p role="alert" className={styles.problem}>
          {refusal}
        </p>
      )}
    </div>
  );
}
