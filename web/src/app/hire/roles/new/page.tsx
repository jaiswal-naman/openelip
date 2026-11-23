import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { NewRole } from "@/features/hiring/RoleIntake";

export const metadata: Metadata = { title: "New role" };

export default async function NewRolePage() {
  await guardHiringPage("/hire/roles/new");
  return <NewRole />;
}
