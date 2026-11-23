"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { useApi } from "@/lib/api/context";
import { ApiFailure, unwrap } from "@/lib/api/errors";
import { messageFor } from "@/lib/labels";
import { reloadTo } from "@/lib/navigate";

/**
 * Local mode has no sign-in page. This opens the owner's session (POST /v1/auth/local, which sets the cookie in the browser
 * itself) and goes on to `next`, or to /setup first when the stack has no model key yet. `next` is already checked by the page.
 */
export function LocalSession({ next }: { next: string }) {
  const api = useApi();
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let stale = false;
    (async () => {
      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        await unwrap(api.POST("/v1/auth/local", { body: { timezone } }));
        const status = await unwrap(api.GET("/v1/local/status"));
        if (stale) return;
        // A full page load, not a client navigation and a refresh: every page and query this browser holds was made for
        // nobody. The client navigation left a second browser on this page in the Compose browser test.
        reloadTo(status.model.key_set ? next : `/setup?next=${encodeURIComponent(next)}`);
      } catch (failure) {
        const known = failure instanceof ApiFailure ? failure : { code: "unknown", requestId: null, retryAfterSeconds: null };
        if (!stale) setError(messageFor(known));
      }
    })();
    return () => {
      stale = true;
    };
  }, [api, next, attempt]);

  if (error === null) return <p role="status">Opening your workspace.</p>;
  return (
    <>
      <h1 className="t-page">Could not open your workspace</h1>
      <p role="alert">{error}</p>
      <Button
        onPress={() => {
          setError(null);
          setAttempt((count) => count + 1);
        }}
      >
        Try again
      </Button>
    </>
  );
}
