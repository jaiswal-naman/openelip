import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { ApplicationsSection } from "@/features/applications/ApplicationsSection";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Applications" };

/** The place applications are tracked (APP-01). Phase 7 adds the campaign board around this section. No session goes to sign in and back. */
export default async function CampaignsRoute() {
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent("/app/campaigns")}`);
  return (
    <div className="container page">
      <h1 className="t-page">Applications</h1>
      <p className="muted">What you applied for, and how each one went. You record every change yourself; nothing here is sent anywhere.</p>
      <ApplicationsSection />
    </div>
  );
}
