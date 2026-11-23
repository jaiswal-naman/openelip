import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { recordQuery, unconfirmedFactsQuery } from "@/features/record/api";
import { RecordPage } from "@/features/record/RecordPage";
import { subprocessorsQuery } from "@/features/trust/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Your record" };

/**
 * No session cookie, or one the API no longer knows, goes to sign in and comes back here. If the API cannot answer at
 * all, the page is shown and each region says what failed with Retry. What the page needs first is fetched here.
 */
export default async function RecordRoute() {
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent("/app/record")}`);
  await Promise.all([
    queryClient.prefetchQuery(recordQuery(api)),
    queryClient.prefetchQuery(unconfirmedFactsQuery(api)),
    queryClient.prefetchQuery(subprocessorsQuery(api)),
  ]);

  return (
    <div className="container page">
      <HydrationBoundary state={dehydrate(queryClient)}>
        <RecordPage />
      </HydrationBoundary>
    </div>
  );
}
