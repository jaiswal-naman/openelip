/**
 * One bar: a row's share of the total, drawn as SVG. The percentage is printed beside it (FRONTEND_UI 3.21), so the
 * bar is decoration for the eye and hidden from screen readers. There is no limit to measure against in Phase 1, so it
 * has no amber or red: those colours belong to a budget bar.
 */
export function ShareBar({ fraction }: { fraction: number }) {
  const width = `${Math.min(100, Math.max(0, fraction * 100))}%`;
  return (
    <svg width="100%" height="8" aria-hidden="true" focusable="false">
      <rect width="100%" height="8" fill="var(--surface-2)" />
      <rect width={width} height="8" fill="var(--accent)" />
    </svg>
  );
}
