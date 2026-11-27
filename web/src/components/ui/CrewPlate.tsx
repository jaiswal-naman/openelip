import { AGENT_ROLE_LABELS } from "@/lib/labels";
import styles from "./crewPlate.module.css";

// Each crew member's fixed mark, the same on every page: timeline, recorder, crew tab, board.
const MARKS: Record<string, string> = {
  scout: "SC",
  researcher: "RS",
  analyst: "AN",
  writer: "WR",
  operator: "OP",
  chaser: "CH",
  coach: "CO",
  twin: "TW",
  guardian: "GD",
};

// What each one does, in one line, from the code that runs it (backend app/campaigns/crew and app/guardian). A role with
// no line here shows its name only.
export const CREW_DUTIES: Record<string, string> = {
  researcher: "Reads the company's own pages and keeps only quotes found there word for word.",
  analyst: "Groups the job's requirements by the verdicts already stored. No model call.",
  writer: "Drafts the application packet from your record.",
  operator: "Proposes each outside action for the Guardian to check.",
  guardian: "Checks every outside action; nothing goes out without your approval.",
  chaser: "Drafts follow-ups and checks them sentence by sentence.",
  coach: "Prepares interview questions from your evidence and the dossier.",
};

/** The two-letter mark alone, for a dense row; the role's name must be in the row's text beside it. */
export function CrewMark({ role }: { role: string }) {
  return (
    <span className={styles.mark} aria-hidden="true">
      {MARKS[role] ?? role.slice(0, 2).toUpperCase()}
    </span>
  );
}

/** The identity plate: mark, name and, when asked, the one-line duty. */
export function CrewPlate({ role, withDuty = false }: { role: string; withDuty?: boolean }) {
  const duty = withDuty ? CREW_DUTIES[role] : undefined;
  return (
    <span className={styles.plate}>
      <CrewMark role={role} />
      <span className={styles.text}>
        <span className={styles.name}>{AGENT_ROLE_LABELS[role] ?? role}</span>
        {duty && <span className={styles.duty}>{duty}</span>}
      </span>
    </span>
  );
}
