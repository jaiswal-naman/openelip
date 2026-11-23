"use client";

import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { clockTime, useServerNow } from "@/features/campaigns/time";
import { useUndo } from "@/features/campaigns/queries";
import { messageFor } from "@/lib/labels";
import { ApiFailure } from "@/lib/api/errors";
import { useViewer } from "@/lib/viewer";
import type { BriefData } from "@/features/matches/queries";
import styles from "@/features/introductions/introductions.module.css";

type Decision = BriefData["decisions"][number];
export type CampaignItem = Extract<Decision, { kind: "approval" | "activity" }>;
type Undoable = BriefData["undoable"][number];

/** The items that are matches or record changes: everything but introductions and the agents' own items. */
export function matchDecisions(decisions: BriefData["decisions"]): Extract<Decision, { kind: "match" | "record_proposals" }>[] {
  return decisions.filter((decision): decision is Extract<Decision, { kind: "match" | "record_proposals" }> => decision.kind === "match" || decision.kind === "record_proposals");
}

/** The `approval` and `activity` items of a brief (07-06): what waits for the candidate and what the agents did. */
export function campaignItems(decisions: BriefData["decisions"]): CampaignItem[] {
  return decisions.filter((decision): decision is CampaignItem => decision.kind === "approval" || decision.kind === "activity");
}

function Receipt({ item }: { item: Undoable }) {
  const viewer = useViewer();
  const undo = useUndo();
  const now = useServerNow(0, true);
  const left = Date.parse(item.undo_until) - now;
  const undone = undo.data?.action.status === "cancelled";
  return (
    <li className={styles.row}>
      <span className={styles.words}>
        {undone ? `Undone: the follow-up to ${item.company_name} will not be sent.` : `A follow-up to ${item.company_name} is scheduled and sends at ${clockTime(item.undo_until, viewer.locale)}.`}
      </span>{" "}
      {!undone && left > 0 && (
        <Button size="sm" isPending={undo.isPending} onPress={() => undo.mutate(item.action_id)}>
          Undo
        </Button>
      )}{" "}
      <Link href={`/app/campaigns/${item.campaign_id}?tab=follow-ups`}>Open the campaign</Link>
      {undo.error && (
        <span role="alert"> {undo.error instanceof ApiFailure ? messageFor(undo.error) : "Could not undo."}</span>
      )}
    </li>
  );
}

/** What waits for approval, what the agents did, and a receipt with Undo for each scheduled follow-up. Decides nothing itself. */
export function BriefCampaignItems({ items, undoable }: { items: CampaignItem[]; undoable: Undoable[] }) {
  if (items.length === 0 && undoable.length === 0) return null;
  const approvals = items.filter((item) => item.kind === "approval");
  const activity = items.filter((item) => item.kind === "activity");
  return (
    <>
      {approvals.length > 0 && (
        <section aria-labelledby="brief-approvals" className={styles.section}>
          <h2 id="brief-approvals" className="t-section">
            Waiting for your approval
          </h2>
          <ul className={styles.rows}>
            {approvals.map((item) => (
              <li key={item.approval_id ?? item.text} className={styles.row}>
                <span className={styles.words}>{item.text}</span> <Link href={item.link}>Review</Link>
              </li>
            ))}
          </ul>
        </section>
      )}
      {undoable.length > 0 && (
        <section aria-labelledby="brief-undo" className={styles.section}>
          <h2 id="brief-undo" className="t-section">
            Sending soon
          </h2>
          <ul className={styles.rows}>
            {undoable.map((item) => (
              <Receipt key={item.action_id} item={item} />
            ))}
          </ul>
        </section>
      )}
      {activity.length > 0 && (
        <section aria-labelledby="brief-activity" className={styles.section}>
          <h2 id="brief-activity" className="t-section">
            What your agents did
          </h2>
          <ul className={styles.rows}>
            {activity.map((item) => (
              <li key={`${item.agent_action_id}:${item.text}`} className={styles.row}>
                <span className={styles.words}>{item.text}</span> <Link href={item.link}>Open the campaign</Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
