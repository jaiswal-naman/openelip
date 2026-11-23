import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import styles from "@/features/privacy/privacy.module.css";
import { PrivacySection } from "@/features/privacy/PrivacySection";
import { subprocessorsQuery } from "@/features/trust/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Settings" };

/**
 * Sections are URL state (?section=). Privacy is the only one in this phase, so it is the default and anything else
 * shows it too; later phases add theirs to the list. No session goes to sign in and comes back here.
 */
export default async function SettingsPage() {
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent("/app/settings?section=privacy")}`);
  await queryClient.prefetchQuery(subprocessorsQuery(api));

  return (
    <div className="container page">
      <header>
        <h1 className="t-page">Settings</h1>
      </header>
      <div className={styles.layout}>
        <nav aria-label="Settings sections" className={styles.nav}>
          <ul>
            <li>
              <Link href="/app/settings?section=privacy" aria-current="page">
                Privacy
              </Link>
            </li>
          </ul>
        </nav>
        <HydrationBoundary state={dehydrate(queryClient)}>
          <PrivacySection />
        </HydrationBoundary>
      </div>
    </div>
  );
}
