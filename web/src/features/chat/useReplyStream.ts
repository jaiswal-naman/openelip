import { useEffect, useRef } from "react";

const EVENTS = ["message.start", "text.delta", "tool.start", "tool.result", "citation", "proposal", "campaign_proposal", "message.snapshot", "message.end", "error"];

/**
 * Open one EventSource for a reply and pass each event to `onEvent`, closing on `message.end` or `error`. The browser
 * reconnects by itself with Last-Event-ID; a connection that is closed for good before `message.end` is reported as an
 * `error` with the code `connection_lost`. The built-in connection `error` event has no data and is told apart from the
 * server's `error` event by that.
 */
export function useReplyStream(onEvent: (messageId: string, event: string, data: Record<string, unknown>) => void) {
  const sources = useRef(new Map<string, EventSource>());
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });
  useEffect(() => {
    const open = sources.current;
    return () => {
      open.forEach((source) => source.close());
      open.clear();
    };
  }, []);

  return (messageId: string, url: string) => {
    const source = new EventSource(url, { withCredentials: true });
    sources.current.set(messageId, source);
    const finish = () => {
      source.close();
      sources.current.delete(messageId);
    };
    for (const name of EVENTS) {
      source.addEventListener(name, (raw) => {
        const message = raw as MessageEvent;
        if (typeof message.data !== "string") {
          // the connection's own error: the browser retries unless it gave up
          if (name === "error" && source.readyState === EventSource.CLOSED) {
            handler.current(messageId, "error", { code: "connection_lost", is_retryable: true });
            handler.current(messageId, "message.end", { finish_reason: "interrupted" });
            finish();
          }
          return;
        }
        let data: Record<string, unknown> = {};
        try {
          data = JSON.parse(message.data) as Record<string, unknown>;
        } catch {
          return;
        }
        handler.current(messageId, name, data);
        if (name === "error") handler.current(messageId, "message.end", { finish_reason: "interrupted" }); // the API ends it so too
        if (name === "message.end" || name === "error") finish();
      });
    }
  };
}
