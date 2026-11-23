"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { LocalTime } from "@/components/ui/Viewer";
import { RecordStatusMenu } from "@/features/applications/RecordStatusMenu";
import { describeFailure } from "@/lib/api/problems";
import { CAMPAIGN_STAGE_LABELS, CLOSED_REASON_LABELS, PAUSE_LABELS, scamSignalText } from "@/lib/labels";
import { CancelDialog } from "./CancelDialog";
import { type CampaignDetail, useCampaignControl, usePatchCampaign } from "./queries";
import styles from "./campaigns.module.css";

const usd = (value: string) => Number(value).toFixed(2);

/** The campaign's header: who and what, the stage, the cost against the budget, and the controls (CMP-07, GRD-07). */
export function CampaignHeader({ campaign, applicationId }: { campaign: CampaignDetail; applicationId: string | null }) {
  const control = useCampaignControl(campaign.id);
  const patch = usePatchCampaign(campaign.id);
  const [budget, setBudget] = useState((Number(campaign.budget_usd) + 0.5).toFixed(2));
  const closed = campaign.stage === "closed";
  const paused = campaign.paused_at !== null;

  async function raiseAndResume() {
    try {
      await patch.mutateAsync({ budget_usd: budget });
      await control.resume.mutateAsync();
    } catch {
      /* the error shows from the mutation state below */
    }
  }

  return (
    <header className={styles.head}>
      <h1 className="t-page">{campaign.job.title}</h1>
      <p className="muted">{campaign.job.company_name.trim() === "" ? "Company not stated" : campaign.job.company_name}</p>
      <div className={styles.row}>
        <Pill tone={closed ? "unk" : campaign.stage === "needs_you" ? "warn" : "ok"}>{CAMPAIGN_STAGE_LABELS[campaign.stage]}</Pill>
        {campaign.pause_reason !== null && <Pill tone="warn">{PAUSE_LABELS[campaign.pause_reason]}</Pill>}
        {closed && campaign.closed_reason !== null && <span>{CLOSED_REASON_LABELS[campaign.closed_reason] ?? "Closed"}</span>}
        {campaign.from_introduction && <span className={styles.tag}>From an introduction</span>}
      </div>
      <p>
        Cost so far USD {usd(campaign.spent_usd)} of USD {usd(campaign.budget_usd)} budget
      </p>
      {campaign.introduction !== null && campaign.introduction.answer_due_at !== null && campaign.introduction.employer_answer === null && (
        <p>
          The employer answers by <LocalTime iso={campaign.introduction.answer_due_at} />.
        </p>
      )}
      {!closed && (
        <div className={styles.row}>
          {paused ? (
            campaign.pause_reason === "budget_exhausted" ? null : (
              <Button variant="primary" size="sm" isPending={control.resume.isPending} onPress={() => control.resume.mutate()}>
                {campaign.pause_reason === "scam_suspected" ? "Resume anyway" : "Resume"}
              </Button>
            )
          ) : (
            <Button size="sm" isPending={control.pause.isPending} onPress={() => control.pause.mutate()}>
              Pause
            </Button>
          )}
          <CancelDialog campaignId={campaign.id} stage={campaign.stage} />
          {applicationId !== null && <RecordStatusMenu applicationId={applicationId} title={campaign.job.title} />}
        </div>
      )}
      {campaign.pause_reason === "budget_exhausted" && !closed && (
        <div className={`${styles.banner}`} role="group" aria-label="Budget reached">
          <p>Paused: this campaign reached its USD {usd(campaign.budget_usd)} budget. Raise the budget and resume, or cancel.</p>
          <label className={styles.field}>
            <span className="t-card">New budget (USD)</span>
            <input className={styles.input} inputMode="decimal" value={budget} onChange={(event) => setBudget(event.target.value)} />
          </label>
          <div className={styles.row}>
            <Button variant="primary" size="sm" isPending={patch.isPending || control.resume.isPending} onPress={() => void raiseAndResume()}>
              Raise budget and resume
            </Button>
          </div>
        </div>
      )}
      {campaign.pause_reason === "scam_suspected" && !closed && (
        <div className={`${styles.banner} ${styles.bannerBad}`} role="group" aria-label="Possible scam">
          <p>Paused: this posting looks like a scam, so nothing was shared. Resume only if you are sure it is real.</p>
          <ul>
            {campaign.scam_signals.map((signal) => (
              <li key={`${signal.kind}:${signal.quote}`}>
                {scamSignalText(signal.kind)} The posting says: &ldquo;{signal.quote}&rdquo;
              </li>
            ))}
          </ul>
        </div>
      )}
      {campaign.pause_reason === "candidate" && !closed && <p>{campaign.pause_text ?? "You paused this campaign."}</p>}
      {(control.pause.error || control.resume.error || patch.error) && (
        <p role="alert" className={styles.problem}>
          {describeFailure(control.pause.error ?? control.resume.error ?? patch.error)}
        </p>
      )}
    </header>
  );
}
