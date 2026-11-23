"use client";

import { useState } from "react";
import { ErrorState } from "@/components/ui/States";
import { AcceptedIntroductions } from "./AcceptedIntroductions";
import { isOpenAsk, useIntroductions } from "./api";
import { IntroductionCard } from "./IntroductionCard";
import { IntroductionConsentSheet } from "./IntroductionConsentSheet";
import styles from "./introductions.module.css";

/**
 * The introductions on Today: each ask still open as an INTRODUCTION card, and what the person accepted. `?decision=<id>`
 * (the brief's link) opens that ask's consent sheet when it loads. The sheet lives here, not in the card, because accepting
 * makes the card go away while the sheet is still finishing (the optional signal).
 */
export function TodayIntroductions({ openId }: { openId: string | null }) {
  const list = useIntroductions();
  // undefined until the list first arrives; then the ask ?decision= names, if it is still open, or none. Chosen once, so
  // the sheet stays open while the list changes under it (accepting removes the card).
  const [sheetId, setSheetId] = useState<string | null | undefined>(undefined);
  const items = list.data ?? [];
  const asked = items.filter((item) => isOpenAsk(item));
  if (sheetId === undefined && list.data !== undefined) {
    setSheetId(openId !== null && asked.some((item) => item.id === openId) ? openId : null);
  }

  if (list.isError) return <ErrorState what="Could not load your introductions." error={list.error} onRetry={() => void list.refetch()} />;
  return (
    <>
      {asked.length > 0 && (
        <ul className={styles.list} aria-label="Introductions waiting for you">
          {asked.map((item) => (
            <li key={item.id}>
              <IntroductionCard item={item} isOpen={item.id === openId} onReview={() => setSheetId(item.id)} />
            </li>
          ))}
        </ul>
      )}
      <AcceptedIntroductions items={items} />
      {typeof sheetId === "string" && (
        <IntroductionConsentSheet introductionId={sheetId} isOpen onClose={() => setSheetId(null)} onDone={() => setSheetId(null)} />
      )}
    </>
  );
}
