"use client";

import Link from "next/link";
import { buttonClass } from "@/components/ui/buttonStyles";
import { useAnswerClock } from "./api";
import styles from "./hiring.module.css";

/** "Paused: answers overdue" on every hiring-desk page while any answer is overdue (FRONTEND_UI 4.7, HIR-06). */
export function PauseBadge({ orgId }: { orgId: string }) {
  const clock = useAnswerClock(orgId);
  if (!clock.data?.is_asking_paused) return null;
  const overdue = clock.data.owed.filter((item) => item.is_overdue).length;
  return (
    <div role="status" className={styles.banner}>
      <span className={styles.bannerLabel}>Paused: answers overdue</span>
      <span>
        New introductions are paused for the whole organization until you answer {overdue} overdue.
      </span>
      <Link href="/hire/answers" className={buttonClass("secondary", "sm")}>
        Answer now
      </Link>
    </div>
  );
}
