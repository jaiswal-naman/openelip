/**
 * The line of a text that a span starts on: the line breaks before it, plus one. `start` counts Unicode code points, the
 * unit every span is in (Python and PostgreSQL index text that way), so the text is walked by code point and never by
 * JavaScript's UTF-16 string index, which is wrong after any emoji.
 */
export function lineNumberAt(text: string, start: number): number {
  let line = 1;
  let seen = 0;
  for (const point of text) {
    if (seen >= start) break;
    if (point === "\n") line += 1;
    seen += 1;
  }
  return line;
}

/**
 * The sentence a polite live region says when a requirement row is selected (FRONTEND_UI 4.2): it claims only what is
 * really marked. `line` is the resume line, or null when no resume text is marked.
 */
export function highlightAnnouncement(jobMarked: boolean, line: number | null): string {
  if (jobMarked && line !== null) return `Job sentence and resume line ${line} highlighted.`;
  if (jobMarked) return "Job sentence highlighted.";
  if (line !== null) return `Resume line ${line} highlighted.`;
  return "";
}
