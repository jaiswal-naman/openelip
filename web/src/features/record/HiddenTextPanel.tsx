import { WarningIcon } from "@/components/icons/icons";
import states from "@/components/ui/states.module.css";
import type { DocumentView } from "./api";
import { hiddenReasonLabel } from "./labels";
import styles from "./record.module.css";

/**
 * Text the reader removed because a person could not see it: white on white, too small, off the page (REC-06). It is
 * listed with what it said, and the panel says it is not used. The samples are the candidate's own file text, so they
 * are shown as text and never as markup, and nothing here logs them.
 */
export function HiddenTextPanel({ chars, flags }: { chars: number; flags: DocumentView["hidden_text_flags"] }) {
  if (chars <= 0) return null;
  const reasons = (flags?.reasons ?? []).map(hiddenReasonLabel);
  const samples = flags?.samples ?? [];
  return (
    <div className={`${states.panel} ${states.notice}`} role="group" aria-label="Hidden text removed from your file">
      <span className={states.icon}>
        <WarningIcon size={20} />
      </span>
      <div className={states.body}>
        <p className="t-card">
          We removed {chars} {chars === 1 ? "character" : "characters"} of hidden text from your file
          {reasons.length > 0 && ` (${reasons.join(", ")})`}.
        </p>
        <p>It is not used for anything. Only text a person can see on the page is read.</p>
        {samples.length > 0 && (
          <details className={styles.hidden}>
            <summary>See what was removed</summary>
            <ul>
              {samples.map((sample, index) => (
                <li key={index}>
                  It said: <q>{sample}</q>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
