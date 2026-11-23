import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { RolesList } from "@/features/hiring/RolesList";

export const metadata: Metadata = { title: "Roles" };

export default async function RolesPage() {
  await guardHiringPage("/hire/roles", { allowNoOrganization: true });
  return <RolesList />;
}
