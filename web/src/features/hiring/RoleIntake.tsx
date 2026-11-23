"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Form, TextArea } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import { ErrorState } from "@/components/ui/States";
import { TextField } from "@/components/ui/TextField";
import { LinkButton } from "@/components/ui/LinkButton";
import { describeFailure } from "@/lib/api/problems";
import { IMPORTANCE_LABELS, ROLE_STATUS_LABELS } from "@/lib/labels";
import { HighlightedText } from "@/ui/patterns/HighlightedText";
import { type RoleView, useCloseRole, useCreateRole, useMembership, usePatchRole, useRole } from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import { PayRange, PayRangeReadOnly } from "./PayRange";
import { PreviewPanel } from "./PreviewPanel";
import { PublishBar } from "./PublishBar";
import { RequirementsEditor } from "./RequirementsEditor";
import styles from "./hiring.module.css";

const MAX_TITLE = 200;
const MAX_DESCRIPTION = 20_000;

/** /hire/roles/new: a title and the description, saved as a draft; the requirements are read from the text. */
export function NewRole() {
  const router = useRouter();
  const { membership } = useMembership();
  const create = useCreateRole();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  if (membership === null) return <p className={styles.muted}>Create your organization on the Roles page first.</p>;
  return (
    <div className={styles.page}>
      <h1 className="t-page">New role</h1>
      <Form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate({ org_id: membership.id, title: title.trim(), description }, { onSuccess: (role) => router.push(`/hire/roles/${role.id}`) });
        }}
      >
        <TextField label="Role title" value={title} onChange={setTitle} isRequired maxLength={MAX_TITLE} />
        <label className="t-card" htmlFor="new-description">Describe the role</label>
        <TextArea
          id="new-description"
          className={styles.textarea}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          maxLength={MAX_DESCRIPTION}
          placeholder="Paste a job description or write it in your own words."
          required
        />
        {create.isError && (
          <p role="alert" className={styles.problem}>
            {describeFailure(create.error)} Nothing was saved.
          </p>
        )}
        <div className={styles.row}>
          <Button type="submit" variant="primary" isPending={create.isPending} isDisabled={title.trim() === "" || description.trim() === ""}>
            Save draft
          </Button>
        </div>
      </Form>
    </div>
  );
}

/** /hire/roles/[id]: from text to published (FRONTEND_UI 3.13). An open or closed role is read-only. */
export function RoleIntake({ id }: { id: string }) {
  const role = useRole(id);
  if (role.isError) return <ErrorState what="Could not load this role." error={role.error} onRetry={() => void role.refetch()} />;
  if (role.data === undefined) return <p role="status" className="muted">Loading the role.</p>;
  return <Intake role={role.data} />;
}

function Intake({ role }: { role: RoleView }) {
  const draft = role.status === "draft";
  const patch = usePatchRole(role.id);
  const close = useCloseRole(role.id, role.organization_id);
  const [description, setDescription] = useState(role.description);
  const [extraction, setExtraction] = useState(role.extraction ?? null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const source = role.requirements.find((item) => item.id === sourceId)?.span ?? null;
  const state = extraction?.state;

  return (
    <div className={styles.page}>
      <div className={styles.head}>
        <h1 className="t-page">{role.title}</h1>
        <span className={styles.row}>
          <Pill tone={role.status === "open" ? "ok" : "unk"}>{ROLE_STATUS_LABELS[role.status]}</Pill>
          {role.status === "open" && (
            <>
              <LinkButton href={`/hire/shortlist/${role.id}`}>Shortlist</LinkButton>
              <ConfirmDialog trigger="Close role" title="Close this role?" confirm="Close role" onConfirm={() => close.mutateAsync()}>
                Closing withdraws every ask that is still waiting for a reply. People who already accepted keep their answer due.
              </ConfirmDialog>
            </>
          )}
          {role.status === "closed" && <LinkButton href={`/hire/shortlist/${role.id}`}>Shortlist</LinkButton>}
        </span>
      </div>

      <div className={styles.split}>
        <section aria-labelledby="describe-heading" className={styles.shell}>
          <h2 id="describe-heading" className="t-label">
            Describe the role
          </h2>
          {draft ? (
            <>
              <TextArea
                aria-labelledby="describe-heading"
                className={styles.textarea}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={MAX_DESCRIPTION}
              />
              <div className={styles.row}>
                <Button
                  isPending={patch.isPending}
                  isDisabled={description.trim() === ""}
                  onPress={() => patch.mutate({ description }, { onSuccess: (saved) => setExtraction(saved.extraction ?? null) })}
                >
                  Extract
                </Button>
              </div>
              {patch.isError && (
                <p role="alert" className={styles.problem}>
                  {describeFailure(patch.error)} Nothing was changed.
                </p>
              )}
              {state === "ai_disabled" && <p role="status">Extraction is unavailable on this server. Add the requirements yourself.</p>}
              {state === "failed" && <p role="status">Extraction failed. Extract again, or add the requirements yourself.</p>}
              {state === "empty" && <p role="status">No requirements were found in this text. Add them yourself.</p>}
            </>
          ) : (
            <p className={`${styles.words} ${styles.source}`}>{role.description}</p>
          )}
          {source && (
            <div aria-label="Source sentence">
              <HighlightedText text={role.description} span={source} announcement="Source sentence highlighted." />
            </div>
          )}
        </section>

        {draft ? (
          <RequirementsEditor roleId={role.id} items={role.requirements} onShowSource={setSourceId} />
        ) : (
          <section aria-labelledby="requirements-heading" className={styles.shell}>
            <h2 id="requirements-heading" className="t-label">
              Requirements
            </h2>
            <ul className={styles.list}>
              {role.requirements.map((item) => (
                <li key={item.id} className={styles.words}>
                  {item.text} <span className={styles.muted}>({IMPORTANCE_LABELS[item.importance]})</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      {draft ? <PayRange role={role} /> : <PayRangeReadOnly role={role} />}
      <PreviewPanel role={role} />
      {draft && <PublishBar role={role} />}
    </div>
  );
}
