import Link from "next/link";
import { Pill } from "@/components/ui/Pill";
import { LocalTime } from "@/components/ui/Viewer";
import { RecordStatusMenu } from "@/features/applications/RecordStatusMenu";
import { CLOSED_REASON_LABELS, PAUSE_LABELS } from "@/lib/labels";
import type { BoardCard } from "./queries";
import styles from "./campaigns.module.css";

/** One campaign on the board: the job, what happens next, and the things that need a look (pause, approvals, closing reason). */
export function CampaignCard({ card, stage, applicationId }: { card: BoardCard; stage: string; applicationId: string | null }) {
  const needsYou = card.pending_approvals > 0 && stage !== "researching" && stage !== "closed";
  return (
    <article className={styles.card} aria-label={`${card.title} at ${card.company_name}`}>
      <h3 className={`t-card ${styles.cardTitle}`}>
        <Link href={`/app/campaigns/${card.id}`}>{card.title}</Link>
      </h3>
      <p className="muted">{card.company_name.trim() === "" ? "Company not stated" : card.company_name}</p>
      <div className={styles.tags}>
        {card.from_introduction && <span className={styles.tag}>From an introduction</span>}
        {card.pause_reason !== null && <Pill tone="warn">{PAUSE_LABELS[card.pause_reason]}</Pill>}
        {needsYou && <Pill tone="warn">Needs you</Pill>}
        {card.closed_reason !== null && <span className={styles.tag}>{CLOSED_REASON_LABELS[card.closed_reason] ?? "Closed"}</span>}
      </div>
      {card.next_step !== null && <p className="t-small">{card.next_step.text}</p>}
      {card.last_activity_at !== null && (
        <p className="t-small muted">
          Last activity <LocalTime iso={card.last_activity_at} />
        </p>
      )}
      {applicationId !== null && <RecordStatusMenu applicationId={applicationId} title={card.title} />}
    </article>
  );
}
