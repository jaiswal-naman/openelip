import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { Shortlist } from "@/features/hiring/Shortlist";

export const metadata: Metadata = { title: "Shortlist" };

export default async function ShortlistPage({ params }: { params: Promise<{ roleId: string }> }) {
  const { roleId } = await params;
  await guardHiringPage(`/hire/shortlist/${roleId}`);
  return <Shortlist roleId={roleId} />;
}
