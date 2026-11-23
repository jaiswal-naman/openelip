import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { loadMatch } from "@/features/matches/loadMatch";
import { ChatContext } from "@/features/chat/ChatContext";
import { TailorView } from "@/features/tailor/TailorView";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Tailor resume" };

type Props = { params: Promise<{ id: string }> };

/**
 * Tailor the resume for one match (TLR-01 to TLR-03, APP-02). The id is the match's decision id, as on the match page. Only a
 * shown match can be tailored: a job the constraints filtered or the run withheld is the not-found page. No session goes to
 * sign in and comes back here.
 */
export default async function TailorRoute({ params }: Props) {
  const { id } = await params;
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/app/matches/${id}/tailor`)}`);
  const loaded = await loadMatch(await serverApi(), id);
  if (loaded.kind !== "match") notFound();
  const { job } = loaded.match;
  return (
    <div className="container page">
      <ChatContext jobId={job.id} company={job.company_name} />
      <TailorView jobId={job.id} header={{ title: job.title, company: job.company_name }} />
    </div>
  );
}
