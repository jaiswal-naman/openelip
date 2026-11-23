import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { safeNextPath } from "@/features/auth/safeNext";
import { SetupForm } from "@/features/local/SetupForm";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Set up your model" };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

/**
 * Local mode: where the first visit asks for the model API key. No session, or one the API no longer knows, goes through
 * sign-in (which in local mode opens the owner's session) and comes back here. If the API cannot answer, the page is shown
 * and the form says what failed.
 */
export default async function SetupPage({ searchParams }: { searchParams: SearchParams }) {
  const next = safeNextPath((await searchParams).next);
  const me = await makeQueryClient()
    .fetchQuery(meQuery(await serverApi()))
    .catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/setup?next=${encodeURIComponent(next)}`)}`);
  return (
    <div className="container page">
      <SetupForm next={next} />
    </div>
  );
}
