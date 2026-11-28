"use client";

import Link from "next/link";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { TitleBlock } from "@/components/ui/TitleBlock";
import ledger from "@/components/ui/ledger.module.css";
import { type OwedAnswer, useAnswer, useAnswerClock, useMembership } from "./api";
import { AnswerClock } from "./AnswerClock";
import { AnswerDialog } from "./AnswerDialog";
import styles from "./hiring.module.css";

/** /hire/answers: everything the organization owes, soonest due first, overdue first of all (HIR-06, FRONTEND_UI 3.16). */
export function Answers() {
  const { membership, isLoading } = useMembership();
  if (isLoading) return <p role="status" className="muted">Loading.</p>;
  if (membership === null) return <EmptyState>Create your organization on the Roles page first.</EmptyState>;
  return <AnswersOf orgId={membership.id} orgName={membership.name} />;
}

function AnswersOf({ orgId, orgName }: { orgId: string; orgName: string }) {
  const clock = useAnswerClock(orgId);
  const answer = useAnswer(null, orgId);
  if (clock.isError) return <ErrorState what="Could not load your answers." error={clock.error} onRetry={() => void clock.refetch()} />;
  if (clock.data === undefined) return <p role="status" className="muted">Loading your answers.</p>;

  const { owed, timezone, answered_last_30_days: record, is_asking_paused: paused } = clock.data;
  const sorted = [...owed].sort((a, b) => Number(b.is_overdue) - Number(a.is_overdue) || a.answer_due_at.localeCompare(b.answer_due_at));
  const overdue = sorted.filter((item) => item.is_overdue).length;
  const when = (iso: string | null) => (iso === null ? "" : new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: timezone }));
  const due = (iso: string) => new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: timezone });
  const send = (item: OwedAnswer, verdict: "yes" | "no") => (reason: string) => answer.mutateAsync({ introductionId: item.introduction_id, answer: verdict, reason });

  return (
    <div className={styles.page}>
      <TitleBlock
        title={sorted.length === 0 ? "You owe no answers." : `You owe ${sorted.length} ${sorted.length === 1 ? "answer" : "answers"}`}
        lines={
          <>
            <p className={styles.company}>{orgName}</p>
            <p className="t-small">Answered in the last 30 days: {record.total}</p>
          </>
        }
        cells={[
          { label: "Owed", value: sorted.length, tone: sorted.length > 0 ? "redline" : "muted" },
          { label: "Overdue", value: overdue, tone: overdue > 0 ? "redline" : "muted" },
          { label: "On time, 30 days", value: `${record.on_time} of ${record.total}`, tone: "muted" },
          { label: "New asks", value: paused ? "Paused" : "Not paused", tone: paused ? "redline" : "ok" },
        ]}
      />
      {sorted.length === 0 ? (
        <EmptyState>Every introduction that was accepted has an answer.</EmptyState>
      ) : (
        <Table aria-label="Answers owed" className={ledger.table}>
          <TableHeader>
            <Column isRowHeader>Candidate</Column>
            <Column>Role</Column>
            <Column>Accepted</Column>
            <Column>Due</Column>
            <Column>Clock</Column>
            <Column>Answer</Column>
          </TableHeader>
          <TableBody>
            {sorted.map((item) => (
              <Row key={item.introduction_id} id={item.introduction_id} textValue={item.name ?? "Candidate"}>
                <Cell className={styles.words}>{item.name ?? "Candidate"}</Cell>
                <Cell data-label="Role">
                  <Link href={`/hire/shortlist/${item.role.id}`}>{item.role.title}</Link>
                </Cell>
                <Cell data-label="Accepted">
                  <span className="t-data">{when(item.responded_at)}</span>
                </Cell>
                <Cell data-label="Due">
                  <span className="t-data">{due(item.answer_due_at)}</span>
                </Cell>
                <Cell data-label="Clock">
                  <AnswerClock dueAt={item.answer_due_at} businessDaysLeft={item.business_days_left} isOverdue={item.is_overdue} timeZone={timezone} />
                </Cell>
                <Cell data-label="Answer">
                  <span className={styles.row}>
                    <AnswerDialog answer="yes" candidate={item.name ?? "Candidate"} onSubmit={send(item, "yes")} onStale={() => void clock.refetch()} />
                    <AnswerDialog answer="no" candidate={item.name ?? "Candidate"} onSubmit={send(item, "no")} onStale={() => void clock.refetch()} />
                  </span>
                </Cell>
              </Row>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
