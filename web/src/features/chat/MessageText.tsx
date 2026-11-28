import Link from "next/link";
import type { ReactNode } from "react";
import { FileTextIcon } from "@/components/icons/icons";
import type { Citation } from "./queries";
import styles from "./chat.module.css";

const MARKER = /\[\[([dfjc][0-9]{1,4})\]\]/g;
const KIND_LABELS: Record<string, string> = { decision: "Decision", job: "Job", fact: "Record", constraint: "Constraint" };

/** The internal page a citation points at, or null when there is none. Never built from model text: only from ids of the citation event. */
export function citationHref(citation: Citation): string | null {
  if (citation.kind === "decision" && citation.decision_id) return `/app/matches/${encodeURIComponent(citation.decision_id)}`;
  if (citation.kind === "job" && citation.job_id !== null && citation.job_id !== undefined) return `/jobs/${citation.job_id}`;
  if (citation.kind === "fact" || citation.kind === "constraint") return "/app/record";
  return null;
}

/**
 * A reply as text. Every character is a text node: no markdown, no HTML, no link and no image comes from what the model
 * wrote (CHT-04, threat T-05-41). The one exception is a `[[d1]]` marker the API vouched for with a citation event, which
 * becomes a chip that links to an internal page. A marker with no citation stays plain text.
 */
export function MessageText({ text, citations }: { text: string; citations: Record<string, Citation> }) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(MARKER)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    const alias = match[1];
    const citation = citations[alias];
    if (citation === undefined) {
      parts.push(match[0]);
    } else {
      const label = `${KIND_LABELS[citation.kind] ?? "Source"} ${alias.slice(1)}`;
      const href = citationHref(citation);
      parts.push(
        href === null ? (
          <span key={index} className={styles.chip}>
            <FileTextIcon />
            {label}
          </span>
        ) : (
          <Link key={index} href={href} className={styles.chip}>
            <FileTextIcon />
            {label}
          </Link>
        ),
      );
    }
    last = index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <p className={styles.text}>{parts}</p>;
}
