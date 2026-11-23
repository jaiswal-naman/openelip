import type { ReactNode } from "react";
import styles from "./titleBlock.module.css";

export type TitleCell = {
  label: string;
  value: ReactNode;
  /** redline: needs the person; ok: done or approved; muted: not known yet or nothing to say. */
  tone?: "redline" | "ok" | "muted";
};

/**
 * A sheet's title block: the page heading and its lines on the left, ruled cells of facts on the right (who drew it, who
 * checked it, what needs you). The cells are a description list, so a screen reader hears each label with its value.
 */
export function TitleBlock({
  title,
  lines,
  cells = [],
  actions,
}: {
  title: ReactNode;
  /** Short lines under the heading: what this sheet is, and what it says today. */
  lines?: ReactNode;
  cells?: TitleCell[];
  actions?: ReactNode;
}) {
  return (
    <header className={styles.block}>
      <div className={styles.name}>
        <h1 className="t-page">{title}</h1>
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
