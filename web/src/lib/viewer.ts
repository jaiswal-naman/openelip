import { useSyncExternalStore } from "react";

/** Who is looking: numbers and dates follow their locale and time zone (FRONTEND_UI 6.5 and 6.6). */
export type Viewer = { locale: string; timeZone: string };

const SERVER_VIEWER: Viewer = { locale: "en-US", timeZone: "UTC" };
let browserViewer: Viewer | undefined;

function readBrowserViewer(): Viewer {
  browserViewer ??= {
    locale: navigator.language || SERVER_VIEWER.locale,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || SERVER_VIEWER.timeZone,
  };
  return browserViewer;
}

function subscribeNever(): () => void {
  return () => {};
}

/**
 * The server cannot know the viewer, so it renders in UTC and en-US. `useSyncExternalStore` swaps in the browser's own
 * values after hydration without a mismatch warning, so a page never claims the viewer's local time while showing UTC.
 */
export function useViewer(): Viewer {
  return useSyncExternalStore(subscribeNever, readBrowserViewer, () => SERVER_VIEWER);
}
