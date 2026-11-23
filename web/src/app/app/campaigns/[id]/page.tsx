import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { CampaignView } from "@/features/campaigns/CampaignView";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Campaign" };

type Props = { params: Promise<{ id: string }> };

/** One campaign. No session goes to sign in and comes back here; an id that is not the person's shows the not-found message. */
export default async function CampaignRoute({ params }: Props) {
  const { id } = await params;
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/app/campaigns/${id}`)}`);
  return (
    <div className="container page">
      <CampaignView id={id} />
    </div>
  );
}
