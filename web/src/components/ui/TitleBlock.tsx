import type { ReactNode } from "react";
import styles from "./titleBlock.module.css";

export type TitleCell = {
  label: string;
  value: ReactNode;
  /** redline: needs the person; ok: done or approved; muted: not known yet or nothing to say. */
  tone?: "redline" | "ok" | "muted";
};

/**
 * A page header: an optional eyebrow, the heading, its lede, and a quiet row of small stat cells (who drew it, who checked
 * it, what needs you), each a label over its value. The cells are a description list, so a screen reader hears each label
 * with its value.
 */
export function TitleBlock({
  title,
  eyebrow,
  display = false,
  lines,
  cells = [],
  actions,
}: {
  title: ReactNode;
  /** The larger display size, for the home page and the onboarding steps. */
  display?: boolean;
  /** A small label above the heading, with a hairline dash before it: where this page sits ("Today"). */
  eyebrow?: ReactNode;
  /** The lede under the heading: what this page is, and what it says today. */
  lines?: ReactNode;
  cells?: TitleCell[];
  actions?: ReactNode;
}) {
  return (
    <header className={styles.block}>
      <div className={styles.name}>
        {eyebrow && <p className={`t-label t-eyebrow ${styles.eyebrow}`}>{eyebrow}</p>}
        <h1 className={display ? "t-display" : "t-page"}>{title}</h1>
        {lines && <div className={styles.lines}>{lines}</div>}
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
      {cells.length > 0 && (
        <dl className={styles.cells}>
          {cells.map((cell) => (
            <div key={cell.label} className={styles.cell} data-tone={cell.tone}>
              <dt className="t-label">{cell.label}</dt>
              <dd>{cell.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </header>
  );
}
