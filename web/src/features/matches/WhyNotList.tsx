import Link from "next/link";
import { CONSTRAINT_KIND_LABELS, IMPORTANCE_LABELS, MATCH_REASON_LABELS, VERDICT_LABELS } from "@/lib/labels";
import type { DecisionView, TodayMatches } from "./queries";
import mine from "./match.module.css";
import styles from "./today.module.css";

type Reason = DecisionView["reason_codes"][number];

// What a person most wants to know first. A reason not listed here comes after these, in the order the ledger gave it.
const GROUP_ORDER: Reason[] = [
  "hard_constraint",
  "citation_unresolved",
  "injection_suspected",
  "required_not_met",
  "weak_fit",
  "below_daily_cap",
  "not_scored",
  "job_closed",
  "already_shown",
];

/** A decision's main reason: the first code it carries. A withheld or filtered decision always has at least one. */
const reasonOf = (decision: DecisionView): Reason => decision.reason_codes[0] ?? "not_scored";

/**
 * Everything held back on a day, grouped by reason with counts, each row opening the job (MAT-06, FRONTEND_UI 3.3). A job
 * the constraints filtered shows the constraint that removed it; a job that was scored and withheld shows the requirements
 * it fell short on. The same job in two runs of one day is listed once. `run` is the day's latest finished run: its
 * `filtered` counts every job the constraints removed, of which the ledger stores only the best-ranked few.
 */
export function WhyNotList({ rows, run }: { rows: DecisionView[]; run: TodayMatches["run"] }) {
  const seen = new Set<string>();
  const groups = new Map<Reason, DecisionView[]>();
  for (const decision of rows) {
    const key = `${reasonOf(decision)}:${decision.job.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    groups.set(reasonOf(decision), [...(groups.get(reasonOf(decision)) ?? []), decision]);
  }
  const order = [...groups.keys()].sort((a, b) => rank(a) - rank(b));

  if (order.length === 0) {
    return <p className={mine.held}>{run === null ? "No match run has finished for this day yet." : "Nothing was held back on this day."}</p>;
  }
  return (
    <div className={`${styles.groups} ${mine.held}`}>
      {order.map((reason) => {
        const listed = groups.get(reason) ?? [];
        // Only the filtered group has a larger true count than it lists: the run counts every violator, the ledger keeps the best few.
        const total = reason === "hard_constraint" && run !== null ? Math.max(run.filtered, listed.length) : listed.length;
        return (
          <section key={reason} aria-labelledby={`why-${reason}`} className={styles.group}>
            <h2 id={`why-${reason}`} className="t-section">
              {MATCH_REASON_LABELS[reason]} ({total})
            </h2>
            {total > listed.length && <p className="t-small muted">Showing the {listed.length} best-ranked of {total}.</p>}
            <ul className={styles.rows}>
              {listed.map((decision) => (
                <li key={decision.decision_id ?? decision.job.id} className={styles.row}>
                  <p className={`t-card ${styles.cardTitle}`}>
                    {decision.decision_id === null ? decision.job.title : <Link href={`/app/matches/${decision.decision_id}`}>{decision.job.title}</Link>}
                  </p>
                  <p className={styles.company}>{decision.job.company_name}</p>
                  <Detail decision={decision} />
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

const rank = (reason: Reason) => {
  const index = GROUP_ORDER.indexOf(reason);
  return index === -1 ? GROUP_ORDER.length : index;
};

/** The one line that says what rule or gap held the job back, or nothing when the reason's label says it all. */
function Detail({ decision }: { decision: DecisionView }) {
  if (decision.outcome === "filtered" && decision.constraint !== null) {
    const { kind, statement } = decision.constraint;
    return (
      <p className="t-small">
        {CONSTRAINT_KIND_LABELS[kind]}: {statement === null ? "a constraint you have since removed or changed" : <>&ldquo;{statement}&rdquo;</>}
      </p>
    );
  }
  if (decision.gaps.length === 0) return null;
  return (
    <ul className={`t-small ${styles.gaps}`}>
      {decision.gaps.map((gap) => (
        <li key={gap.requirement}>
          {gap.requirement} ({IMPORTANCE_LABELS[gap.importance]}, {VERDICT_LABELS[gap.verdict]})
        </li>
      ))}
    </ul>
  );
}
