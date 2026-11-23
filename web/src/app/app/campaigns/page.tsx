import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { Suspense } from "react";
import { meQuery } from "@/features/auth/queries";
import { Board } from "@/features/campaigns/Board";
import { CampaignsTitle } from "@/features/campaigns/CampaignsTitle";
import { ApplicationsSection } from "@/features/applications/ApplicationsSection";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Applications" };

/** The place applications are tracked (APP-01). The campaign board comes first, the Phase 5 tracker below it. No session goes to sign in and back. */
export default async function CampaignsRoute() {
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent("/app/campaigns")}`);
  return (
    <div className="container page">
      <CampaignsTitle />
      <Suspense fallback={<p role="status">Loading your campaigns...</p>}>
        <Board />
      </Suspense>
      <ApplicationsSection />
    </div>
  );
}
