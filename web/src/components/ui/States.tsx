"use client";

import type { ReactNode } from "react";
import { ApiFailure } from "@/lib/api/errors";
import { messageFor } from "@/lib/labels";
import { WarningIcon } from "../icons/icons";
import { Button } from "./Button";
import styles from "./states.module.css";

/** Empty: one sentence that says why, and the one action that changes it. Never an illustration on its own. */
export function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className={styles.panel}>
      <div className={styles.body}>
        <p>{children}</p>
        {action}
      </div>
    </div>
  );
}

/** Error: what failed, what the person can do, and Retry. The rest of the page keeps working. */
export function ErrorState({ what, error, onRetry }: { what: string; error: unknown; onRetry: () => void }) {
  // Anything that is not an answer from the API is a bug in this page: it gets the generic message.
  const failure = error instanceof ApiFailure ? error : { code: "unknown", requestId: null, retryAfterSeconds: null };
  return (
    <div role="alert" className={`${styles.panel} ${styles.error}`}>
      <span className={styles.icon}>
        <WarningIcon size={20} />
      </span>
      <div className={styles.body}>
        <p className="t-card">{what}</p>
        <p>{messageFor(failure)}</p>
        <Button size="sm" onPress={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}

/** "AI features are not set up on this server" (FRONTEND_UI 3.0): the feature is off, search and saved things still work. */
export function AiOffNotice({ children }: { children?: ReactNode }) {
  return (
    <div className={`${styles.panel} ${styles.notice}`}>
      <span className={styles.icon}>
        <WarningIcon size={20} />
      </span>
      <div className={styles.body}>
        <p className="t-card">AI features are off on this server</p>
        <p>{children ?? "AI features are not set up on this server. Search and everything already saved still work."}</p>
      </div>
    </div>
  );
}

/** The thin line at the top of a region while it refetches. The previous data stays on screen under it. */
export function ProgressLine({ active }: { active: boolean }) {
  return <div className={styles.line} data-active={active || undefined} aria-hidden="true" />;
}
