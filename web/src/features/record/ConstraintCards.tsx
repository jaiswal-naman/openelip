"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { ErrorState } from "@/components/ui/States";
import { useApi } from "@/lib/api/context";
import { recordQuery, removeConstraint, type RecordConstraint, useRecordWrite } from "./api";
import { CONSTRAINT_ORDER, type ConstraintKind, draftFromTyped, emptyDraft } from "./constraints";
import styles from "./constraints.module.css";
import { ConstraintEditor } from "./ConstraintEditor";
import { constraintKindLabel, NOT_GUARANTEED_NOTICE, writeProblemText } from "./labels";

type Mode = "intake" | "record";
type Left = "skipped" | "preference"; // how a card ended without becoming a hard constraint

/**
 * The seven hard constraints, one card each, in the order the intake asks (REC-03). In the intake (onboarding step 4)
 * the first card without an answer is open and the next opens after Confirm or Skip; on the record page every card is
 * closed until Edit or Set. A card without a value reads "Not set (not enforced)", and says so rather than meaning "any".
 */
export function ConstraintCards({ mode }: { mode: Mode }) {
  const api = useApi();
  const record = useQuery(recordQuery(api));
  const remove = useRecordWrite(removeConstraint);
  // undefined lets the intake open the first card with no answer; a kind or null is the person's own choice
  const [chosen, setChosen] = useState<ConstraintKind | null | undefined>(undefined);
  const [left, setLeft] = useState<Partial<Record<ConstraintKind, Left>>>({});

  if (record.isError) return <ErrorState what="Could not load your constraints." error={record.error} onRetry={() => void record.refetch()} />;
  if (!record.data) {
    return (
      <p role="status" className="muted">
        Loading your constraints.
      </p>
    );
  }

  const saved = new Map<string, RecordConstraint>(record.data.constraints.map((constraint) => [constraint.kind, constraint]));
  const unanswered = CONSTRAINT_ORDER.find((kind) => !saved.has(kind) && !left[kind]);
  const open = chosen !== undefined ? chosen : mode === "intake" ? (unanswered ?? null) : null;
  const next = () => setChosen(mode === "intake" ? undefined : null);

  return (
    <div className={styles.wrap}>
      <ol className={styles.cards}>
        {CONSTRAINT_ORDER.map((kind) => {
          const constraint = saved.get(kind);
          const label = constraintKindLabel(kind).toLowerCase();
          return (
            <li key={kind} className={styles.card} data-open={open === kind || undefined}>
              <div className={styles.head}>
                <div className={styles.summary}>
                  <h3 className="t-card">{constraintKindLabel(kind)}</h3>
                  {constraint ? (
                    <p className={styles.value}>{constraint.statement}</p>
                  ) : left[kind] === "preference" ? (
                    <p className={styles.value}>Saved as a preference instead. {NOT_GUARANTEED_NOTICE}</p>
                  ) : (
                    <p className={styles.unset}>Not set (not enforced)</p>
                  )}
                </div>
                {open !== kind && (
                  <div className={styles.buttons}>
                    {constraint && <Pill tone="ok">Confirmed</Pill>}
                    <Button size="sm" aria-label={`${constraint ? "Edit" : "Set"} ${label}`} onPress={() => setChosen(kind)}>
                      {constraint ? "Edit" : "Set"}
                    </Button>
                    {constraint && mode === "record" && (
                      <Button size="sm" isDisabled={remove.isPending} aria-label={`Remove ${label}`} onPress={() => remove.mutate(constraint.id)}>
                        Remove
                      </Button>
                    )}
                  </div>
                )}
              </div>
              {open === kind && (
                <ConstraintEditor
                  key={kind}
                  kind={kind}
                  mode={mode}
                  initial={constraint ? draftFromTyped(constraint) : emptyDraft()}
                  onSaved={(outcome) => {
                    if (outcome === "preference") setLeft((current) => ({ ...current, [kind]: "preference" }));
                    next();
                  }}
                  onLeave={() => {
                    if (mode === "intake") setLeft((current) => ({ ...current, [kind]: "skipped" }));
                    next();
                  }}
                />
              )}
            </li>
          );
        })}
      </ol>
      {remove.isError && (
        <p role="alert" className={styles.alert}>
          {writeProblemText(remove.error)}
        </p>
      )}
    </div>
  );
}
