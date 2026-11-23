import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { RoleIntake } from "@/features/hiring/RoleIntake";

export const metadata: Metadata = { title: "Role" };

export default async function RolePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  await guardHiringPage(`/hire/roles/${id}`);
  return <RoleIntake id={id} />;
}
