import { notFound, redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

/**
 * The server-side check of a hiring-desk page. Nobody signed in goes to sign in and comes back to `path` (a deep link
 * wins). Someone who belongs to no organization gets the Phase 1 not-found page, except on /hire/roles where they create or
 * join one. If the API cannot answer, the page is shown and each region says what failed. The API decides who sees what.
 */
export async function guardHiringPage(path: string, options: { allowNoOrganization?: boolean } = {}): Promise<void> {
  const me = await makeQueryClient()
    .fetchQuery(meQuery(await serverApi()))
    .catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(path)}`);
  if (me !== undefined && !options.allowNoOrganization && (me.organizations ?? []).length === 0) notFound();
}
