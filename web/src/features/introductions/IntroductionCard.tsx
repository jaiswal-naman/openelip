"use client";

import { useId } from "react";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { CANDIDATE_INTRODUCTION_LABELS as WORDS } from "@/lib/labels";
import { type IntroductionItem, useIntroduction } from "./api";
import { payText } from "./IntroductionConsentSheet";
import styles from "./introductions.module.css";

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

/**
 * One ask waiting for the person: an INTRODUCTION decision card (FRONTEND_UI 4.1). It is the candidate's own action, not an
 * approval, and its one button opens the consent sheet. The organization, role and pay come from the introduction itself
 * (the list has no pay and no demo flag), read once here and shared with the sheet through the query cache.
 */
export function IntroductionCard({ item, isOpen, onReview }: { item: IntroductionItem; isOpen: boolean; onReview: () => void }) {
  const headingId = useId();
  const detail = useIntroduction(item.id);
  const pay = detail.data ? payText(detail.data.role.pay_range) : null;
  const organization = item.organization_name ?? "An employer";
  const role = item.role_title ?? "a role";
  return (
    <article className={styles.card} aria-labelledby={headingId} aria-current={isOpen ? "true" : undefined} data-open={isOpen || undefined}>
      <div className={styles.cardMain}>
        <p className={`t-label ${styles.kind}`}>{WORDS.kind}</p>
        <div className={styles.head}>
          <h2 id={headingId} className={`t-card ${styles.cardTitle}`}>
            {organization} asks about {role}
          </h2>
          {detail.data?.organization.is_synthetic && <Pill tone="unk">{WORDS.demo}</Pill>}
        </div>
        {pay !== null && <p>{pay}</p>}
        <p className="t-small muted">Answer by {day(item.expires_at)}. No reply counts as no.</p>
      </div>
      <div className={styles.cardSide}>
        <Button variant="primary" size="sm" onPress={onReview}>
          {WORDS.review}
        </Button>
      </div>
    </article>
  );
}
