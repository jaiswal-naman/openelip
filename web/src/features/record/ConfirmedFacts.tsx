"use client";

import { useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/States";
import { TextField } from "@/components/ui/TextField";
import { addFact, type FactKind, type FactDecision, type RecordFact, reviewFacts, useRecordWrite } from "./api";
import { SelectField } from "./formFields";
import { factKindLabel, sourceLabel, writeProblemText } from "./labels";
import styles from "./record.module.css";
import { StatementEditor } from "./StatementEditor";

const GROUPS = [
  { title: "Experience", kinds: ["role", "achievement", "project"] },
  { title: "Skills", kinds: ["skill"] },
  { title: "Education", kinds: ["education", "certification"] },
  { title: "Other", kinds: ["other"] },
];
const KIND_OPTIONS = ["role", "achievement", "skill", "education", "certification", "project", "other"].map((value) => ({ value, label: factKindLabel(value) }));

/**
 * The facts the person confirmed, the only ones used anywhere (REC-02), grouped as a resume is: each can be reworded or
 * removed in place, and a fact can be added by hand. Typing a fact needs no AI and no consent. Rewording replaces the
 * fact with one in the person's words; removing retires it. Both are in the change log.
 */
export function ConfirmedFacts({ facts }: { facts: RecordFact[] }) {
  const write = useRecordWrite(reviewFacts);
  const [editing, setEditing] = useState<string | null>(null);
  const decide = (decision: FactDecision) => write.mutate([decision], { onSuccess: () => setEditing(null) });

  return (
    <div className={styles.review}>
      {facts.length === 0 && <EmptyState>No confirmed facts yet. Upload a resume and confirm what is read from it, or add a fact by hand below.</EmptyState>}
      {GROUPS.map((group) => {
        const inGroup = facts.filter((fact) => group.kinds.includes(fact.kind));
        if (inGroup.length === 0) return null;
        return (
          <div key={group.title} className={styles.group}>
            <h3 className="t-section">{group.title}</h3>
            <ul className={styles.facts}>
              {inGroup.map((fact) => (
                <li key={fact.id} className={styles.confirmed}>
                  {editing === fact.id ? (
                    <StatementEditor
                      label="Your wording"
                      initial={fact.statement}
                      pending={write.isPending}
                      onCancel={() => setEditing(null)}
                      onSave={(statement) => decide({ fact_id: fact.id, action: "correct", statement })}
                    />
                  ) : (
                    <>
                      <div className={styles.what}>
                        <p className="t-label muted">{factKindLabel(fact.kind)}</p>
                        <p className={styles.statement}>{fact.statement}</p>
                        <p className="t-small muted">{sourceLabel(fact.source)}</p>
                      </div>
                      <div className={styles.actions}>
                        <Button size="sm" isDisabled={write.isPending} aria-label={`Edit: ${fact.statement}`} onPress={() => setEditing(fact.id)}>
                          Edit
                        </Button>
                        <Button size="sm" isDisabled={write.isPending} aria-label={`Remove: ${fact.statement}`} onPress={() => decide({ fact_id: fact.id, action: "reject" })}>
                          Remove
                        </Button>
                      </div>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {write.isError && (
        <p role="alert" className={styles.problem}>
          {writeProblemText(write.error)}
        </p>
      )}
      <AddFact />
    </div>
  );
}

/** A fact written by the person: confirmed at once, because they wrote it. */
function AddFact() {
  const add = useRecordWrite(addFact);
  const [kind, setKind] = useState<FactKind>("role");
  const [statement, setStatement] = useState("");
  const [empty, setEmpty] = useState<string | null>(null);

  return (
    <Form
      validationBehavior="aria"
      className={styles.addFact}
      aria-label="Add a fact"
      onSubmit={(event) => {
        event.preventDefault();
        if (statement.trim() === "") {
          setEmpty("Write the fact first.");
          return;
        }
        setEmpty(null);
        add.mutate({ kind, statement: statement.trim() }, { onSuccess: () => setStatement("") });
      }}
    >
      <h3 className="t-section">Add a fact</h3>
      <div className={styles.addRow}>
        <SelectField label="Kind" value={kind} options={KIND_OPTIONS} onChange={(next) => setKind(next as FactKind)} />
        <TextField label="The fact, in your words" value={statement} onChange={setStatement} error={empty} />
      </div>
      {add.isError && (
        <p role="alert" className={styles.problem}>
          {writeProblemText(add.error)}
        </p>
      )}
      <div>
        <Button type="submit" isPending={add.isPending}>
          Add fact
        </Button>
      </div>
    </Form>
  );
}
