"use client";

import { Cell, Column, Disclosure, DisclosurePanel, Heading, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { VerdictPill } from "@/features/matches/VerdictPill";
import { EMPLOYER_ANSWER_LABELS, IMPORTANCE_LABELS } from "@/lib/labels";
import { type OwedAnswer, type ShortlistItem, useAnswer } from "./api";
import { AnswerClock } from "./AnswerClock";
import { AnswerDialog } from "./AnswerDialog";
import { SignalButton } from "./SignalButton";
import styles from "./hiring.module.css";

const day = (iso: string | null) => (iso === null ? "" : new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }));

/** One person who said yes: exactly what they agreed to share, the answer controls, the clock and the signal. */
export function ShortlistCard({
  item,
  owed,
  roleId,
  orgId,
  timeZone,
  onStale,
}: {
  item: ShortlistItem;
  owed: OwedAnswer | undefined;
  roleId: string;
  orgId: string;
  timeZone: string | undefined;
  onStale: () => void;
}) {
  const answer = useAnswer(roleId, orgId);
  const shared = item.shared;
  const name = shared?.name ?? "Candidate";
  const required = (shared?.evidence ?? []).filter((row) => row.importance === "required");
  const met = required.filter((row) => row.verdict === "met").length;
  const roles = (shared?.facts ?? []).filter((fact) => fact.kind === "role");
  const education = (shared?.facts ?? []).filter((fact) => fact.kind === "education");
  const send = (verdict: "yes" | "no") => (reason: string) => answer.mutateAsync({ introductionId: item.introduction_id, answer: verdict, reason });

  return (
    <article className={styles.card} aria-label={`Candidate ${name}`}>
      <div className={styles.head}>
        <div>
          <h2 className={`t-card ${styles.words}`}>{name}</h2>
          {shared?.headline && <p className={styles.words}>{shared.headline}</p>}
        </div>
        <span className={styles.row}>
          <span className={styles.muted}>accepted {day(item.responded_at)}</span>
          {item.employer_answer === null && item.answer_due_at && (
            <AnswerClock dueAt={item.answer_due_at} businessDaysLeft={owed?.business_days_left ?? 99} isOverdue={item.is_overdue} timeZone={timeZone} />
          )}
        </span>
      </div>
      {item.signals.from_candidate && <span className={styles.badge}>Serious interest from candidate</span>}
      {required.length > 0 && (
        <p>
          Meets {met} of {required.length} required.
        </p>
      )}

      <Disclosure>
        <Heading level={3}>
          <Button slot="trigger" size="sm">
            Shared details
          </Button>
        </Heading>
        <DisclosurePanel>
          <div className={styles.shared}>
            <div className={styles.tableWrap}>
              <Table aria-label={`Shared evidence of ${name}`} className={styles.table}>
                <TableHeader>
                  {["Requirement", "Verdict", "Evidence (shared)"].map((column, index) => (
                    <Column key={column} isRowHeader={index === 0} className={`t-label ${styles.column}`}>
                      {column}
                    </Column>
                  ))}
                </TableHeader>
                <TableBody>
                  {(shared?.evidence ?? []).map((row, index) => (
                    <Row key={index} id={String(index)} textValue={row.requirement}>
                      <Cell className={styles.cell}>
                        {row.requirement} <span className={styles.muted}>({IMPORTANCE_LABELS[row.importance]})</span>
                      </Cell>
                      <Cell className={styles.cell}>
                        <VerdictPill verdict={row.verdict} />
                      </Cell>
                      <Cell className={styles.cell}>{row.record_quotes.length === 0 ? "No quote shared" : row.record_quotes.map((quote) => `"${quote}"`).join(" ")}</Cell>
                    </Row>
                  ))}
                </TableBody>
              </Table>
            </div>
            {roles.length > 0 && (
              <div>
                <h4 className="t-label">Roles</h4>
                <ul className={styles.facts}>
                  {roles.map((fact, index) => (
                    <li key={index}>{fact.statement}</li>
                  ))}
                </ul>
              </div>
            )}
            {education.length > 0 && (
              <div>
                <h4 className="t-label">Education</h4>
                <ul className={styles.facts}>
                  {education.map((fact, index) => (
                    <li key={index}>{fact.statement}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </DisclosurePanel>
      </Disclosure>

      {item.employer_answer === null ? (
        <div className={styles.row}>
          <AnswerDialog answer="yes" candidate={name} onSubmit={send("yes")} onStale={onStale} />
          <AnswerDialog answer="no" candidate={name} onSubmit={send("no")} onStale={onStale} />
        </div>
      ) : (
        <div className={styles.shell}>
          <p>
            <strong>{EMPLOYER_ANSWER_LABELS[item.employer_answer]}.</strong> <span className={styles.words}>{item.employer_answer_reason}</span>
          </p>
          {item.employer_answer === "yes" && item.email && <p>Email: {item.email}</p>}
        </div>
      )}
      <SignalButton introductionId={item.introduction_id} roleId={roleId} orgId={orgId} alreadySent={item.signals.from_you} />
    </article>
  );
}
