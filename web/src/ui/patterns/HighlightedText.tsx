"use client";

import { useEffect, useMemo, useRef } from "react";
import styles from "./HighlightedText.module.css";

/** A range of a text counted in Unicode code points, end exclusive: the unit Python and PostgreSQL index text by. */
export type CodePointSpan = { start: number; end: number };

const isInside = (span: CodePointSpan, length: number) => span.start >= 0 && span.start < span.end && span.end <= length;

/**
 * True when the text at the span is exactly the quote, so a citation that no longer resolves is never shown as if it did
 * (FRONTEND_UI 4.2). `points` is `Array.from(text)`. JavaScript strings count UTF-16 units, so `text.slice(start, end)`
 * is wrong after any emoji; a span must be cut from code points, never from the string.
 */
export function citationResolves(points: readonly string[], span: CodePointSpan, quote: string): boolean {
  return isInside(span, points.length) && points.slice(span.start, span.end).join("") === quote;
}

/**
 * The job-text half of the evidence highlight (FRONTEND_UI 4.2), built once here so every screen that cites a posting
 * looks and behaves the same. It marks the span in a `<mark>`, scrolls the mark into view and tells a screen reader.
 * A span that is not inside the text marks nothing: the text is shown whole, never a nearby guess. Callers decide
 * whether a citation resolves with `citationResolves` and show "Withheld: citation did not resolve" themselves.
 *
 * The text is third-party text and is only ever rendered as text.
 */
export function HighlightedText({
  text,
  span,
  announcement,
}: {
  text: string;
  /** Where to mark, in code points, or `null` to mark nothing. */
  span: CodePointSpan | null;
  /** What a screen reader hears when the mark appears or moves, for example "Job sentence highlighted." */
  announcement: string;
}) {
  const points = useMemo(() => Array.from(text), [text]);
  const marked = span !== null && isInside(span, points.length) ? span : null;
  const markRef = useRef<HTMLElement>(null);
  const key = marked === null ? null : `${marked.start}:${marked.end}`;

  useEffect(() => {
    if (key === null) return;
    // Centre the mark, or with reduced motion just bring it to the nearest edge, with no smooth scrolling (FRONTEND_UI 4.2).
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    markRef.current?.scrollIntoView(reduced ? { block: "nearest", behavior: "auto" } : { block: "center" });
  }, [key]);

  return (
    <>
      <div className={styles.text}>
        {marked === null ? (
          text
        ) : (
          <>
            {points.slice(0, marked.start).join("")}
            <span className="sr-only">Cited: </span>
            <mark ref={markRef} className={styles.mark}>
              {points.slice(marked.start, marked.end).join("")}
            </mark>
            {points.slice(marked.end).join("")}
          </>
        )}
      </div>
      {/* A new message node each time the mark moves, so the same words are announced again for the next sentence. */}
      <div role="status" aria-live="polite" className="sr-only">
        {key !== null && <span key={key}>{announcement}</span>}
      </div>
    </>
  );
}
