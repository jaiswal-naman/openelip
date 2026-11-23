import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { OnboardingFlow } from "@/features/onboarding/OnboardingFlow";
import { recordQuery } from "@/features/record/api";
import { subprocessorsQuery } from "@/features/trust/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Set up your record" };

/**
 * No session cookie, or one the API no longer knows, goes to sign in and comes back here. If the API cannot answer at
 * all, the page is shown and the client says what failed with Retry. The data the first step needs is fetched here, so the
 * first paint has it.
 */
export default async function OnboardingPage() {
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent("/onboarding")}`);
  await Promise.all([queryClient.prefetchQuery(subprocessorsQuery(api)), queryClient.prefetchQuery(recordQuery(api))]);

  return (
    <div className="container page">
      <HydrationBoundary state={dehydrate(queryClient)}>
        <OnboardingFlow />
      </HydrationBoundary>
    </div>
  );
}
