import balloon from "@/components/ui/balloon.module.css";
import { LocalTime } from "@/components/ui/Viewer";
import { safeHttpUrl } from "@/features/jobs/jobs";

/**
 * One source of a dossier claim as a balloon callout (CMP-02): the numbered balloon opens the page, the exact quote sits beside it,
 * then where and when it was fetched. `label` ("Source 2a") is the accessible name; the balloon shows its number only.
 */
export function CitationChip({ source, label }: { source: { url: string; quote: string; retrieved_at: string }; label: string }) {
  const href = safeHttpUrl(source.url);
  const mark = label.replace(/^Source\s*/, "");
  return (
    <span className={balloon.callout}>
      {href !== null ? (
        <a className={balloon.balloon} href={href} target="_blank" rel="noopener noreferrer nofollow" aria-label={`${label}: open the source page (opens in a new tab)`}>
          {mark}
        </a>
      ) : (
        <span className={balloon.balloon}>
          <span className="sr-only">Source </span>
          {mark}
        </span>
      )}
      <blockquote className={balloon.quote}>{source.quote}</blockquote>
      <span className={balloon.where}>
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
