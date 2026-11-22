import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { costsQuery } from "@/features/control/queries";
import { CostsView } from "@/features/control/CostsView";
import { serverApi } from "@/lib/api/server";
import { isFailure } from "@/lib/api/errors";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "My model cost" };

/**
 * proxy.ts has already sent someone with no session cookie to sign in. This asks the API, which is what actually decides:
 * a cookie that is expired or revoked gets the same redirect. Any other failure is left for the client to show with Retry.
 */
export default async function CostsPage() {
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const results = await Promise.allSettled([
    queryClient.fetchQuery(costsQuery(api, "month")),
    queryClient.fetchQuery(costsQuery(api, "task")),
  ]);
  if (results.some((result) => result.status === "rejected" && isFailure(result.reason, "unauthenticated"))) {
    redirect(`/sign-in?next=${encodeURIComponent("/control/costs")}`);
  }

  return (
    <div className="container page">
      <HydrationBoundary state={dehydrate(queryClient)}>
        <CostsView />
      </HydrationBoundary>
    </div>
  );
}
