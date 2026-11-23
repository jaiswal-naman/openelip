import type { ReactNode } from "react";
import { CheckIcon, HalfCircleIcon, HelpIcon, XIcon } from "../icons/icons";
import styles from "./pill.module.css";

// The verdict tones of FRONTEND_UI 6.2: ok a check, warn a half circle, bad a cross, unk a question mark.
const ICONS = { ok: <CheckIcon />, warn: <HalfCircleIcon />, bad: <XIcon />, unk: <HelpIcon /> };

/** An outlined status pill: always an icon and text, never colour alone (FRONTEND_UI 5.1 and 7.1). */
export function Pill({ tone, children }: { tone: keyof typeof ICONS; children: ReactNode }) {
  return (
    <span className={`${styles.pill} ${styles[tone]}`}>
      {ICONS[tone]}
      {children}
    </span>
  );
}
