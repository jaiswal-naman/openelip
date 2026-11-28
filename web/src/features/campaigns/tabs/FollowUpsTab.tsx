"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { describeFailure } from "@/lib/api/problems";
import { reasonText } from "@/lib/labels";
import { useViewer } from "@/lib/viewer";
import { ApprovalPanel } from "../ApprovalPanel";
import { ApprovedChip } from "../ApprovedChip";
import { type CampaignDetail, type FollowUp, usePatchCampaign, useUndo } from "../queries";
import { clockTime } from "../time";
import { UndoCountdown } from "../UndoCountdown";
import styles from "../campaigns.module.css";

const TOAST_MS = 10_000;

const ENDED: Record<string, string> = { rejected: "Skipped", cancelled: "Undone", blocked: "Blocked", failed: "Failed", expired: "Expired" };
const ENDED_BAD = new Set(["blocked", "failed"]);

/** How a follow-up that is no longer waiting ended, with the server's reason in words when there is one. */
function endedText(item: FollowUp): string {
  const head = ENDED[item.status] ?? item.status;
  const why = item.outcome_text ?? (item.outcome_code !== null && item.status !== "rejected" ? reasonText(item.outcome_code) : "");
  return why === "" ? head : `${head}: ${why}`;
}

function AddressForm({ campaign }: { campaign: CampaignDetail }) {
  const patch = usePatchCampaign(campaign.id);
  const [address, setAddress] = useState(campaign.follow_up_email ?? "");
  const [days, setDays] = useState(String(campaign.follow_up_after_days));
  const [saved, setSaved] = useState(false);
  function save() {
    setSaved(false);
    patch.mutate(
      { follow_up_email: address.trim() === "" ? null : address.trim(), follow_up_after_days: Number(days) },
      { onSuccess: () => setSaved(true) },
    );
  }
  return (
    <section aria-labelledby="address-heading" className={`${styles.stack} ${styles.addressBox}`}>
      <h2 id="address-heading" className="t-section">
        Where follow-ups go
      </h2>
      <p>
        Type the hiring contact&apos;s email address yourself. Follow-ups go only to this address, and replies come to you, not to the system.
      </p>
      <label className={styles.field}>
        <span className="t-card">Follow-up address</span>
        <input className={styles.input} type="email" value={address} onChange={(event) => setAddress(event.target.value)} />
      </label>
      <label className={styles.field}>
        <span className="t-card">Quiet days before a follow-up</span>
        <input className={styles.input} type="number" min={1} max={60} value={days} onChange={(event) => setDays(event.target.value)} />
      </label>
      <div className={styles.row}>
        <Button size="sm" variant="primary" isPending={patch.isPending} onPress={save}>
          Save
        </Button>
        {saved && <span role="status">Saved</span>}
      </div>
      {patch.error && (
        <p role="alert" className={styles.problem}>
          {describeFailure(patch.error)} Nothing was saved.
        </p>
      )}
    </section>
  );
}

function DraftView({ followUp }: { followUp: FollowUp }) {
  return (
    <div className={styles.stack}>
      <dl className={styles.defs}>
        <dt>To</dt>
        <dd>{followUp.to} (you typed)</dd>
        <dt>Subject</dt>
        <dd>{followUp.subject}</dd>
      </dl>
      <p className={styles.draft}>{followUp.body}</p>
    </div>
  );
}

/**
 * Follow-ups (CMP-04): the address the candidate typed, then each draft as a row with Approve, Edit and Skip, the undo
 * window with its countdown, and how each one ended. An approved follow-up carries the black Approved chip; it lands once, in the row
 * the person has just approved.
 */
export function FollowUpsTab({ campaign, offsetMs, onApproved }: { campaign: CampaignDetail; offsetMs: number; onApproved: () => void }) {
  const viewer = useViewer();
  const undo = useUndo();
  const [toast, setToast] = useState<number | null>(null); // the action id whose "sends at" toast shows
  const [justApproved, setJustApproved] = useState<number | null>(null); // the action id whose chip lands now
  useEffect(() => {
    if (toast === null) return;
    const timer = window.setTimeout(() => setToast(null), TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [toast]);
  const toasted = campaign.follow_ups.find((item) => item.action_id === toast && item.status === "scheduled" && item.undo_until !== null);

  return (
    <div className={styles.stack}>
      <AddressForm campaign={campaign} />
      {campaign.follow_ups.length === 0 ? (
        <p className="muted">
          {campaign.follow_up_email === null ? "Add the hiring contact's address and the agents will draft a follow-up after the quiet days." : "No follow-up has been drafted yet. One is drafted after the quiet days."}
        </p>
      ) : (
        <ul className={styles.rows} aria-label="Follow-ups">
          {campaign.follow_ups.map((item) => {
            const sent = item.status === "scheduled" || item.status === "executed";
            // Approved in this view and the server has not caught up yet: the chip is already there, the buttons are gone.
            const settling = justApproved === item.action_id && item.status === "awaiting_approval";
            const waiting = item.status === "awaiting_approval" && item.approval_id !== null && !settling;
            return (
              <li key={item.action_id} className={styles.item}>
                <div className={styles.itemHead}>
                  <h2 className="t-card">Follow-up {item.step_key?.split(":")[1]?.split(".")[0] ?? ""}</h2>
                  {waiting && <span className={styles.needsYou}>Waiting for you</span>}
                  {(sent || settling) && <ApprovedChip landing={justApproved === item.action_id} />}
                </div>
                {waiting && item.approval_id !== null ? (
                  <>
                    <DraftView followUp={item} />
                    <ApprovalPanel
                      campaignId={campaign.id}
                      approvalId={item.approval_id}
                      draft={{ subject: item.subject ?? "", body: item.body ?? "" }}
                      approveLabel="Approve"
                      declineLabel="Skip"
                      onApproved={(result) => {
                        setToast(result.actionId);
                        setJustApproved(result.actionId);
                        onApproved();
                      }}
                    />
                  </>
                ) : sent || settling ? (
                  <>
                    <DraftView followUp={item} />
                    {sent ? <UndoCountdown followUp={item} offsetMs={offsetMs} /> : <p role="status">Checking...</p>}
                  </>
                ) : (
                  <>
                    {item.body !== null && <DraftView followUp={item} />}
                    <p className={styles.ended} data-tone={ENDED_BAD.has(item.status) ? "bad" : "muted"}>
                      {endedText(item)}
                    </p>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {toasted !== undefined && toasted.undo_until !== null && (
        <div role="status" className={styles.notice}>
          <span>Sends at {clockTime(toasted.undo_until, viewer.locale)}. </span>
          <Button size="sm" isPending={undo.isPending} onPress={() => undo.mutate(toasted.action_id)}>
            Undo
          </Button>
        </div>
      )}
      <p className="t-small muted">
        At most two follow-ups are sent for one campaign. {campaign.follow_up_email !== null && <>Current address: {campaign.follow_up_email}. </>}
      </p>
    </div>
  );
}
