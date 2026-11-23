import type { DocumentLine } from "./api";

const lineAt = (lines: DocumentLine[], offset: number) => lines.find((line) => line.start <= offset && offset < line.end);

/**
 * "L4", or "L6-L9" when the cited text runs over several lines: the lines of the cleaned resume text that hold a fact's
 * span. The span is half-open, so its last character is at end - 1. Null when the lines are not known or do not cover it.
 */
export function lineLabel(lines: DocumentLine[] | undefined, span: { start: number; end: number }): string | null {
  const first = lines && lineAt(lines, span.start);
  if (!lines || !first) return null;
  const last = lineAt(lines, span.end - 1);
  return last && last.n !== first.n ? `L${first.n}-L${last.n}` : `L${first.n}`;
}
