import { LinkButton } from "@/components/ui/LinkButton";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { CONSTRAINT_KIND_LABELS, IMPORTANCE_LABELS, MATCH_REASON_LABELS, OUTCOME_LABELS, VERDICT_LABELS } from "@/lib/labels";
import type { DecisionData } from "./queries";
import styles from "./match.module.css";

/** One plain sentence for the reasons a person is likely to meet. The label of the reason follows for the rest. */
const EXPLANATIONS: Partial<Record<DecisionData["reason_codes"][number], string>> = {
  not_scored: "No model has read this job against your record, so there is no requirement table for it.",
  weak_fit: "The evidence for this job was too weak to show it to you.",
  required_not_met: "A requirement the posting calls required is not met by your record.",
  below_daily_cap: "It fits, but your five matches for that day were already chosen.",
  citation_unresolved: "A citation in the assessment did not resolve, so the job was held back instead of shown with evidence you cannot check.",
  already_shown: "You have already seen this version of the posting.",
  job_closed: "The job closed before it could be shown.",
};

/**
 * The page behind a link to a job that was never a match: one the constraints filtered or the run withheld (MAT-05,
 * MAT-06). It says which and why, with no evidence table, because a job that breaks a hard constraint never reaches the
 * match page (invariant 1) and a withheld one has no assessment to show.
 */
export function HeldDecision({ decision }: { decision: DecisionData }) {
  const { job } = decision;
  const filtered = decision.outcome === "filtered";
  return (
    <article className={styles.detail}>
      <TitleBlock
        title={<span className={styles.title}>{job.title}</span>}
        lines={<p className={styles.company}>{job.company_name.trim() === "" ? "Company not stated" : job.company_name}</p>}
      />
      <p className={styles.banner}>
        <span className={`t-label ${styles.bannerLabel}`}>{OUTCOME_LABELS[decision.outcome]}</span>
        <span>{filtered ? "This job no longer meets your constraints." : "This job was held back, so there is no match to open."}</span>
      </p>

      <section aria-labelledby="why-heading" className={styles.section}>
        <h2 id="why-heading" className="t-section">
          Why
        </h2>
        <ul className={styles.reasonList}>
          {decision.reason_codes.map((code) => (
            <li key={code}>
              <strong>{MATCH_REASON_LABELS[code]}</strong>
              {EXPLANATIONS[code] && <span>. {EXPLANATIONS[code]}</span>}
            </li>
          ))}
        </ul>
        {filtered && (
          <p>
            {decision.constraint?.statement ? <>Your rule: &ldquo;{decision.constraint.statement}&rdquo;</> : "The constraint that removed it is one you have since removed or changed."}
            {decision.violated_kinds.length > 0 && <> ({decision.violated_kinds.map((kind) => CONSTRAINT_KIND_LABELS[kind]).join(", ")})</>}
          </p>
        )}
        {decision.gaps.length > 0 && (
          <>
            <h3 className="t-card">Where it fell short</h3>
            <ul className={styles.reasonList}>
              {decision.gaps.map((gap) => (
                <li key={gap.requirement}>
                  <span className={styles.words}>{gap.requirement}</span> ({IMPORTANCE_LABELS[gap.importance]}, {VERDICT_LABELS[gap.verdict]})
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <div className={styles.actions}>
        <LinkButton href="/app/today?view=why-not" variant="primary">
          See everything held back
        </LinkButton>
      </div>
    </article>
  );
}
