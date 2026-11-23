import { useSyncExternalStore } from "react";

/** The job a page is about, so "Ask the agent" can send it as context. A page sets it while it is on screen. */
export type ChatJobContext = { jobId: number; company: string } | null;

let current: ChatJobContext = null;
const listeners = new Set<() => void>();

export function setChatContext(next: ChatJobContext) {
  current = next;
  listeners.forEach((listener) => listener());
}

export function useChatContext(): ChatJobContext {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
    () => null,
  );
}
