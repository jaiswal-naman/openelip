import Link from "next/link";
import { CrewPlate } from "@/components/ui/CrewPlate";
import { Pill } from "@/components/ui/Pill";
import { LocalTime } from "@/components/ui/Viewer";
import { RecordStatusMenu } from "@/features/applications/RecordStatusMenu";
import { CLOSED_REASON_LABELS, PAUSE_LABELS } from "@/lib/labels";
import type { BoardCard } from "./queries";
import styles from "./campaigns.module.css";

/**
 * One campaign on the board, as a bordered row: the job and its flags, who has the next step and what it is, and when anything
 * last happened. The Record status menu sits at the right edge.
 */
export function CampaignCard({ card, stage, applicationId }: { card: BoardCard; stage: string; applicationId: string | null }) {
  const needsYou = card.pending_approvals > 0 && stage !== "researching" && stage !== "closed";
  return (
    <article className={styles.card} aria-label={`${card.title} at ${card.company_name}`}>
      <div className={styles.cardMain}>
        <h3 className={`t-card ${styles.cardTitle}`}>
          <Link href={`/app/campaigns/${card.id}`}>{card.title}</Link>
        </h3>
        <p className="muted">{card.company_name.trim() === "" ? "Company not stated" : card.company_name}</p>
        {(card.from_introduction || card.pause_reason !== null || needsYou || card.closed_reason !== null) && (
          <div className={styles.tags}>
            {card.from_introduction && <span className={styles.tag}>From an introduction</span>}
            {card.pause_reason !== null && <Pill tone="warn">{PAUSE_LABELS[card.pause_reason]}</Pill>}
            {needsYou && <Pill tone="warn">Needs you</Pill>}
            {card.closed_reason !== null && <span className={styles.tag}>{CLOSED_REASON_LABELS[card.closed_reason] ?? "Closed"}</span>}
          </div>
        )}
      </div>
      <div className={styles.nextStep}>
        {card.next_step !== null && (
          <>
            <CrewPlate role={card.next_step.agent_role} />
            <p className="t-small">{card.next_step.text}</p>
          </>
        )}
      </div>
      <div className={styles.cardSide}>
        {card.last_activity_at !== null && (
          <p className="t-small muted">
            Last activity <LocalTime iso={card.last_activity_at} />
          </p>
        )}
        {applicationId !== null && <RecordStatusMenu applicationId={applicationId} title={card.title} />}
      </div>
    </article>
  );
}
