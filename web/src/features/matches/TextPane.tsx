"use client";

import { type ReactNode, useId } from "react";
import { type CodePointSpan, HighlightedText } from "@/ui/patterns/HighlightedText";
import styles from "./match.module.css";

/**
 * One of the two texts a requirement row points into (FRONTEND_UI 4.2 and 5.7): the posting or the resume. It is a
 * labelled, keyboard-scrollable region that shows the text with the cited span marked, or `empty` while there is no text
 * to show. `span` is in code points and `null` marks nothing; `note` says why when a citation could not be marked. The
 * text is third-party or personal text and is only ever rendered as text. `letter` names the column on the sheet and
 * `callout` is the number of the requirement the mark answers to; both are drawn, not read out, because the heading and the
 * announcement already say it.
 */
export function TextPane({
  title,
  letter,
  text,
  span,
  callout,
  announcement,
  note,
  empty,
}: {
  title: string;
  letter?: string;
  /** The whole text, or `null` when it is not available (yet). */
  text: string | null;
  span: CodePointSpan | null;
  callout?: number;
  /** What a screen reader hears when the mark appears; leave it empty for the pane that stays silent. */
  announcement: string;
  note?: ReactNode;
  /** Shown in place of the text. */
  empty: ReactNode;
}) {
  const headingId = useId();
  return (
    <div className={styles.pane}>
      <div className={styles.paneHead}>
        {letter && (
          <span className={styles.letter} aria-hidden="true">
            {letter}
          </span>
        )}
        <h3 id={headingId} className="t-section">
          {title}
        </h3>
      </div>
      <p className={`t-small ${styles.paneNote}`}>{note}</p>
      {text === null ? (
        <div className={styles.paneEmpty}>{empty}</div>
      ) : (
        <div className={styles.scroll} role="region" aria-labelledby={headingId} tabIndex={0}>
          <HighlightedText text={text} span={span} announcement={announcement} callout={callout} />
        </div>
      )}
    </div>
  );
}
