"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { isFailure } from "@/lib/api/errors";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { approvalQuery, campaignKeys, useApprove, useReject } from "./queries";
import styles from "./campaigns.module.css";

type Props = {
  campaignId: string;
  approvalId: string;
  /** Set for a follow-up: the draft the candidate may edit, and the label of the button that declines it. */
  draft?: { subject: string; body: string };
  approveLabel: string;
  declineLabel?: string;
  onApproved?: (result: { actionId: number; status: string }) => void;
};

/**
 * Approve, Edit and Skip for one waiting approval (CMP-03, CMP-04). The page sends back the `payload_hash` of the draft it
 * showed, so a draft that changed meanwhile is refused (409 preview_changed) and shown again instead of approved. Edits are
 * only `subject` and `body`. Whether it is scheduled comes back from the server, never from this button.
 */
export function ApprovalPanel({ campaignId, approvalId, draft, approveLabel, declineLabel, onApproved }: Props) {
  const api = useApi();
  const client = useQueryClient();
  const approval = useQuery(approvalQuery(api, approvalId));
  const approve = useApprove(campaignId);
  const reject = useReject(campaignId);
  const [editing, setEditing] = useState(false);
  const [subject, setSubject] = useState(draft?.subject ?? "");
  const [body, setBody] = useState(draft?.body ?? "");
  const [changed, setChanged] = useState(false);
  const error = approve.error ?? reject.error;
  const staleDraft = isFailure(approve.error, "preview_changed");

  function reload() {
    setChanged(true);
    setEditing(false);
    setSubject(draft?.subject ?? "");
    setBody(draft?.body ?? "");
    void client.invalidateQueries({ queryKey: ["approvals", approvalId] });
    void client.invalidateQueries({ queryKey: campaignKeys.all });
  }

  function send(withEdits: boolean) {
    if (approval.data === undefined) return;
    setChanged(false);
    approve.mutate(
      { approvalId, payloadHash: approval.data.payload_hash, edits: withEdits ? { subject, body } : undefined },
      {
        onSuccess: (result) => onApproved?.({ actionId: result.action.id, status: result.action.status }),
        onError: (failure) => {
          if (isFailure(failure, "preview_changed") || isFailure(failure, "already_resolved")) reload();
        },
      },
    );
  }

  return (
    <div className={styles.stack}>
      {editing && (
        <div className={styles.stack}>
          <label className={styles.field}>
            <span className="t-card">Subject</span>
            <input className={styles.input} value={subject} onChange={(event) => setSubject(event.target.value)} />
          </label>
          <label className={styles.field}>
            <span className="t-card">Message</span>
            <textarea className={styles.textarea} value={body} onChange={(event) => setBody(event.target.value)} />
          </label>
        </div>
      )}
      <div className={styles.row}>
        <Button variant="primary" size="sm" isPending={approve.isPending} onPress={() => send(editing)} isDisabled={approval.data === undefined}>
          {editing ? "Approve with my edits" : approveLabel}
        </Button>
        {draft !== undefined && (
          <Button size="sm" onPress={() => setEditing((on) => !on)}>
            {editing ? "Stop editing" : "Edit"}
          </Button>
        )}
        {declineLabel !== undefined && (
          <Button size="sm" isPending={reject.isPending} onPress={() => reject.mutate({ approvalId, note: "Skipped by you" })}>
            {declineLabel}
          </Button>
        )}
      </div>
      {changed && <p role="status" className={styles.notice}>This changed since you opened it. Read the latest version before you decide.</p>}
      {error && !staleDraft && !changed && (
        <p role="alert" className={styles.problem}>
          {describeFailure(error)}
        </p>
      )}
      {approval.isError && (
        <p role="alert" className={styles.problem}>
          Could not load this approval. {describeFailure(approval.error)}
        </p>
      )}
    </div>
  );
}
