import type { Metadata } from "next";
import { guardHiringPage } from "@/features/hiring/server";
import { Answers } from "@/features/hiring/Answers";

export const metadata: Metadata = { title: "Answers" };

export default async function AnswersPage() {
  await guardHiringPage("/hire/answers");
  return <Answers />;
}
