"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { ErrorState } from "@/components/ui/States";
import { applicationsQuery } from "@/features/applications/queries";
import { ApiFailure } from "@/lib/api/errors";
import { useApi } from "@/lib/api/context";
import { CampaignHeader } from "./CampaignHeader";
import { APPROVAL_POLL_LIMIT_MS, APPROVAL_POLL_MS, CREW_REFRESH_MS, campaignQuery } from "./queries";
import { CrewTab } from "./tabs/CrewTab";
import { DossierTab } from "./tabs/DossierTab";
import { FollowUpsTab } from "./tabs/FollowUpsTab";
import { PacketTab } from "./tabs/PacketTab";
import { PrepTab } from "./tabs/PrepTab";
import { TimelineTab } from "./tabs/TimelineTab";
import { serverOffsetMs } from "./time";
import styles from "./campaigns.module.css";

const TABS = [
  { id: "timeline", label: "Timeline" },
  { id: "packet", label: "Packet" },
  { id: "dossier", label: "Dossier" },
  { id: "crew", label: "Crew" },
  { id: "prep", label: "Prep" },
  { id: "follow-ups", label: "Follow-ups" },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** Every second until the deadline set by an approval, else every 15 seconds. */
function pollInterval(pollUntil: number): number {
  return Date.now() < pollUntil ? APPROVAL_POLL_MS : CREW_REFRESH_MS;
}

/**
 * One campaign (FRONTEND_UI 3.7): the header with its controls, and six tabs held in `?tab=`. The page asks again every 15
 * seconds, and every second for up to 30 seconds after an approval, until the server says the action is scheduled or blocked.
 * A campaign from an introduction has no Packet or Follow-ups tab: the candidate applies to an employer who already asked.
 */
export function CampaignView({ id }: { id: string }) {
  const api = useApi();
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const [pollUntil, setPollUntil] = useState(0);
  const campaign = useQuery(campaignQuery(api, id, () => pollInterval(pollUntil)));
  const applications = useQuery(applicationsQuery(api));

  if (campaign.isPending) return <p role="status">Loading the campaign...</p>;
  if (campaign.isError) {
    if (campaign.error instanceof ApiFailure && campaign.error.status === 404) {
      return <p>This campaign does not exist, or it is not yours.</p>;
    }
    return <ErrorState what="Could not load the campaign." error={campaign.error} onRetry={() => void campaign.refetch()} />;
  }

  const data = campaign.data;
  const offsetMs = serverOffsetMs(data.server_time, campaign.dataUpdatedAt);
  const tabs = TABS.filter((tab) => !data.from_introduction || (tab.id !== "packet" && tab.id !== "follow-ups"));
  const asked = params.get("tab");
  const selected: TabId = tabs.find((tab) => tab.id === asked)?.id ?? tabs[0].id;
  const applicationId = (applications.data ?? []).find((item) => item.job_id === data.job.id)?.id ?? null;
  const approvedNow = () => setPollUntil(Date.now() + APPROVAL_POLL_LIMIT_MS);

  function select(tab: TabId) {
    const next = new URLSearchParams(params.toString());
    next.set("tab", tab);
    router.replace(`${path}?${next.toString()}`, { scroll: false });
  }

  return (
    <div className={styles.page}>
      <CampaignHeader campaign={data} applicationId={applicationId} />
      <Tabs selectedKey={selected} onSelectionChange={(key) => select(String(key) as TabId)}>
        <TabList aria-label="Campaign" className={styles.tabList}>
          {tabs.map((tab) => (
            <Tab key={tab.id} id={tab.id} className={styles.tab}>
              {tab.label}
            </Tab>
          ))}
        </TabList>
        <TabPanel id="timeline" className={styles.panel}>
          <TimelineTab campaignId={data.id} />
        </TabPanel>
        <TabPanel id="packet" className={styles.panel}>
          <PacketTab campaign={data} applicationId={applicationId} onApproved={approvedNow} />
        </TabPanel>
        <TabPanel id="dossier" className={styles.panel}>
          <DossierTab dossier={data.dossier} />
        </TabPanel>
        <TabPanel id="crew" className={styles.panel}>
          <CrewTab steps={data.steps} />
        </TabPanel>
        <TabPanel id="prep" className={styles.panel}>
          <PrepTab prep={data.prep} />
        </TabPanel>
        <TabPanel id="follow-ups" className={styles.panel}>
          <FollowUpsTab campaign={data} offsetMs={offsetMs} onApproved={approvedNow} />
        </TabPanel>
      </Tabs>
    </div>
  );
}
