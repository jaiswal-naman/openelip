import { safeHttpUrl } from "@/features/jobs/jobs";
import { LocalTime } from "@/components/ui/Viewer";
import styles from "./campaigns.module.css";

/**
 * One source of a dossier claim: a chip that opens the page it was read from, with the exact quote and the time it was
 * fetched (CMP-02). The URL, the quote and the host are text; a URL that is not http or https is shown as text and not linked.
 */
export function CitationChip({ source, label }: { source: { url: string; quote: string; retrieved_at: string }; label: string }) {
  const href = safeHttpUrl(source.url);
  return (
    <span className={styles.stack}>
      {href !== null ? (
        <a className={styles.chip} href={href} target="_blank" rel="noopener noreferrer nofollow" aria-label={`${label}: open the source page (opens in a new tab)`}>
          {label}
        </a>
      ) : (
        <span className={styles.chip}>{label}</span>
      )}
      <blockquote className={styles.quote}>{source.quote}</blockquote>
      <span className="t-small muted">
        {hostOf(source.url)}, fetched <LocalTime iso={source.retrieved_at} />
      </span>
    </span>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return "unknown source";
  }
}
