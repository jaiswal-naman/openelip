import type { Viewer } from "./viewer";

/** Plain formatting functions (FRONTEND_UI 6.5 and 6.6): no React, so server and client components can both use them. */

/**
 * Money is USD with its code. Two decimals; four under one cent; six (the ledger's own precision) when four would
 * round a real cost to zero, so a measured cost never prints as nothing.
 */
export function formatMoney(usd: string | number, locale?: string): string {
  const amount = typeof usd === "string" ? Number(usd) : usd;
  if (!Number.isFinite(amount)) return "Unknown";
  const magnitude = Math.abs(amount);
  const digits = magnitude === 0 || magnitude >= 0.01 ? 2 : magnitude >= 0.00005 ? 4 : 6;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    currencyDisplay: "code",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(amount);
}

/** Exact up to 9,999, then compact ("12.4K"). */
export function formatCount(count: number, locale?: string): string {
  if (Math.abs(count) <= 9999) return new Intl.NumberFormat(locale).format(count);
  return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(count);
}

export function formatPercent(fraction: number, locale?: string): string {
  if (fraction > 0 && fraction < 0.005) return "<1%";
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(fraction);
}

/** "22 Nov 06:12 IST", with the year when it is not the current one: "3 Mar 2025 06:12 IST". */
export function formatDateTime(iso: string, viewer: Viewer, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const { locale, timeZone } = viewer;
  const parts = new Intl.DateTimeFormat(locale, {
    timeZone,
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  const currentYear = new Intl.DateTimeFormat(locale, { timeZone, year: "numeric" }).format(now);
  const year = part("year") === currentYear ? "" : ` ${part("year")}`;
  return `${part("day")} ${part("month")}${year} ${part("hour")}:${part("minute")} ${part("timeZoneName")}`;
}

/** "2026-11" is a UTC month in the ledger; show it as "Nov 2026". Anything else is shown as it came. */
export function formatMonth(period: string, locale?: string): string {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(period);
  if (!match) return period;
  const first = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1));
  return new Intl.DateTimeFormat(locale, { month: "short", year: "numeric", timeZone: "UTC" }).format(first);
}
