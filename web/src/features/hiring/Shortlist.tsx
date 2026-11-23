"use client";

import { ErrorState, EmptyState } from "@/components/ui/States";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { type Preview, useAnswerClock, useMembership, usePreview, useRole, useShortlist } from "./api";
import { AnswerClock } from "./AnswerClock";
import { ShortlistCard } from "./ShortlistCard";
import styles from "./hiring.module.css";

/** People asked so far, from the anonymous counts. Any "fewer than 5" makes the sum unknown, so it is not guessed. */
function askedCount(preview: Preview | undefined): number | null {
  if (!preview) return null;
  const parts = [preview.waiting_for_reply, preview.said_yes, preview.said_no_or_expired];
  return parts.every((part): part is number => typeof part === "number") ? parts.reduce((total, part) => total + part, 0) : null;
}

function askedPhrase(preview: Preview | undefined): string {
  const count = askedCount(preview);
  return count === null ? "Some people were asked" : `${count} people were asked`;
}

/** /hire/shortlist/[roleId]: only people who accepted, with what they agreed to share (HIR-05, invariant 4). */
export function Shortlist({ roleId }: { roleId: string }) {
  const role = useRole(roleId);
  if (role.isError) return <ErrorState what="Could not load this role." error={role.error} onRetry={() => void role.refetch()} />;
  if (role.data === undefined) return <p role="status" className="muted">Loading the shortlist.</p>;
  return <ShortlistOf roleId={roleId} orgId={role.data.organization_id} title={role.data.title} status={role.data.status} />;
}

function ShortlistOf({ roleId, orgId, title, status }: { roleId: string; orgId: string; title: string; status: "draft" | "open" | "closed" }) {
  const { membership } = useMembership();
  const list = useShortlist(roleId);
  const clock = useAnswerClock(orgId);
  const preview = usePreview({ id: roleId, status });
  const owedHere = (clock.data?.owed ?? []).filter((item) => item.role.id === roleId);
  const reload = () => {
    void list.refetch();
    void clock.refetch();
  };

  // Each cell is counted from what the page loaded; one that is not known yet is left out.
  const asked = askedCount(preview.data);
  const cells: TitleCell[] = [];
  if (list.data !== undefined) cells.push({ label: "Shortlisted", value: list.data.length });
  if (clock.data !== undefined) cells.push({ label: "Answers owed", value: owedHere.length, tone: owedHere.length > 0 ? "redline" : "muted" });
  if (asked !== null) cells.push({ label: "Asked", value: asked, tone: "muted" });

  return (
    <div className={styles.page}>
      <TitleBlock
        title={`Shortlist, ${title}`}
        lines={
          <>
            {membership && <p className={styles.company}>{membership.name}</p>}
            <p className="t-small">Only people who said yes are here, with what each agreed to share.</p>
            {owedHere.length > 0 && (
              <p className={styles.owe}>
                <span>You owe {owedHere.length} {owedHere.length === 1 ? "answer" : "answers"}</span>
                {owedHere.map((item) => (
                  <AnswerClock key={item.introduction_id} dueAt={item.answer_due_at} businessDaysLeft={item.business_days_left} isOverdue={item.is_overdue} timeZone={clock.data?.timezone} />
                ))}
              </p>
            )}
          </>
        }
        cells={cells}
      />
      {list.isError ? (
        <ErrorState what="Could not load the shortlist." error={list.error} onRetry={() => void list.refetch()} />
      ) : list.data === undefined ? (
        <p role="status" className="muted">Loading the shortlist.</p>
      ) : list.data.length === 0 ? (
        <EmptyState>Nobody has accepted yet. {askedPhrase(preview.data)}; each has 7 days to reply.</EmptyState>
      ) : (
        <ul className={styles.candidates}>
          {list.data.map((item) => (
            <li key={item.introduction_id}>
              <ShortlistCard
                item={item}
                owed={owedHere.find((entry) => entry.introduction_id === item.introduction_id)}
                roleId={roleId}
                orgId={orgId}
                timeZone={clock.data?.timezone}
                onStale={reload}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
