import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { subprocessorsQuery } from "@/features/trust/queries";
import styles from "@/features/trust/trust.module.css";
import { TrustView } from "@/features/trust/TrustView";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Trust" };

// The list is built from the server's configuration, so it is read for every request, never at build time.
// Reading the session cookie in serverApi() already makes this page dynamic.
export default async function TrustPage() {
  const queryClient = makeQueryClient();
  await queryClient.prefetchQuery(subprocessorsQuery(await serverApi()));

  return (
    <div className="container page">
      <header className={styles.head}>
        <h1 className="t-page">Trust</h1>
        <p className="muted">
          The model providers and other services this server sends data to, read from its running configuration.
        </p>
      </header>
      <HydrationBoundary state={dehydrate(queryClient)}>
        <TrustView />
      </HydrationBoundary>
    </div>
  );
}
