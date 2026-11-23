"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { ErrorState } from "@/components/ui/States";
import { safeHttpUrl } from "@/features/jobs/jobs";
import { packetFileUrl, useRecordApplied } from "@/features/tailor/queries";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { PACKET_QUESTION_LABELS } from "@/lib/labels";
import { ApprovalPanel } from "../ApprovalPanel";
import { type CampaignDetail, packetQuery } from "../queries";
import styles from "../campaigns.module.css";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <span className={styles.row}>
      <Button
        size="sm"
        aria-label={label}
        onPress={() => {
          navigator.clipboard.writeText(text).then(
            () => setState("copied"),
            () => setState("failed"),
          );
        }}
      >
        Copy
      </Button>
      <span role="status" className="t-small">
        {state === "copied" ? "Copied" : state === "failed" ? "Could not copy. Select the text and copy it." : ""}
      </span>
    </span>
  );
}

/**
 * The campaign's application packet (CMP-03): the note and the answers, "You answer this" where only the candidate can, and one
 * Approve packet. After approval it says "Checking..." until the server has scheduled or blocked the release, then shows the
 * submit kit. The system never submits anything.
 */
export function PacketTab({ campaign, applicationId, onApproved }: { campaign: CampaignDetail; applicationId: string | null; onApproved: () => void }) {
  const api = useApi();
  const summary = campaign.packet;
  const packet = useQuery({ ...packetQuery(api, summary?.id ?? ""), enabled: summary !== null });
  const [approved, setApproved] = useState(false);
  const applied = useRecordApplied(campaign.job.id, summary?.id ?? "");
  if (summary === null) return <p className="muted">The packet is not ready yet. The crew prepares it after the dossier.</p>;
  if (packet.isError) return <ErrorState what="Could not load the packet." error={packet.error} onRetry={() => void packet.refetch()} />;
  const data = packet.data;
  const waiting = summary.approval_id !== null && campaign.pending_approvals.some((item) => item.id === summary.approval_id);
  const released = summary.action_status === "scheduled" || summary.action_status === "executed";
  const blocked = summary.action_status === "blocked";
  const checking = !waiting && !released && !blocked && (approved || summary.status === "approved" || summary.action_status === "awaiting_approval");
  const posting = safeHttpUrl(campaign.job.apply_url);

  return (
    <div className={styles.stack}>
      {data === undefined ? (
        <p role="status">Loading the packet...</p>
      ) : (
        <>
          {data.cover_note !== null && data.cover_note !== "" && (
            <div className={styles.stack}>
              <h2 className="t-card">Short note</h2>
              <p className={styles.draft}>{data.cover_note}</p>
              {released && <CopyButton text={data.cover_note} label="Copy the short note" />}
            </div>
          )}
          <ul className={styles.list}>
            {data.answers.map((answer) => {
              const question = PACKET_QUESTION_LABELS[answer.question_id] ?? answer.question;
              return (
                <li key={answer.question_id} className={styles.item}>
                  <h2 className="t-card">{question}</h2>
                  {answer.status === "drafted" ? (
                    <>
                      <p className={styles.draft}>{answer.answer}</p>
                      {released && <CopyButton text={answer.answer} label={`Copy the answer to: ${question}`} />}
                    </>
                  ) : (
                    <p className="t-card">You answer this</p>
                  )}
                </li>
              );
            })}
          </ul>
          <p className="t-small">
            {data.removed_sentences === 1
              ? "1 drafted sentence was removed because your record does not support it."
              : `${data.removed_sentences} drafted sentences were removed because your record does not support them.`}
          </p>
        </>
      )}

      {waiting && summary.approval_id !== null && (
        <ApprovalPanel campaignId={campaign.id} approvalId={summary.approval_id} approveLabel="Approve packet" onApproved={() => { setApproved(true); onApproved(); }} />
      )}
      {checking && <p role="status">Checking...</p>}
      {blocked && (
        <p role="alert" className={styles.problem}>
          The Guardian did not release this packet. The Timeline says why.
        </p>
      )}
      {released && (
        <section aria-labelledby="kit-heading" className={styles.stack}>
          <h2 id="kit-heading" className="t-section">
            Your submit kit
          </h2>
          <div className={styles.row}>
            <a href={packetFileUrl(summary.id)} download className={buttonClass("secondary")}>
              Download the resume packet
            </a>
            {posting !== null && (
              <a href={posting} target="_blank" rel="noopener noreferrer nofollow" className={buttonClass("secondary")} aria-label={`Open the posting for ${campaign.job.title} (opens in a new tab)`}>
                Open the posting
              </a>
            )}
          </div>
          <p>The system never submits. Record Applied once you have.</p>
          {applicationId !== null || applied.isSuccess ? (
            <p role="status">Recorded as applied.</p>
          ) : (
            <div className={styles.row}>
              <Button variant="primary" isPending={applied.isPending} onPress={() => applied.mutate()}>
                Record that I applied
              </Button>
            </div>
          )}
          {applied.error && (
            <p role="alert" className={styles.problem}>
              {describeFailure(applied.error)} Nothing was recorded.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
