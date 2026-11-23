"use client";

import Link from "next/link";
import { useEffect, useId, useRef } from "react";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { describeFailure } from "@/lib/api/problems";
import { DismissDialog } from "./DismissDialog";
import type { BriefDecision } from "./queries";
import styles from "./today.module.css";
import { useFeedback } from "./useFeedback";

/** The id of the decision a match item refers to, or null. The API's `ref` is an open object, so it is read with a check. */
export function decisionIdOf(decision: BriefDecision): string | null {
  const id = decision.ref.decision_id;
  return typeof id === "string" ? id : null;
}

const KIND_LABEL = { match: "Match", record_proposals: "Record changes" } as const;

/**
 * One thing waiting for the person (FRONTEND_UI 4.1): a match to open, save or dismiss, a match nobody has assessed yet, or
 * the changes someone suggested to their record. The card is an article named by its title; its actions are real buttons and
 * links in reading order, the open link first. After a dismissal it becomes a one-line receipt, so the list does not jump.
 */
export function DecisionCard({
  decision,
  feedback,
  isOpen,
}: {
  decision: BriefDecision;
  /** Whether the person already saved or dismissed it, when today's matches know. */
  feedback?: { is_saved: boolean; is_dismissed: boolean };
  /** `?decision=` names this one: it is marked and scrolled into view. */
  isOpen: boolean;
}) {
  const headingId = useId();
  const ref = useRef<HTMLElement>(null);
  const decisionId = decisionIdOf(decision);
  const act = useFeedback(decisionId ?? "", feedback ?? { is_saved: false, is_dismissed: false });

  useEffect(() => {
    if (isOpen) ref.current?.scrollIntoView({ block: "center" });
  }, [isOpen]);

  const isMatch = decision.kind === "match";
  const label = isMatch && !decision.is_assessed ? "Not yet assessed" : KIND_LABEL[decision.kind];

  return (
    <article ref={ref} className={styles.card} aria-labelledby={headingId} aria-current={isOpen ? "true" : undefined} data-open={isOpen || undefined}>
      <div className={styles.cardMain}>
        <p className={`t-label ${styles.kind}`}>{label}</p>
        <h2 id={headingId} className={`t-card ${styles.cardTitle}`}>
          {decision.title}
        </h2>
        <p>{decision.why}</p>
        {decision.change_note && (
          <p className={styles.changed}>
            <span className={`t-label ${styles.changedLabel}`}>Changed since you saw it</span>
            <span>{decision.change_note}</span>
          </p>
        )}
        {decision.note && <p className="t-small muted">{decision.note}</p>}
        {act.hasProposal && (
          <p role="status" className={styles.receipt}>
            We suggested a change to your record. It changes nothing until you confirm it: see <Link href="/app/record">your record</Link>.
          </p>
        )}
        {act.error !== null && (
          <p role="alert" className={styles.problem}>
            {describeFailure(act.error)} Your choice was not saved.
          </p>
        )}
      </div>
      <div className={styles.cardSide}>
        {act.isDismissed ? (
          <p role="status" className={styles.receipt}>
            Dismissed. Open it and save it to take that back.
          </p>
        ) : (
          <div className={styles.cardActions}>
            {isMatch && decisionId !== null ? (
              <>
                <LinkButton href={`/app/matches/${decisionId}`} variant="primary" size="sm">
                  {decision.is_assessed ? "Open" : "See why"}
                </LinkButton>
                {decision.is_assessed && (
                  <>
                    <Button size="sm" isPending={act.isPending} onPress={act.toggleSave}>
                      {act.isSaved ? "Unsave" : "Save"}
                    </Button>
                    <DismissDialog onDismiss={act.dismiss} isPending={act.isPending} size="sm" />
                    {act.isSaved && <Pill tone="ok">Saved</Pill>}
                  </>
                )}
              </>
            ) : (
              <LinkButton href="/app/record" variant="primary" size="sm">
                Review
              </LinkButton>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
