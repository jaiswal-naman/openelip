import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { meQuery } from "@/features/auth/queries";
import { AutonomySection } from "@/features/autonomy/AutonomySection";
import { BriefSettings } from "@/features/brief/BriefSettings";
import { IntroductionsSettings } from "@/features/introductions/IntroductionsSettings";
import { ExportCard } from "@/features/privacy/ExportCard";
import { AccountDeletion, AiProcessing } from "@/features/privacy/PrivacySection";
import { subprocessorsQuery } from "@/features/trust/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";
import styles from "./settings.module.css";
import { SettingsHeader } from "./SettingsHeader";

export const metadata: Metadata = { title: "Settings" };

// FRONTEND_UI 3.10 orders the sections; later phases add theirs to this list. `section` is the old address of a part, kept
// working: the settings are one page now, and the address scrolls to the part it names.
const SECTIONS = [
  { id: "ai", section: "privacy" },
  { id: "brief", section: "brief" },
  { id: "introductions", section: "introductions" },
  { id: "autonomy", section: "autonomy" },
  { id: "export", section: null },
  { id: "account", section: null },
] as const;

type Props = { searchParams: Promise<{ section?: string | string[] }> };

/** One numbered part of the page. The number is read by nobody: the heading inside says what the part is. */
function Item({ id, children }: { id: string; children: ReactNode }) {
  const number = String(SECTIONS.findIndex((item) => item.id === id) + 1).padStart(2, "0");
  return (
    <div id={id} className={styles.item}>
      <span className={styles.num} aria-hidden="true">
        {number}
      </span>
      {children}
    </div>
  );
}

/**
 * The settings page: every section on one page, numbered. No session goes to sign in and comes back; `?section=` still
 * names a part (privacy is the default for the sign-in return) and the page scrolls to it, except to the first, which is
 * already under the page header.
 */
export default async function SettingsPage({ searchParams }: Props) {
  const asked = (await searchParams).section;
  const wanted = Array.isArray(asked) ? asked[0] : asked;
  const part = SECTIONS.find((item) => item.section === wanted);
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/app/settings?section=${part?.section ?? "privacy"}`)}`);
  await queryClient.prefetchQuery(subprocessorsQuery(api));

  return (
    <div className="container page">
      <HydrationBoundary state={dehydrate(queryClient)}>
        <SettingsHeader jumpTo={part === undefined || part.id === "ai" ? null : part.id} />
        <div className={styles.sheet}>
          <Item id="ai">
            <AiProcessing />
          </Item>
          <Item id="brief">
            <BriefSettings />
          </Item>
          <Item id="introductions">
            <IntroductionsSettings />
          </Item>
          <Item id="autonomy">
            <AutonomySection />
          </Item>
          <Item id="export">
            <ExportCard />
          </Item>
          <Item id="account">
            <AccountDeletion />
          </Item>
        </div>
      </HydrationBoundary>
    </div>
  );
}
