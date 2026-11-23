import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { cache } from "react";
import { meQuery } from "@/features/auth/queries";
import { HeldDecision } from "@/features/matches/HeldDecision";
import { loadMatch } from "@/features/matches/loadMatch";
import { MatchDetail } from "@/features/matches/MatchDetail";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

type Props = { params: Promise<{ id: string }> };

// The page and its title both need the match; `cache` lets one request ask the API once for both.
const getMatch = cache(async (id: string) => loadMatch(await serverApi(), id));

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  try {
    const loaded = await getMatch((await params).id);
    const job = loaded.kind === "match" ? loaded.match.job : loaded.decision.job;
    return { title: `${job.company_name} match: ${job.title}` };
  } catch {
    // Not found, or not signed in: the page itself says so. The title only needs to be something.
    return { title: "Match" };
  }
}

/**
 * One match (MAT-03, MAT-04): the requirement table with the evidence, and both texts it cites. No session goes to sign in
 * and comes back here. The decision id is the match's id; another person's id, an unknown id and an address that is not an
 * id all show the not-found page (threat T-04-35). A link to a job that was filtered or withheld says so instead.
 */
export default async function MatchRoute({ params }: Props) {
  const { id } = await params;
  const me = await makeQueryClient().fetchQuery(meQuery(await serverApi())).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/app/matches/${id}`)}`);
  const loaded = await getMatch(id);
  return <div className="container page">{loaded.kind === "match" ? <MatchDetail match={loaded.match} /> : <HeldDecision decision={loaded.decision} />}</div>;
}
