"use client";

import { ErrorState, EmptyState } from "@/components/ui/States";
import { type Preview, useAnswerClock, usePreview, useRole, useShortlist } from "./api";
import { AnswerClock } from "./AnswerClock";
import { ShortlistCard } from "./ShortlistCard";
import styles from "./hiring.module.css";

/** People asked so far, from the anonymous counts. Any "fewer than 5" makes the sum unknown, so it is not guessed. */
function askedPhrase(preview: Preview | undefined): string {
  if (!preview) return "Some people were asked";
  const parts = [preview.waiting_for_reply, preview.said_yes, preview.said_no_or_expired];
  return parts.every((part): part is number => typeof part === "number")
    ? `${parts.reduce((total, part) => total + part, 0)} people were asked`
    : "Some people were asked";
}

/** /hire/shortlist/[roleId]: only people who accepted, with what they agreed to share (HIR-05, invariant 4). */
export function Shortlist({ roleId }: { roleId: string }) {
  const role = useRole(roleId);
  if (role.isError) return <ErrorState what="Could not load this role." error={role.error} onRetry={() => void role.refetch()} />;
  if (role.data === undefined) return <p role="status" className="muted">Loading the shortlist.</p>;
  return <ShortlistOf roleId={roleId} orgId={role.data.organization_id} title={role.data.title} status={role.data.status} />;
}

function ShortlistOf({ roleId, orgId, title, status }: { roleId: string; orgId: string; title: string; status: "draft" | "open" | "closed" }) {
  const list = useShortlist(roleId);
  const clock = useAnswerClock(orgId);
  const preview = usePreview({ id: roleId, status });
  const owedHere = (clock.data?.owed ?? []).filter((item) => item.role.id === roleId);
  const reload = () => {
    void list.refetch();
    void clock.refetch();
  };

  return (
    <div className={styles.page}>
      <div className={styles.head}>
        <h1 className="t-page">Shortlist, {title}</h1>
        {owedHere.length > 0 && (
          <span className={styles.row}>
            <span>You owe {owedHere.length} {owedHere.length === 1 ? "answer" : "answers"}</span>
            {owedHere.map((item) => (
              <AnswerClock key={item.introduction_id} dueAt={item.answer_due_at} businessDaysLeft={item.business_days_left} isOverdue={item.is_overdue} timeZone={clock.data?.timezone} />
            ))}
          </span>
        )}
      </div>
      {list.isError ? (
        <ErrorState what="Could not load the shortlist." error={list.error} onRetry={() => void list.refetch()} />
      ) : list.data === undefined ? (
        <p role="status" className="muted">Loading the shortlist.</p>
      ) : list.data.length === 0 ? (
        <EmptyState>Nobody has accepted yet. {askedPhrase(preview.data)}; each has 7 days to reply.</EmptyState>
      ) : (
        <div className={styles.list}>
          {list.data.map((item) => (
            <ShortlistCard
              key={item.introduction_id}
              item={item}
              owed={owedHere.find((entry) => entry.introduction_id === item.introduction_id)}
              roleId={roleId}
              orgId={orgId}
              timeZone={clock.data?.timezone}
              onStale={reload}
            />
          ))}
        </div>
      )}
    </div>
  );
}
