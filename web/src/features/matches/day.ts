/**
 * "Mon 23 Nov": a local date (the API's `local_date`, "2026-11-23") as FRONTEND_UI 3.3 writes the day. It is the person's
 * own calendar date, so it is formatted in UTC from a time at noon, which no time zone can move to another day.
 */
export function dayLabel(isoDate: string, locale?: string): string {
  const date = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return isoDate;
  const parts = new Intl.DateTimeFormat(locale, { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("weekday")} ${part("day")} ${part("month")}`;
}
