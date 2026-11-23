import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { Team } from "@/features/hiring/Team";

export const metadata: Metadata = { title: "Team" };

export default async function TeamPage() {
  await guardHiringPage("/hire/team");
  return <Team />;
}
