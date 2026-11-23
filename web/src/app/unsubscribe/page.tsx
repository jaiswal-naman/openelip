import type { Metadata } from "next";
import { UnsubscribeView } from "@/features/brief/UnsubscribeView";

// The address carries a token that names a person: keep it out of the Referer of anything the page links to, and out of search.
export const metadata: Metadata = { title: "Stop the morning brief email", referrer: "no-referrer", robots: { index: false, follow: false } };

type Props = { searchParams: Promise<{ t?: string | string[] }> };

/**
 * Public: it is opened from an email, signed in or not (FRONTEND_UI 3.25). It reads the token from the address and does
 * nothing else on load, not even a request to the API, so a link scanner that fetches it unsubscribes no one. The button
 * on the page is what sends the token.
 */
export default async function UnsubscribePage({ searchParams }: Props) {
  const { t } = await searchParams;
  const token = (Array.isArray(t) ? t[0] : t) ?? null;
  return (
    <div className="container page">
      <UnsubscribeView token={token === "" ? null : token} />
    </div>
  );
}
