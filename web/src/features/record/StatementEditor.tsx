"use client";

import { useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import styles from "./record.module.css";

const MAX_STATEMENT = 2000; // the API's own limit for a statement

type Props = {
  label: string;
  initial: string;
  pending: boolean;
  onSave: (statement: string) => void;
  onCancel: () => void;
};

/** One line of text edited in place: a labelled field, Save and Cancel. The API checks the words; this only refuses an empty one. */
export function StatementEditor({ label, initial, pending, onSave, onCancel }: Props) {
  const [problem, setProblem] = useState<string | null>(null);

  return (
    <Form
      validationBehavior="aria"
      className={styles.editor}
      onSubmit={(event) => {
        event.preventDefault();
        const next = String(new FormData(event.currentTarget).get("statement") ?? "").trim();
        if (next === "") {
          setProblem("Write something, or cancel.");
          return;
        }
        setProblem(null);
        onSave(next);
      }}
    >
      <TextField name="statement" label={label} defaultValue={initial} maxLength={MAX_STATEMENT} error={problem} autoFocus />
      <div className={styles.buttons}>
        <Button type="submit" variant="primary" size="sm" isPending={pending}>
          Save
        </Button>
        <Button size="sm" onPress={onCancel} isDisabled={pending}>
          Cancel
        </Button>
      </div>
    </Form>
  );
}
