import type { CampaignDetail, DossierClaim } from "../queries";
import { CitationChip } from "../CitationChip";
import styles from "../campaigns.module.css";

const TOPIC_LABELS: Record<string, string> = {
  product: "Product",
  customers: "Customers",
  size: "Size",
  funding: "Funding",
  engineering: "Engineering",
  hiring_process: "Hiring process",
  values: "Values",
  leadership: "Leadership",
  news: "News",
  culture: "Culture",
  other: "Other",
};

/**
 * The one-page company dossier (CMP-02): each claim is a hairline row, and every source under it is a numbered callout
 * with its exact quote and when it was fetched. Text only.
 */
export function DossierTab({ dossier }: { dossier: CampaignDetail["dossier"] }) {
  if (dossier === null) return <p className="muted">The Researcher has not finished the dossier yet.</p>;
  const claims = dossier.claims as unknown as DossierClaim[];
  return (
    <div className={styles.stack}>
      {dossier.incomplete && <p className={styles.notice}>This dossier is incomplete: some pages could not be read.</p>}
      {claims.length === 0 ? (
        <p>No claim could be checked against the company&apos;s own pages.</p>
      ) : (
        <ul className={styles.rows} aria-label="Dossier claims">
          {claims.map((claim, index) => (
            <li key={`${index}:${claim.text}`} className={styles.claim}>
              {claim.topic ? <span className="t-label muted">{TOPIC_LABELS[claim.topic] ?? claim.topic}</span> : <span />}
              <div className={styles.claimBody}>
                <p>{claim.text}</p>
                {claim.injection_suspected && <p className="t-small">This page carried an instruction meant for an AI, so it was not used.</p>}
                <div className={styles.callouts}>
                  {claim.sources.map((source, n) => (
                    <CitationChip key={`${source.url}:${n}`} source={source} label={`Source ${index + 1}${claim.sources.length > 1 ? String.fromCharCode(97 + n) : ""}`} />
                  ))}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {dossier.missing_topics.length > 0 && (
        <p className="muted">Not found on the company&apos;s pages: {dossier.missing_topics.map((topic) => TOPIC_LABELS[topic] ?? topic).join(", ")}.</p>
      )}
    </div>
  );
}
