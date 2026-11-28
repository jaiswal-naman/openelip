import type { Metadata } from "next";
import { safeNextPath } from "@/features/auth/safeNext";
import { LocalSession } from "@/features/local/LocalSession";

export const metadata: Metadata = { title: "Opening your workspace" };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

/** Where a visit with no session lands in local mode (the proxy and the sign-in page send it here). Nothing to see. */
export default async function LocalSessionPage({ searchParams }: { searchParams: SearchParams }) {
  const next = safeNextPath((await searchParams).next);
  return (
    <div className="container page">
      <LocalSession next={next} />
    </div>
  );
}
