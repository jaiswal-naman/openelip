"use client";

import { useEffect } from "react";
import { setChatContext } from "./context";

/** Put on a page about one job: while it is shown, the chat panel offers "context: {company}". Renders nothing. */
export function ChatContext({ jobId, company }: { jobId: number; company: string }) {
  useEffect(() => {
    setChatContext({ jobId, company });
    return () => setChatContext(null);
  }, [jobId, company]);
  return null;
}
