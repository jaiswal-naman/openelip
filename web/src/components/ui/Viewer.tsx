"use client";

import { formatCount, formatDateTime } from "@/lib/format";
import { useViewer } from "@/lib/viewer";

/** A time in the viewer's own zone, as <time datetime> with the full ISO value (FRONTEND_UI 6.6). */
export function LocalTime({ iso }: { iso: string }) {
  const viewer = useViewer();
  return <time dateTime={iso}>{formatDateTime(iso, viewer)}</time>;
}

/** A count in the viewer's locale: exact up to 9,999, then compact with the exact value on hover and for screen readers. */
export function Count({ value }: { value: number }) {
  const { locale } = useViewer();
  const text = formatCount(value, locale);
  if (Math.abs(value) <= 9999) return <>{text}</>;
  const exact = new Intl.NumberFormat(locale).format(value);
  return (
    <span title={exact}>
      {text}
      <span className="sr-only"> ({exact})</span>
    </span>
  );
}
