import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { meQuery } from "@/features/auth/queries";
import { BriefSettings } from "@/features/brief/BriefSettings";
import styles from "@/features/privacy/privacy.module.css";
import { PrivacySection } from "@/features/privacy/PrivacySection";
import { subprocessorsQuery } from "@/features/trust/queries";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Settings" };

// FRONTEND_UI 3.10 orders the sections; later phases add theirs to this list.
const SECTIONS = [
  { id: "brief", label: "Brief" },
  { id: "privacy", label: "Privacy" },
] as const;
type Section = (typeof SECTIONS)[number]["id"];

type Props = { searchParams: Promise<{ section?: string | string[] }> };

/**
 * Sections are URL state (?section=). Privacy is the default, and anything that is not a section shows it too. No session
 * goes to sign in and comes back to the section that was asked for.
 */
export default async function SettingsPage({ searchParams }: Props) {
  const asked = (await searchParams).section;
  const section: Section = (Array.isArray(asked) ? asked[0] : asked) === "brief" ? "brief" : "privacy";
  const queryClient = makeQueryClient();
  const api = await serverApi();
  const me = await queryClient.fetchQuery(meQuery(api)).catch(() => undefined);
  if (me === null) redirect(`/sign-in?next=${encodeURIComponent(`/app/settings?section=${section}`)}`);
  if (section === "privacy") await queryClient.prefetchQuery(subprocessorsQuery(api));

  return (
    <div className="container page">
      <header>
        <h1 className="t-page">Settings</h1>
      </header>
      <div className={styles.layout}>
        <nav aria-label="Settings sections" className={styles.nav}>
          <ul>
            {SECTIONS.map((item) => (
              <li key={item.id}>
                <Link href={`/app/settings?section=${item.id}`} aria-current={item.id === section ? "page" : undefined}>
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <HydrationBoundary state={dehydrate(queryClient)}>{section === "brief" ? <BriefSettings /> : <PrivacySection />}</HydrationBoundary>
      </div>
    </div>
  );
}
