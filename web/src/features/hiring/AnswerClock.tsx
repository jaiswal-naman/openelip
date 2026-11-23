"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./hiring.module.css";

export type ClockTone = "neutral" | "amber" | "red" | "overdue";

const MINUTE = 60_000;
const AMBER_AT = 2; // business days (FRONTEND_UI 4.7)

/** The current time, re-read once a minute: the clock is never a second counter. */
function useMinute(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), MINUTE);
    return () => clearInterval(tick);
  }, []);
  return now;
}

/**
 * The tone and words of FRONTEND_UI 4.7. The business days left come from the server and are never counted here; the browser
 * only notices that the due time has passed between two server answers.
 */
export function clockState(input: { dueAt: string; businessDaysLeft: number; isOverdue: boolean; timeZone?: string; now: number }): { tone: ClockTone; text: string } {
  if (input.isOverdue || input.now >= new Date(input.dueAt).getTime()) return { tone: "overdue", text: "Overdue" };
  if (input.businessDaysLeft < 1) {
    const time = new Date(input.dueAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: input.timeZone });
    return { tone: "red", text: `Due today ${time}` };
  }
  const days = `${input.businessDaysLeft} ${input.businessDaysLeft === 1 ? "day" : "days"}`;
  return { tone: input.businessDaysLeft <= AMBER_AT ? "amber" : "neutral", text: days };
}

/** A pill for one owed answer. Its live region speaks only when the tone changes, never on a plain re-render. */
export function AnswerClock({ dueAt, businessDaysLeft, isOverdue, timeZone }: { dueAt: string; businessDaysLeft: number; isOverdue: boolean; timeZone?: string }) {
  const now = useMinute();
  const { tone, text } = clockState({ dueAt, businessDaysLeft, isOverdue, timeZone, now });
  const previous = useRef<ClockTone>(tone);
  const [announcement, setAnnouncement] = useState("");
  useEffect(() => {
    if (previous.current !== tone) setAnnouncement(`Answer clock: ${text}`);
    previous.current = tone;
  }, [tone, text]);
  return (
    <>
      <span className={styles.clock} data-tone={tone}>
        {text}
      </span>
      <span role="status" className="sr-only">
        {announcement}
      </span>
    </>
  );
}
