"use client";

import { useMe } from "@/features/auth/queries";
import { ChatPanel } from "./ChatPanel";

/** The header's "Ask the agent", for a signed-in candidate only. */
export function ChatOpener() {
  const me = useMe();
  return me.data ? <ChatPanel /> : null;
}
