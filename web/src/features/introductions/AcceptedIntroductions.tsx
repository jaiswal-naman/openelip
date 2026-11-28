"use client";

import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import ledger from "@/components/ui/ledger.module.css";
import { describeFailure } from "@/lib/api/problems";
import { CANDIDATE_INTRODUCTION_LABELS as WORDS } from "@/lib/labels";
import { type IntroductionItem, useCandidateSignals, useIntroduction, useSendInterest } from "./api";
import styles from "./introductions.module.css";

const day = (iso: string | null) => (iso === null ? "" : new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }));

/**
 * The introductions the person accepted, as a ledger: the employer and role, when, the employer's answer and reason once
 * given, and any serious-interest signal (HIR-05, HIR-08). Until a campaign page exists (Phase 7) this is where a candidate
 * follows them.
 */
export function AcceptedIntroductions({ items }: { items: IntroductionItem[] }) {
  const accepted = items.filter((item) => item.status === "accepted");
  if (accepted.length === 0) return null;
  return (
    <section aria-labelledby="accepted-introductions" className={styles.section}>
      <h2 id="accepted-introductions" className="t-section">
        {WORDS.acceptedHeading}
      </h2>
      <table className={ledger.table} aria-labelledby="accepted-introductions">
        <thead>
          <tr>
            <th scope="col">Employer and role</th>
            <th scope="col">Status</th>
            <th scope="col">Their answer</th>
            <th scope="col">Serious interest</th>
          </tr>
        </thead>
        <tbody>
          {accepted.map((item) => (
            <AcceptedRow key={item.id} item={item} />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function AcceptedRow({ item }: { item: IntroductionItem }) {
  const detail = useIntroduction(item.id);
  const signals = useCandidateSignals();
  const send = useSendInterest();
  const answer = detail.data?.employer_answer ?? item.employer_answer;
  const reason = detail.data?.employer_answer_reason ?? null;
  const left = signals.data ? Math.max(0, signals.data.quota.limit - signals.data.quota.used) : null;
  const sent = (signals.data?.sent ?? []).some((signal) => signal.introduction_id === item.id);
  return (
    <tr>
      <th scope="row">
        <span className={styles.employer}>
          <span className={`t-card ${styles.words}`}>
            {item.organization_name ?? "An employer"}: {item.role_title ?? "a role"}
          </span>
          {detail.data?.organization.is_synthetic && <Pill tone="unk">{WORDS.demo}</Pill>}
        </span>
      </th>
      <td data-label="Status">
        <span className={`t-small muted ${styles.nowrap}`}>Accepted {day(item.responded_at)}</span>
      </td>
      <td data-label="Their answer">
        {answer === null ? (
          <p>{WORDS.waiting}</p>
        ) : (
          <p>
            <strong>{answer === "yes" ? "They answered yes" : "They answered no"}.</strong>
            {reason !== null && <span className={styles.words}> Their reason: {reason}</span>}
          </p>
        )}
      </td>
      <td data-label="Serious interest">
        <div className={styles.signals}>
          {detail.data?.has_employer_signal && <Pill tone="stage">{WORDS.employerSignal}</Pill>}
          {sent ? (
            <p className="t-small muted">{WORDS.youSentSignal}</p>
          ) : (
            left !== null &&
            left > 0 && (
              <Button size="sm" isPending={send.isPending} onPress={() => send.mutate(item.id)}>
                {`Send serious interest (${left} of ${signals.data?.quota.limit} left this week)`}
              </Button>
            )
          )}
          {send.isError && (
            <p role="alert" className={styles.problem}>
              {describeFailure(send.error)}
            </p>
          )}
        </div>
      </td>
    </tr>
  );
}
