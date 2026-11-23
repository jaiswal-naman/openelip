"use client";

import Link from "next/link";
import { StartCampaignButton } from "@/features/campaigns/StartCampaignButton";
import { LinkButton } from "@/components/ui/LinkButton";
import type { CampaignProposal } from "./reply";
import styles from "./chat.module.css";

/**
 * The chat's offer to start a campaign (CMP-01): the job, and a button. Nothing is called until the candidate presses it;
 * a job that already has a campaign offers to open it. The words are text, whatever the posting said.
 */
export function CampaignCard({ offer }: { offer: CampaignProposal }) {
  return (
    <section className={styles.proposal} aria-label="Suggested campaign">
      <p className={`t-card ${styles.proposalTitle}`}>Start a campaign?</p>
      <p>
        {offer.title}
        {offer.company_name.trim() === "" ? "" : ` at ${offer.company_name}`}
      </p>
      <div className={styles.action}>
        {offer.campaign_id !== null ? (
          <LinkButton href={`/app/campaigns/${offer.campaign_id}`}>Open campaign</LinkButton>
        ) : (
          <StartCampaignButton jobId={offer.job_id} />
        )}
      </div>
      <p className="t-small muted">
        The agents research the company and prepare your packet. Nothing is sent without your approval. <Link href="/app/campaigns">Your campaigns</Link>
      </p>
    </section>
  );
}
