"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import { NativeSelect } from "@/ui/patterns/NativeSelect";
import { describeFailure } from "@/lib/api/problems";
import { IMPORTANCE_LABELS } from "@/lib/labels";
import { type RequirementIn, type RequirementView, usePutRequirements } from "./api";
import styles from "./hiring.module.css";

type Importance = RequirementView["importance"];
const MAX_TEXT = 1000;

/** What the server keeps of a row when it is sent back: the span stays only while the text is unchanged. */
const asInput = (item: RequirementView): RequirementIn => ({
  kind: item.kind,
  text: item.text,
  importance: item.importance,
  is_confirmed: item.is_confirmed,
  span: item.span ?? null,
});

/**
 * The requirement list (HIR-02). Every change sends the whole list with PUT, because that is the route; ids are read again from
 * the answer. Typed or edited items carry no span, so their source highlight is gone. A person who types or edits an item has
 * read it, so saving one confirms it.
 */
export function RequirementsEditor({ roleId, items, onShowSource }: { roleId: string; items: RequirementView[]; onShowSource: (id: string) => void }) {
  const put = usePutRequirements(roleId);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [text, setText] = useState("");
  const [importance, setImportance] = useState<Importance>("required");

  const send = (next: RequirementIn[], done?: () => void) => put.mutate(next, { onSuccess: done });
  const all = items.map(asInput);

  function begin(id: string | "new", from?: RequirementView) {
    setEditing(id);
    setText(from?.text ?? "");
    setImportance(from?.importance ?? "required");
  }

  function save() {
    const value = text.trim();
    if (value === "") return;
    const edited: RequirementIn = { kind: "other", text: value, importance, is_confirmed: true, span: null };
    if (editing === "new") return send([...all, edited], () => setEditing(null));
    const index = items.findIndex((item) => item.id === editing);
    send(all.map((row, i) => (i === index ? { ...row, ...edited, kind: row.kind } : row)), () => setEditing(null));
  }

  const form = (
    <div className={styles.card}>
      <TextField label="Requirement" value={text} onChange={setText} maxLength={MAX_TEXT} />
      <NativeSelect
        label="Importance"
        value={importance}
        onChange={(next) => setImportance(next as Importance)}
        options={(["required", "preferred"] as const).map((value) => ({ value, label: IMPORTANCE_LABELS[value] }))}
      />
      <div className={styles.row}>
        <Button variant="primary" size="sm" isPending={put.isPending} isDisabled={text.trim() === ""} onPress={save}>
          Save requirement
        </Button>
        <Button size="sm" onPress={() => setEditing(null)}>
          Cancel
        </Button>
      </div>
    </div>
  );

  return (
    <section aria-labelledby="requirements-heading" className={styles.shell}>
      <h2 id="requirements-heading" className="t-label">
        Requirements
      </h2>
      {items.length === 0 && editing !== "new" && <p className={styles.muted}>No requirements yet.</p>}
      <ul className={styles.list}>
        {items.map((item, index) => (
          <li key={item.id}>
            {editing === item.id ? (
              form
            ) : (
              <div className={styles.card}>
                <p className={styles.words}>{item.text}</p>
                <p className={styles.muted}>
                  {IMPORTANCE_LABELS[item.importance]}, {item.is_confirmed ? "confirmed" : "not confirmed yet"}
                </p>
                <div className={styles.row}>
                  {!item.is_confirmed && (
                    <Button size="sm" aria-label={`Confirm: ${item.text}`} isDisabled={put.isPending} onPress={() => send(all.map((row, i) => (i === index ? { ...row, is_confirmed: true } : row)))}>
                      Confirm
                    </Button>
                  )}
                  <Button size="sm" aria-label={`Edit: ${item.text}`} onPress={() => begin(item.id, item)}>
                    Edit
                  </Button>
                  <Button size="sm" aria-label={`Remove: ${item.text}`} isDisabled={put.isPending} onPress={() => send(all.filter((_, i) => i !== index))}>
                    Remove
                  </Button>
                  {item.span && (
                    <Button size="sm" aria-label={`Show source of: ${item.text}`} onPress={() => onShowSource(item.id)}>
                      Show source
                    </Button>
                  )}
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
      {editing === "new" ? (
        form
      ) : (
        <div className={styles.row}>
          <Button size="sm" onPress={() => begin("new")}>
            Add requirement
          </Button>
        </div>
      )}
      {put.isError && (
        <p role="alert" className={styles.problem}>
          {describeFailure(put.error)} The list was not saved.
        </p>
      )}
    </section>
  );
}
