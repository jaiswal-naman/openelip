import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { latestBriefQuery, todayQuery, whyNotQuery } from "@/features/matches/queries";
import { TodayView } from "@/features/matches/TodayView";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Today" };

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

const one = (value: string | string[] | undefined) => (Array.isArray(value) ? value[0] : value);

/**
 * Today. No session cookie, or one the API no longer knows, goes to sign in and comes back here with the tab or the
 * decision an email link named. If the API cannot answer at all, the page is shown and each region says what failed with
 * Retry. The brief and today's matches are fetched here so the first paint has them; what was held back is fetched only
 * when that tab is the one asked for.
 */
export default async function TodayRoute({ searchParams }: Props) {
  const asked = await searchParams;
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) {
    const wanted = new URLSearchParams();
    for (const name of ["view", "decision"]) {
      const value = one(asked[name]);
      if (value !== undefined) wanted.set(name, value);
    }
    const query = wanted.toString();
    redirect(`/sign-in?next=${encodeURIComponent(query === "" ? "/app/today" : `/app/today?${query}`)}`);
  }
  await Promise.all([queryClient.prefetchQuery(todayQuery(api)), queryClient.prefetchQuery(latestBriefQuery(api))]);
  const date = queryClient.getQueryData(todayQuery(api).queryKey)?.local_date;
  if (one(asked.view) === "why-not" && date !== undefined) await queryClient.prefetchQuery(whyNotQuery(api, date));

  return (
    <div className="container page">
      <HydrationBoundary state={dehydrate(queryClient)}>
        <TodayView />
      </HydrationBoundary>
    </div>
  );
}
