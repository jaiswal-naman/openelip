"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ApiFailure } from "@/lib/api/errors";
import { messageFor } from "@/lib/labels";
import { useSendSignal, useSignals } from "./api";
import styles from "./hiring.module.css";

const resetDate = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

/**
 * "Send serious interest (N of 3 left this week)" (HIR-08). Only the shortlist renders it, and the shortlist holds accepted
 * introductions only (decision G7). Quota is the organization's, read from GET /v1/signals; the server refuses the fourth.
 */
export function SignalButton({ introductionId, roleId, orgId, alreadySent }: { introductionId: string; roleId: string; orgId: string; alreadySent: boolean }) {
  const signals = useSignals(orgId);
  const send = useSendSignal(roleId, orgId);
  const [refused, setRefused] = useState<string | null>(null);
  const quota = signals.data?.quota;
  const left = quota ? Math.max(0, quota.limit - quota.used) : null;
  const exhausted = refused !== null || left === 0;

  if (alreadySent || send.isSuccess) return <p className={`t-small ${styles.muted}`}>You sent serious interest for this candidate.</p>;
  return (
    <div className={styles.stack}>
      <Button
        size="sm"
        isDisabled={exhausted || quota === undefined}
        isPending={send.isPending}
        onPress={() =>
          send.mutate(introductionId, {
            onError: (error) => {
              if (error instanceof ApiFailure && error.code === "quota_exhausted") setRefused(messageFor(error));
            },
          })
        }
      >
        {quota ? `Send serious interest (${left} of ${quota.limit} left this week)` : "Send serious interest"}
      </Button>
      {exhausted && quota && <p className={`t-small ${styles.muted}`}>{refused ?? `No serious-interest signals left this week. They reset on ${resetDate(quota.resets_at)} (UTC).`}</p>}
      {send.isError && refused === null && (
        <p role="alert" className={styles.problem}>
          {messageFor(send.error as ApiFailure)}
        </p>
      )}
    </div>
  );
}
