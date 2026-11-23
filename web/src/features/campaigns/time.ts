import { useEffect, useState } from "react";

/** HH:MM in the viewer's own zone, for "sends at 14:05". */
export function clockTime(iso: string, locale?: string): string {
  return new Date(iso).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
}

/**
 * The difference between the server's clock and this one, from the `server_time` an answer carried and the moment the
 * answer arrived. The countdown to `undo_until` uses it, so a wrong clock on this computer cannot show an open undo window
 * that has closed (FRONTEND_UI 4.5).
 */
export function serverOffsetMs(serverTime: string, receivedAt: number): number {
  const server = Date.parse(serverTime);
  return Number.isNaN(server) ? 0 : server - receivedAt;
}

/** `Date.now()` on the server's clock, ticking once a second while `active`. One interval for the component. */
export function useServerNow(offsetMs: number, active: boolean): number {
  const [local, setLocal] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setLocal(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return local + offsetMs;
}

export function countdownText(remainingMs: number): string {
  const seconds = Math.max(0, Math.ceil(remainingMs / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
