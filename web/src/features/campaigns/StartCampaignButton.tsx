"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { useStartCampaign } from "./queries";
import styles from "./campaigns.module.css";

/**
 * "Start campaign": a campaign begins only when the candidate presses this (CMP-01); nothing is called before the press.
 * It opens the new campaign. A job that already has one links to it; a closed job and a broken constraint say so here.
 */
export function StartCampaignButton({ jobId, label = "Start campaign", onStarted }: { jobId: number; label?: string; onStarted?: (campaignId: string) => void }) {
  const router = useRouter();
  const start = useStartCampaign();
  const existing = isFailure(start.error, "conflict") && typeof start.error.details.campaign_id === "string" ? start.error.details.campaign_id : null;

  function press() {
    start.mutate(jobId, {
      onSuccess: (campaign) => {
        if (onStarted) onStarted(campaign.id);
        else router.push(`/app/campaigns/${campaign.id}`);
      },
    });
  }

  return (
    <div className={styles.stack}>
      <Button variant="primary" isPending={start.isPending} onPress={press}>
        {label}
      </Button>
      {existing !== null ? (
        <p role="alert" className={styles.problem}>
          You already have a campaign for this job. <Link href={`/app/campaigns/${existing}`}>Open it</Link>.
        </p>
      ) : (
        start.error && (
          <p role="alert" className={styles.problem}>
            {describeFailure(start.error)} No campaign was started.
          </p>
        )
      )}
    </div>
  );
}
