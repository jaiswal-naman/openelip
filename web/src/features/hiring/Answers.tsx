"use client";

import Link from "next/link";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { type OwedAnswer, useAnswer, useAnswerClock, useMembership } from "./api";
import { AnswerClock } from "./AnswerClock";
import { AnswerDialog } from "./AnswerDialog";
import styles from "./hiring.module.css";

/** /hire/answers: everything the organization owes, soonest due first, overdue first of all (HIR-06, FRONTEND_UI 3.16). */
export function Answers() {
  const { membership, isLoading } = useMembership();
  if (isLoading) return <p role="status" className="muted">Loading.</p>;
  if (membership === null) return <EmptyState>Create your organization on the Roles page first.</EmptyState>;
  return <AnswersOf orgId={membership.id} />;
}

function AnswersOf({ orgId }: { orgId: string }) {
  const clock = useAnswerClock(orgId);
  const answer = useAnswer(null, orgId);
  if (clock.isError) return <ErrorState what="Could not load your answers." error={clock.error} onRetry={() => void clock.refetch()} />;
  if (clock.data === undefined) return <p role="status" className="muted">Loading your answers.</p>;

  const { owed, timezone, answered_last_30_days: record } = clock.data;
  const sorted = [...owed].sort((a, b) => Number(b.is_overdue) - Number(a.is_overdue) || a.answer_due_at.localeCompare(b.answer_due_at));
  const when = (iso: string | null) => (iso === null ? "" : new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: timezone }));
  const due = (iso: string) => new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: timezone });
  const send = (item: OwedAnswer, verdict: "yes" | "no") => (reason: string) => answer.mutateAsync({ introductionId: item.introduction_id, answer: verdict, reason });
  const actions = (item: OwedAnswer) => (
    <span className={styles.row}>
      <AnswerDialog answer="yes" candidate={item.name ?? "Candidate"} onSubmit={send(item, "yes")} onStale={() => void clock.refetch()} />
      <AnswerDialog answer="no" candidate={item.name ?? "Candidate"} onSubmit={send(item, "no")} onStale={() => void clock.refetch()} />
    </span>
  );
  const pill = (item: OwedAnswer) => (
    <AnswerClock dueAt={item.answer_due_at} businessDaysLeft={item.business_days_left} isOverdue={item.is_overdue} timeZone={timezone} />
  );

  return (
    <div className={styles.page}>
      <h1 className="t-page">{sorted.length === 0 ? "You owe no answers." : `You owe ${sorted.length} ${sorted.length === 1 ? "answer" : "answers"}`}</h1>
      {sorted.length === 0 ? (
        <EmptyState>Every introduction that was accepted has an answer.</EmptyState>
      ) : (
        <>
          <div className={`${styles.tableWrap} ${styles.swap}`}>
            <Table aria-label="Answers owed" className={styles.table}>
              <TableHeader>
                {["Candidate", "Role", "Accepted", "Due", "Clock", "Answer"].map((name, index) => (
                  <Column key={name} isRowHeader={index === 0} className={`t-label ${styles.column}`}>
                    {name}
                  </Column>
                ))}
              </TableHeader>
              <TableBody>
                {sorted.map((item) => (
                  <Row key={item.introduction_id} id={item.introduction_id} textValue={item.name ?? "Candidate"}>
                    <Cell className={styles.cell}>{item.name ?? "Candidate"}</Cell>
                    <Cell className={styles.cell}>
                      <Link href={`/hire/shortlist/${item.role.id}`}>{item.role.title}</Link>
                    </Cell>
                    <Cell className={styles.cell}>{when(item.responded_at)}</Cell>
                    <Cell className={styles.cell}>{due(item.answer_due_at)}</Cell>
                    <Cell className={styles.cell}>{pill(item)}</Cell>
                    <Cell className={styles.cell}>{actions(item)}</Cell>
                  </Row>
                ))}
              </TableBody>
            </Table>
          </div>
          <ul className={styles.cards} aria-label="Answers owed, as cards">
            {sorted.map((item) => (
              <li key={item.introduction_id} className={styles.card}>
                <span className={styles.row}>
                  <strong>{item.name ?? "Candidate"}</strong>
                  {pill(item)}
                </span>
                <Link href={`/hire/shortlist/${item.role.id}`}>{item.role.title}</Link>
                <span className={styles.muted}>Due {due(item.answer_due_at)}</span>
                {actions(item)}
              </li>
            ))}
          </ul>
        </>
      )}
      <p className={styles.muted}>Answered in the last 30 days: {record.total}</p>
    </div>
  );
}
