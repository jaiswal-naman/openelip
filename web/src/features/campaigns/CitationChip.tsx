import numeral from "@/components/ui/numeral.module.css";
import { LocalTime } from "@/components/ui/Viewer";
import { safeHttpUrl } from "@/features/jobs/jobs";

/**
 * One source of a dossier claim as a numbered callout (CMP-02): the numeral opens the page, the exact quote sits beside it,
 * then where and when it was fetched. `label` ("Source 2a") is the accessible name; the numeral shows its number only.
 */
export function CitationChip({ source, label }: { source: { url: string; quote: string; retrieved_at: string }; label: string }) {
  const href = safeHttpUrl(source.url);
  const mark = label.replace(/^Source\s*/, "");
  return (
    <span className={numeral.callout}>
      {href !== null ? (
        <a className={numeral.numeral} href={href} target="_blank" rel="noopener noreferrer nofollow" aria-label={`${label}: open the source page (opens in a new tab)`}>
          {mark}
        </a>
      ) : (
        <span className={numeral.numeral}>
          <span className="sr-only">Source </span>
          {mark}
        </span>
      )}
      <blockquote className={numeral.quote}>{source.quote}</blockquote>
      <span className={numeral.where}>
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
