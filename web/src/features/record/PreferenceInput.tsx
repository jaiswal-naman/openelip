"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/States";
import { TextField } from "@/components/ui/TextField";
import { useApi } from "@/lib/api/context";
import { problemOf } from "@/lib/api/problems";
import { changePreference, type NewConstraint, type RecordPreference, recordQuery, removePreference, savePreference, useRecordWrite } from "./api";
import { CONSTRAINT_ORDER, type ConstraintKind, draftFromTyped } from "./constraints";
import { ConstraintEditor } from "./ConstraintEditor";
import { NOT_GUARANTEED_NOTICE, PROTECTED_NOTICE, writeProblemText } from "./labels";
import styles from "./preferences.module.css";
import { StatementEditor } from "./StatementEditor";

type Strength = "like" | "avoid" | "dealbreaker";
const STRENGTHS: { value: Strength; label: string }[] = [
  { value: "like", label: "I would like this" },
  { value: "avoid", label: "I would rather avoid this" },
  { value: "dealbreaker", label: "This is a dealbreaker" },
];
const WEIGHT: Record<Exclude<Strength, "dealbreaker">, number> = { like: 2, avoid: -2 };

function weightLabel(preference: RecordPreference): string {
  if (preference.kind === "untyped_dealbreaker" || preference.weight <= -3) return "Dealbreaker";
  return preference.weight < 0 ? "Would rather avoid" : "Would like";
}

/** What a hard constraint of this kind would be, if the dealbreaker the person typed fits one. */
type Offer = { preferenceId: string; said: string; kind: ConstraintKind; suggestion: NewConstraint };
const isKind = (kind: string): kind is ConstraintKind => (CONSTRAINT_ORDER as readonly string[]).includes(kind);

/**
 * Soft preferences in the person's own words (REC-04): free text, how much it matters, and the saved ones as chips. A
 * dealbreaker no job field can check is saved as a preference and says so, never as a guarantee; if it fits one of the
 * hard constraints the person may make it one, through the same editor and Confirm as the intake. A protected
 * characteristic is refused by the API and not kept: the field is emptied and nothing is sent again (REC-07).
 */
export function PreferenceInput() {
  const api = useApi();
  const record = useQuery(recordQuery(api));
  const save = useRecordWrite(savePreference);
  const change = useRecordWrite(changePreference);
  const remove = useRecordWrite(removePreference);
  const [text, setText] = useState("");
  const [strength, setStrength] = useState<Strength>("like");
  const [emptyMessage, setEmptyMessage] = useState<string | null>(null);
  const [notKept, setNotKept] = useState(false);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [making, setMaking] = useState(false);
  const [made, setMade] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  function add() {
    const statement = text.trim();
    setNotKept(false);
    setOffer(null);
    setMade(false);
    if (statement === "") {
      setEmptyMessage("Write what you prefer, or what you will not accept.");
      return;
    }
    setEmptyMessage(null);
    const body = strength === "dealbreaker" ? { kind: "untyped_dealbreaker" as const, statement } : { kind: "other" as const, statement, weight: WEIGHT[strength] };
    save.mutate(body, {
      onSuccess: ({ preference }) => {
        setText("");
        const suggestion = preference.suggested_constraint;
        if (suggestion && isKind(suggestion.kind)) setOffer({ preferenceId: preference.id, said: statement, kind: suggestion.kind, suggestion });
      },
      onError: (error) => {
        // Not kept, and not asked again: the words are gone from the field and nothing else is sent (REC-07).
        if (problemOf(error).kind === "protected_characteristic") {
          setText("");
          setNotKept(true);
        }
      },
    });
  }

  if (record.isError) return <ErrorState what="Could not load your preferences." error={record.error} onRetry={() => void record.refetch()} />;
  const preferences = record.data?.preferences ?? [];
  const failure = save.isError && problemOf(save.error).kind !== "protected_characteristic" ? save.error : (change.error ?? remove.error);

  return (
    <div className={styles.wrap}>
      <Form
        validationBehavior="aria"
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
      >
        <TextField label="A preference, or something you will not accept" description="For example: small teams, or no heavy travel." value={text} onChange={setText} error={emptyMessage} />
        <fieldset className={styles.strengths}>
          <legend className={styles.legend}>How much does it matter?</legend>
          <div className={styles.options}>
            {STRENGTHS.map((option) => (
              <label key={option.value} className={styles.option}>
                <input type="radio" name="strength" value={option.value} checked={strength === option.value} onChange={() => setStrength(option.value)} />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <div>
          <Button type="submit" variant="primary" isPending={save.isPending}>
            Add
          </Button>
        </div>
      </Form>

      {notKept && (
        <p role="alert" className={styles.alert}>
          {PROTECTED_NOTICE}
        </p>
      )}
      {failure !== null && failure !== undefined && (
        <p role="alert" className={styles.alert}>
          {writeProblemText(failure)}
        </p>
      )}

      {offer && !made && (
        <div className={styles.offer}>
          <p>Saved. This one can be checked in job data, so it can be a hard constraint instead of only a preference.</p>
          {making ? (
            <ConstraintEditor
              kind={offer.kind}
              mode="suggestion"
              said={offer.said}
              initial={draftFromTyped(offer.suggestion)}
              onSaved={() => {
                setMaking(false);
                setMade(true);
              }}
              onLeave={() => setMaking(false)}
            />
          ) : (
            <div>
              <Button onPress={() => setMaking(true)}>Make this a hard constraint</Button>
            </div>
          )}
        </div>
      )}
      {made && <p role="status">Saved as a hard constraint. It is now enforced; the preference stays in your list until you remove it.</p>}

      {preferences.length > 0 && (
        <ul className={styles.list} aria-label="Your preferences">
          {preferences.map((preference) => (
            <li key={preference.id} className={styles.item}>
              {editing === preference.id ? (
                <StatementEditor
                  label="Your wording"
                  initial={preference.statement}
                  pending={change.isPending}
                  onCancel={() => setEditing(null)}
                  // a preference with no vocabulary keeps its words as its value, as the API does when it is made
                  onSave={(statement) =>
                    change.mutate(
                      { id: preference.id, body: preference.kind === "other" ? { statement, value: statement.toLowerCase().split(/\s+/).join(" ") } : { statement } },
                      { onSuccess: () => setEditing(null) },
                    )
                  }
                />
              ) : (
                <>
                  <div className={styles.what}>
                    <p className={styles.statement}>{preference.statement}</p>
                    <p className="t-label muted">{weightLabel(preference)}</p>
                    {preference.notice === "not_guaranteed" && <p className="t-small">{NOT_GUARANTEED_NOTICE}</p>}
                  </div>
                  <div className={styles.buttons}>
                    <Button size="sm" aria-label={`Edit: ${preference.statement}`} onPress={() => setEditing(preference.id)}>
                      Edit
                    </Button>
                    <Button size="sm" isDisabled={remove.isPending} aria-label={`Remove: ${preference.statement}`} onPress={() => remove.mutate(preference.id)}>
                      Remove
                    </Button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
