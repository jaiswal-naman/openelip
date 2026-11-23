/** One run of words in a word-level diff: kept, taken out, or put in (FRONTEND_UI 4.3). */
export type DiffRun = { kind: "equal" | "removed" | "added"; text: string };

const words = (text: string): string[] => text.split(/\s+/).filter((word) => word !== "");

/**
 * The words of `before` and `after` as runs of equal, removed and added words, found with the longest common subsequence.
 * Pure. A moved word shows as removed in one place and added in the other; that is honest, and a diff cannot say more.
 */
export function wordDiff(before: string, after: string): DiffRun[] {
  const a = words(before);
  const b = words(after);
  // lengths[i][j] = the longest common subsequence of a[i..] and b[j..]
  const lengths = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lengths[i][j] = a[i] === b[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
    }
  }
  const runs: DiffRun[] = [];
  const push = (kind: DiffRun["kind"], word: string) => {
    const last = runs[runs.length - 1];
    if (last?.kind === kind) last.text += ` ${word}`;
    else runs.push({ kind, text: word });
  };
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      push("equal", a[i]);
      i += 1;
      j += 1;
    } else if (j < b.length && (i === a.length || lengths[i][j + 1] >= lengths[i + 1][j])) {
      push("added", b[j]);
      j += 1;
    } else {
      push("removed", a[i]);
      i += 1;
    }
  }
  return runs;
}
