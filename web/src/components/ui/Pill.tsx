import type { ReactNode } from "react";
import { CheckIcon, HelpIcon } from "../icons/icons";
import styles from "./pill.module.css";

const ICONS = { ok: <CheckIcon />, unk: <HelpIcon /> };

/** An outlined status pill: always an icon and text, never colour alone (FRONTEND_UI 5.1 and 7.1). */
export function Pill({ tone, children }: { tone: keyof typeof ICONS; children: ReactNode }) {
  return (
    <span className={`${styles.pill} ${styles[tone]}`}>
      {ICONS[tone]}
      {children}
    </span>
  );
}
