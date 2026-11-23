import type { ReactNode } from "react";
import { CheckIcon, HalfCircleIcon, HelpIcon, WarningIcon, XIcon } from "../icons/icons";
import styles from "./pill.module.css";

// The verdict tones of FRONTEND_UI 6.2: ok a check, warn a half circle, bad a cross, unk a question mark. Two more for
// stages, which are not verdicts: redline (the warning triangle) for what waits on the person, and stage, plain ink with no
// icon, where the word alone says where something is.
const ICONS = { ok: <CheckIcon />, warn: <HalfCircleIcon />, bad: <XIcon />, unk: <HelpIcon />, redline: <WarningIcon />, stage: null };

/** An outlined status pill: a verdict is always an icon and text, never colour alone (FRONTEND_UI 5.1 and 7.1). */
export function Pill({ tone, children }: { tone: keyof typeof ICONS; children: ReactNode }) {
  return (
    <span className={`${styles.pill} ${styles[tone]}`}>
      {ICONS[tone]}
      {children}
    </span>
  );
}
