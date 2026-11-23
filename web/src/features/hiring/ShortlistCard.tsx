"use client";

import { Cell, Column, Disclosure, DisclosurePanel, Heading, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import ledger from "@/components/ui/ledger.module.css";
import { VerdictPill } from "@/features/matches/VerdictPill";
import { EMPLOYER_ANSWER_LABELS, IMPORTANCE_LABELS } from "@/lib/labels";
import { type OwedAnswer, type ShortlistItem, useAnswer } from "./api";
import { AnswerClock } from "./AnswerClock";
import { AnswerDialog } from "./AnswerDialog";
import { SignalButton } from "./SignalButton";
import styles from "./hiring.module.css";

const day = (iso: string | null) => (iso === null ? "" : new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }));

/**
 * One person who said yes, as a ruled row: what they agreed to share, the verdict on each requirement, the clock, and at the
 * right edge the answer and the signal in one size. No email shows before a yes.
 */
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
  const evidence = shared?.evidence ?? [];
  const required = evidence.filter((row) => row.importance === "required");
  const met = required.filter((row) => row.verdict === "met").length;
  const roles = (shared?.facts ?? []).filter((fact) => fact.kind === "role");
  const education = (shared?.facts ?? []).filter((fact) => fact.kind === "education");
  const send = (verdict: "yes" | "no") => (reason: string) => answer.mutateAsync({ introductionId: item.introduction_id, answer: verdict, reason });

  return (
    <article className={styles.candidate} aria-label={`Candidate ${name}`}>
      <div className={styles.candidateMain}>
        <div className={styles.stack}>
          <h2 className={`t-card ${styles.candidateName}`}>{name}</h2>
          {shared?.headline && <p className={styles.words}>{shared.headline}</p>}
        </div>
        <div className={styles.meta}>
          <span className={`t-small ${styles.muted}`}>accepted {day(item.responded_at)}</span>
          {item.employer_answer === null && item.answer_due_at && (
            <>
              <span className={`t-label ${styles.muted}`}>Answer clock</span>
              <AnswerClock dueAt={item.answer_due_at} businessDaysLeft={owed?.business_days_left ?? 99} isOverdue={item.is_overdue} timeZone={timeZone} />
            </>
          )}
          {item.signals.from_candidate && <Pill tone="stage">Serious interest from candidate</Pill>}
        </div>
        {required.length > 0 && (
          <p>
            Meets {met} of {required.length} required.
          </p>
        )}
        {evidence.length > 0 && (
          <ul className={styles.verdicts} aria-label={`Verdicts for ${name}`}>
            {evidence.map((row, index) => (
              <li key={index}>
                <VerdictPill verdict={row.verdict} />
                <span className={styles.words}>
                  {row.requirement}
                  {row.importance === "preferred" && <span className={styles.muted}> ({IMPORTANCE_LABELS.preferred})</span>}
                </span>
              </li>
            ))}
          </ul>
        )}

        <Disclosure className={styles.disclosure}>
          <Heading level={3}>
            <Button slot="trigger" size="sm">
              Shared details
            </Button>
          </Heading>
          <DisclosurePanel>
            <div className={styles.shared}>
              <Table aria-label={`Shared evidence of ${name}`} className={ledger.table}>
                <TableHeader>
                  <Column isRowHeader>Requirement</Column>
                  <Column>Verdict</Column>
                  <Column>Evidence (shared)</Column>
                </TableHeader>
                <TableBody>
                  {evidence.map((row, index) => (
                    <Row key={index} id={String(index)} textValue={row.requirement}>
                      <Cell className={styles.words}>
                        {row.requirement} <span className={styles.muted}>({IMPORTANCE_LABELS[row.importance]})</span>
                      </Cell>
                      <Cell data-label="Verdict">
                        <VerdictPill verdict={row.verdict} />
                      </Cell>
                      <Cell className={styles.words} data-label="Evidence (shared)">
                        {row.record_quotes.length === 0 ? "No quote shared" : row.record_quotes.map((quote) => `"${quote}"`).join(" ")}
                      </Cell>
                    </Row>
                  ))}
                </TableBody>
              </Table>
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

        {item.employer_answer !== null && (
          <div className={styles.answered} data-answer={item.employer_answer}>
            <p>
              <strong>{EMPLOYER_ANSWER_LABELS[item.employer_answer]}.</strong> <span className={styles.words}>{item.employer_answer_reason}</span>
            </p>
            {item.employer_answer === "yes" && item.email && <p>Email: {item.email}</p>}
          </div>
        )}
      </div>

      <div className={styles.candidateSide}>
        {item.employer_answer === null && (
          <div className={styles.answerPair}>
            <AnswerDialog answer="yes" candidate={name} onSubmit={send("yes")} onStale={onStale} />
            <AnswerDialog answer="no" candidate={name} onSubmit={send("no")} onStale={onStale} />
          </div>
        )}
        <SignalButton introductionId={item.introduction_id} roleId={roleId} orgId={orgId} alreadySent={item.signals.from_you} />
      </div>
    </article>
  );
}
