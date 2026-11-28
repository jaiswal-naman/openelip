import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { meQuery } from "@/features/auth/queries";
import { RecorderView } from "@/features/recorder/RecorderView";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Flight recorder" };

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const one = (value: string | string[] | undefined) => (Array.isArray(value) ? value[0] : value);

/** The flight recorder of one campaign (GRD-03): every step the agents took, replayed one at a time. No session goes to sign in and back. */
export default async function RecorderPage({ searchParams }: Props) {
  const asked = await searchParams;
  const campaignId = one(asked.campaign_id);
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) {
    const wanted = new URLSearchParams();
    for (const name of ["campaign_id", "step"]) {
      const value = one(asked[name]);
      if (value !== undefined) wanted.set(name, value);
    }
    redirect(`/sign-in?next=${encodeURIComponent(`/control/recorder?${wanted.toString()}`)}`);
  }
  return (
    <div className="container page">
      {campaignId !== undefined && UUID.test(campaignId) ? (
        <RecorderView campaignId={campaignId} />
      ) : (
        <>
          <TitleBlock title="Flight recorder" />
          <p>
            Open a campaign and choose a step from its Timeline. <Link href="/app/campaigns">Your campaigns</Link>
          </p>
        </>
      )}
    </div>
  );
}
