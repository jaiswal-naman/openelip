"use client";

import { type ReactNode, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { EmptyState } from "@/components/ui/States";
import { type DocumentLine, type FactDecision, type RecordFact, reviewFacts, useRecordWrite } from "./api";
import { factKindLabel, sourceLabel, writeProblemText } from "./labels";
import { lineLabel } from "./lines";
import styles from "./record.module.css";
import { StatementEditor } from "./StatementEditor";

type Props = {
  /** Facts of any status. Only unconfirmed ones can be decided; confirmed ones are listed; rejected and replaced ones are not active, so not shown. */
  facts: RecordFact[];
  /** The resume the facts were read from, to say which line each one came from ("L4"). */
  document?: { id: string; lines: DocumentLine[] };
};

// attributes.unverified is set by the parse for an item whose names or dates the code could not find in the cited lines
const isUnverified = (fact: RecordFact) => fact.attributes.unverified === true;

/**
 * Check what was read from the resume (REC-01, REC-02, REC-06 in FRONTEND_UI 3.2 step 3). Each fact shows the resume's own
 * words it came from; the candidate confirms, fixes or rejects it, and only confirmed facts are used anywhere. A fix is a
 * new fact in the candidate's words with the old one marked replaced. Every decision is one write, and so is "Confirm all
 * remaining", which leaves out what the code could not confirm: those are decided one by one.
 */
export function FactReview({ facts, document }: Props) {
  const review = useRecordWrite(reviewFacts);
  const [fixing, setFixing] = useState<string | null>(null);

  const toReview = facts.filter((fact) => fact.status === "unconfirmed");
  const unverified = toReview.filter(isUnverified);
  const readable = toReview.filter((fact) => !isUnverified(fact));
  const confirmed = facts.filter((fact) => fact.status === "confirmed");

  const decide = (decisions: FactDecision[]) => review.mutate(decisions, { onSuccess: () => setFixing(null) });
  const rowProps = (fact: RecordFact) => ({
    fact,
    document,
    busy: review.isPending,
    fixing: fixing === fact.id,
    onFix: () => setFixing(fact.id),
    onCancel: () => setFixing(null),
    onDecide: decide,
  });

  if (toReview.length === 0 && confirmed.length === 0) {
    return <EmptyState>No facts were read from your resume. Add them by hand on your record page.</EmptyState>;
  }

  return (
    <section className={styles.review} aria-label="Facts read from your resume">
      {readable.length > 0 && (
        <FactGroup title="To review">
          {readable.map((fact) => (
            <FactRow key={fact.id} {...rowProps(fact)} />
          ))}
        </FactGroup>
      )}
      {unverified.length > 0 && (
        <FactGroup title="We could not confirm these" note="We could not find every name or date in these lines of your resume. Check each one, then confirm, fix or reject it.">
          {unverified.map((fact) => (
            <FactRow key={fact.id} {...rowProps(fact)} />
          ))}
        </FactGroup>
      )}
      {confirmed.length > 0 && (
        <FactGroup title="Confirmed">
          {confirmed.map((fact) => (
            <FactRow key={fact.id} {...rowProps(fact)} />
          ))}
        </FactGroup>
      )}

      {review.isError && (
        <p role="alert" className={styles.problem}>
          {writeProblemText(review.error)}
        </p>
      )}
      <p role="status" className="sr-only">
        {review.isSuccess ? `Saved. ${review.data.unconfirmed_remaining} to review.` : ""}
      </p>

      <div className={styles.footer}>
        <p>{toReview.length === 0 ? "Nothing left to review." : `${toReview.length} to review. They are ignored until you confirm them.`}</p>
        {readable.length > 0 && (
          <div className={styles.footerAction}>
            <Button isPending={review.isPending} onPress={() => decide(readable.map((fact) => ({ fact_id: fact.id, action: "confirm" })))}>
              Confirm all remaining
            </Button>
            <p className="t-small muted">Each one can still be changed on your record page.</p>
          </div>
        )}
      </div>
    </section>
  );
}

function FactGroup({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <div className={styles.group}>
      <h3 className="t-section">{title}</h3>
      {note && <p className={`muted ${styles.note}`}>{note}</p>}
      <div className={styles.ledgerHead} aria-hidden="true">
        <span>Fact</span>
        <span>Where it came from</span>
        <span />
      </div>
      <ul className={styles.facts}>{children}</ul>
    </div>
  );
}

type RowProps = {
  fact: RecordFact;
  document?: Props["document"];
  busy: boolean;
  fixing: boolean;
  onFix: () => void;
  onCancel: () => void;
  onDecide: (decisions: FactDecision[]) => void;
};

function FactRow({ fact, document, busy, fixing, onFix, onCancel, onDecide }: RowProps) {
  const waiting = fact.status === "unconfirmed";
  const lines = document && fact.provenance?.document_id === document.id ? document.lines : undefined;
  const line = fact.provenance ? lineLabel(lines, fact.provenance.span) : null;
  const decision = (action: FactDecision["action"], statement?: string): FactDecision => ({ fact_id: fact.id, action, statement });

  return (
    <li className={styles.fact} data-editing={fixing || undefined} data-waiting={waiting || undefined}>
      <div className={styles.what}>
        <p className={`t-label ${styles.labels}`}>
          <span>{factKindLabel(fact.kind)}</span>
          {waiting && <span className={styles.needs}>Needs you</span>}
        </p>
        {fixing ? (
          <StatementEditor
            label="Your wording"
            initial={fact.statement}
            pending={busy}
            onCancel={onCancel}
            // the same words need no new fact: that is a confirmation
            onSave={(next) => onDecide([next === fact.statement ? decision("confirm") : decision("correct", next)])}
          />
        ) : (
          <p className={styles.statement}>{fact.statement}</p>
        )}
      </div>

      <figure className={styles.source}>
        <figcaption className="t-label">
          {fact.provenance ? `${sourceLabel("resume")}${line ? ` · ${line}` : ""}` : sourceLabel(fact.source)}
        </figcaption>
        {fact.provenance && <blockquote className={styles.quote}>{fact.provenance.quote}</blockquote>}
      </figure>

      {waiting && !fixing && (
        <div className={styles.actions}>
          <Button variant="primary" size="sm" isDisabled={busy} aria-label={`Confirm: ${fact.statement}`} onPress={() => onDecide([decision("confirm")])}>
            Confirm
          </Button>
          <Button size="sm" isDisabled={busy} aria-label={`Fix: ${fact.statement}`} onPress={onFix}>
            Fix
          </Button>
          <Button size="sm" isDisabled={busy} aria-label={`Reject: ${fact.statement}`} onPress={() => onDecide([decision("reject")])}>
            Reject
          </Button>
        </div>
      )}
      {fact.status === "confirmed" && (
        <div className={styles.actions}>
          <Pill tone="ok">Confirmed</Pill>
        </div>
      )}
    </li>
  );
}
