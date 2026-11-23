"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { CrewPlate } from "@/components/ui/CrewPlate";
import { Pill } from "@/components/ui/Pill";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { LocalTime } from "@/components/ui/Viewer";
import { RecordStatusMenu } from "@/features/applications/RecordStatusMenu";
import { recorderQuery } from "@/features/recorder/queries";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { ACTION_STATUS_LABELS, CAMPAIGN_STAGE_LABELS, CLOSED_REASON_LABELS, PAUSE_LABELS, scamSignalText } from "@/lib/labels";
import { type PacketRecord, packetRecord } from "./approvals";
import { CancelDialog } from "./CancelDialog";
import { type CampaignDetail, useCampaignControl, usePatchCampaign } from "./queries";
import styles from "./campaigns.module.css";

const usd = (value: string) => Number(value).toFixed(2);

/**
 * The cells of the title block: what the sheet says about who drew it, who checked it and who approved it. Each cell comes
 * from the campaign or from the flight recorder's rows, and a cell with nothing true to say is left out.
 */
function titleCells(campaign: CampaignDetail, record: PacketRecord | null): TitleCell[] {
  const closed = campaign.stage === "closed";
  const waiting = campaign.pending_approvals.length > 0;
  const cells: TitleCell[] = [
    { label: "Stage", value: <Pill tone={campaign.stage === "needs_you" ? "redline" : "stage"}>{CAMPAIGN_STAGE_LABELS[campaign.stage]}</Pill> },
  ];
  // Who has the next step: an agent, or no cell at all. When the person has it, the approval cell below says so.
  if (!closed && !waiting && campaign.next_step !== null) cells.push({ label: "Next step", value: <CrewPlate role={campaign.next_step.agent_role} /> });
  const drawnBy = campaign.packet === null ? undefined : campaign.steps.find((step) => step.step_key === "packet_draft" && step.status === "done");
  if (drawnBy !== undefined) cells.push({ label: "Drawn by", value: <CrewPlate role={drawnBy.agent_role} /> });
  if (record?.checks != null) {
    const clean = record.checks.passed === record.checks.total;
    cells.push({
      label: "Checked by",
      tone: clean ? "ok" : undefined,
      value: (
        <>
          <CrewPlate role="guardian" />
          <span className={styles.sub}>
            {record.checks.passed} of {record.checks.total} checks passed
          </span>
        </>
      ),
    });
  }
  if (waiting) {
    cells.push({ label: "Your approval", tone: "redline", value: ACTION_STATUS_LABELS.awaiting_approval });
  } else if (campaign.packet?.status === "approved") {
    cells.push({
      label: "Your approval",
      tone: "ok",
      value: (
        <>
          Approved
          {record?.approvedAt != null && (
            <span className={styles.sub}>
              <LocalTime iso={record.approvedAt} />
            </span>
          )}
        </>
      ),
    });
  }
  cells.push({
    label: "Cost so far",
    value: (
      <>
        USD {usd(campaign.spent_usd)}
        <span className={styles.sub}>of USD {usd(campaign.budget_usd)} budget</span>
      </>
    ),
  });
  return cells;
}

/** The campaign's title block: who and what, the stage, who drew, checked and approved, the cost against the budget, and the controls (CMP-07, GRD-07). */
export function CampaignHeader({ campaign, applicationId }: { campaign: CampaignDetail; applicationId: string | null }) {
  const api = useApi();
  const control = useCampaignControl(campaign.id);
  const patch = usePatchCampaign(campaign.id);
  // The same rows the Timeline tab lists, so the title block never asks for anything the page does not already load.
  const rows = useQuery(recorderQuery(api, campaign.id));
  const [budget, setBudget] = useState((Number(campaign.budget_usd) + 0.5).toFixed(2));
  const closed = campaign.stage === "closed";
  const paused = campaign.paused_at !== null;
  const next = !closed && campaign.pending_approvals.length === 0 ? campaign.next_step : null;

  async function raiseAndResume() {
    try {
      await patch.mutateAsync({ budget_usd: budget });
      await control.resume.mutateAsync();
    } catch {
      /* the error shows from the mutation state below */
    }
  }

  return (
    <div className={styles.stack}>
      <TitleBlock
        title={campaign.job.title}
        lines={
          <>
            <p className={styles.company}>{campaign.job.company_name.trim() === "" ? "Company not stated" : campaign.job.company_name}</p>
            {next !== null && <p className="t-small">{next.text}</p>}
            {(campaign.pause_reason !== null || (closed && campaign.closed_reason !== null) || campaign.from_introduction) && (
              <div className={styles.row}>
                {campaign.pause_reason !== null && <Pill tone="warn">{PAUSE_LABELS[campaign.pause_reason]}</Pill>}
                {closed && campaign.closed_reason !== null && <span>{CLOSED_REASON_LABELS[campaign.closed_reason] ?? "Closed"}</span>}
                {campaign.from_introduction && <span className={styles.tag}>From an introduction</span>}
              </div>
            )}
            {campaign.introduction !== null && campaign.introduction.answer_due_at !== null && campaign.introduction.employer_answer === null && (
              <p>
                The employer answers by <LocalTime iso={campaign.introduction.answer_due_at} />.
              </p>
            )}
          </>
        }
        cells={titleCells(campaign, packetRecord(rows.data))}
        actions={
          !closed && (
            <>
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
            </>
          )
        }
      />
      {campaign.pause_reason === "budget_exhausted" && !closed && (
        <div className={styles.banner} role="group" aria-label="Budget reached">
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
    </div>
  );
}
