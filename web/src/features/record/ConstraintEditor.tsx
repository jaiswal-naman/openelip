"use client";

import { useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import { problemOf } from "@/lib/api/problems";
import { useViewer } from "@/lib/viewer";
import { savePreference, saveConstraint, useRecordWrite } from "./api";
import { type ConstraintKind, ARRANGEMENTS, type Draft, EMPLOYMENT_TYPES, type Problem, restatement, SENIORITIES, toBody } from "./constraints";
import styles from "./constraints.module.css";
import { CheckGroup, CountryPicker, LinesField, SelectField } from "./formFields";
import { PROTECTED_NOTICE, writeProblemText } from "./labels";

export type Saved = "constraint" | "preference";

type Props = {
  kind: ConstraintKind;
  initial: Draft;
  /** intake: Confirm or Skip. record: Confirm or Cancel, and what an edit does tomorrow. suggestion: a dealbreaker that fits this kind. */
  mode: "intake" | "record" | "suggestion";
  /** What the person wrote, for a suggestion. */
  said?: string;
  onSaved: (outcome: Saved) => void;
  /** Skip, or Cancel. */
  onLeave: () => void;
};

const PERIODS = [
  { value: "year", label: "a year" },
  { value: "month", label: "a month" },
];

/**
 * One hard constraint, asked for in its own typed fields. It reads the value back in a plain sentence before anything is
 * saved, and Confirm saves exactly that sentence with the typed value (REC-03). Nothing here calls a model. A kind the
 * API cannot enforce is offered back as a preference, never silently saved as a rule (REC-04).
 */
export function ConstraintEditor({ kind, initial, mode, said, onSaved, onLeave }: Props) {
  const { locale } = useViewer();
  const [draft, setDraft] = useState(initial);
  const [problem, setProblem] = useState<Problem | null>(null);
  const save = useRecordWrite(saveConstraint);
  const asPreference = useRecordWrite(savePreference);
  const set = (change: Partial<Draft>) => setDraft((current) => ({ ...current, ...change }));
  const on = (field: Problem["field"]) => (problem?.field === field ? problem.message : null);

  const restated = restatement(kind, draft, locale);
  const refusal = save.isError ? problemOf(save.error) : null;
  const pending = save.isPending || asPreference.isPending;

  function submit() {
    const built = toBody(kind, draft, locale);
    if ("problem" in built) {
      setProblem(built.problem);
      return;
    }
    setProblem(null);
    save.mutate(built.body, { onSuccess: () => onSaved("constraint") });
  }

  return (
    <Form
      validationBehavior="aria"
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {said && <p className="muted">You wrote: &ldquo;{said}&rdquo;. This is what it means as a hard constraint.</p>}

      {(kind === "location" || kind === "work_authorization") && (
        <CountryPicker
          label={kind === "location" ? "Countries you want jobs in" : "Countries you are allowed to work in"}
          value={draft.countryCodes}
          onChange={(countryCodes) => set({ countryCodes })}
          error={on("country")}
        />
      )}
      {kind === "location" && (
        <LinesField label="Cities (optional)" description="One per line. Leave empty for the whole country." value={draft.cities} onChange={(cities) => set({ cities })} />
      )}
      {kind === "remote_policy" && (
        <CheckGroup legend="Ways of working you accept" options={ARRANGEMENTS} value={draft.arrangements} onChange={(arrangements) => set({ arrangements })} error={on("choice")} />
      )}
      {kind === "employment_type" && (
        <CheckGroup
          legend="Types of employment you accept"
          options={EMPLOYMENT_TYPES}
          value={draft.employmentTypes}
          onChange={(employmentTypes) => set({ employmentTypes })}
          error={on("choice")}
        />
      )}
      {kind === "seniority" && (
        <CheckGroup legend="Levels you accept" options={SENIORITIES} value={draft.seniorities} onChange={(seniorities) => set({ seniorities })} error={on("choice")} />
      )}
      {kind === "excluded_company" && (
        <LinesField
          label="Companies you never want to see"
          description="One company per line."
          value={draft.companies}
          onChange={(companies) => set({ companies })}
          error={on("companies")}
        />
      )}
      {kind === "salary_floor" && (
        <div className={styles.pair}>
          <TextField label="Amount" inputMode="numeric" value={draft.amount} onChange={(amount) => set({ amount })} error={on("amount")} />
          <TextField label="Currency" description="Three letters, for example INR" maxLength={3} value={draft.currency} onChange={(currency) => set({ currency })} error={on("currency")} />
          <SelectField label="Period" value={draft.period} options={PERIODS} onChange={(period) => set({ period: period === "month" ? "month" : "year" })} />
        </div>
      )}

      {restated && <p className={styles.restated}>{restated}</p>}
      {mode !== "intake" && <p className={`t-small ${styles.effect}`}>From tomorrow&rsquo;s run, jobs that do not meet this are filtered out. Past matches are not rewritten.</p>}

      {refusal?.kind === "constraint_not_enforceable" && (
        <div role="alert" className={styles.alert}>
          <p>This cannot be checked against job postings as a hard rule, so it cannot be guaranteed.</p>
          <Button
            isPending={asPreference.isPending}
            onPress={() =>
              asPreference.mutate(
                { kind: "untyped_dealbreaker", statement: refusal.preferenceStatement ?? restated ?? "" },
                { onSuccess: () => onSaved("preference") },
              )
            }
          >
            Save as a preference instead
          </Button>
        </div>
      )}
      {refusal?.kind === "protected_characteristic" && (
        <p role="alert" className={styles.alert}>
          {PROTECTED_NOTICE}
        </p>
      )}
      {refusal && refusal.kind !== "constraint_not_enforceable" && refusal.kind !== "protected_characteristic" && (
        <p role="alert" className={styles.alert}>
          {writeProblemText(save.error)}
        </p>
      )}
      {asPreference.isError && (
        <p role="alert" className={styles.alert}>
          {writeProblemText(asPreference.error)}
        </p>
      )}

      <div className={styles.buttons}>
        <Button type="submit" variant="primary" isPending={pending}>
          Confirm
        </Button>
        <Button isDisabled={pending} onPress={onLeave}>
          {mode === "intake" ? "Skip" : "Cancel"}
        </Button>
      </div>
    </Form>
  );
}
