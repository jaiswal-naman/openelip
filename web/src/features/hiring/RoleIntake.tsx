"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Form, TextArea } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { ErrorState } from "@/components/ui/States";
import { TextField } from "@/components/ui/TextField";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { describeFailure } from "@/lib/api/problems";
import { IMPORTANCE_LABELS, ROLE_STATUS_LABELS } from "@/lib/labels";
import { HighlightedText } from "@/ui/patterns/HighlightedText";
import { type RoleView, useCloseRole, useCreateRole, useMembership, usePatchRole, useRole } from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import { PayRange, PayRangeReadOnly } from "./PayRange";
import { PreviewPanel } from "./PreviewPanel";
import { PublishBar } from "./PublishBar";
import { RequirementsEditor } from "./RequirementsEditor";
import { Zone } from "./Zone";
import styles from "./hiring.module.css";

const MAX_TITLE = 200;
const MAX_DESCRIPTION = 20_000;

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

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
      <TitleBlock
        title="New role"
        lines={
          <>
            <p className={styles.company}>{membership.name}</p>
            <p className="t-small">Saving keeps a draft and reads the requirements from your text. Nobody is asked until you publish.</p>
          </>
        }
      />
      <Form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate({ org_id: membership.id, title: title.trim(), description }, { onSuccess: (role) => router.push(`/hire/roles/${role.id}`) });
        }}
      >
        <div className={styles.numbered}>
          <span className={styles.zoneNo} aria-hidden="true">
            1
          </span>
          <TextField label="Role title" value={title} onChange={setTitle} isRequired maxLength={MAX_TITLE} />
        </div>
        <div className={styles.numbered}>
          <span className={styles.zoneNo} aria-hidden="true">
            2
          </span>
          <div className={styles.stack}>
            <label className={styles.fieldLabel} htmlFor="new-description">
              Describe the role
            </label>
            <TextArea
              id="new-description"
              className={styles.textarea}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={MAX_DESCRIPTION}
              placeholder="Paste a job description or write it in your own words."
              required
            />
          </div>
        </div>
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

const STATE_LINE: Record<RoleView["status"], string> = {
  draft: "Draft. Nobody is asked until you publish.",
  open: "Published. People are asked privately; the shortlist holds only those who said yes.",
  closed: "Closed. Nobody new is asked, and people who accepted keep their answer due.",
};

/** The page header's cells, from the role itself: its status, how many requirements are confirmed, and when it opened or closed. */
function titleCells(role: RoleView): TitleCell[] {
  const confirmed = role.requirements.filter((item) => item.is_confirmed).length;
  const total = role.requirements.length;
  const cells: TitleCell[] = [{ label: "Status", value: <Pill tone={role.status === "open" ? "ok" : "stage"}>{ROLE_STATUS_LABELS[role.status]}</Pill> }];
  if (role.status === "draft") {
    cells.push({ label: "Requirements", value: total === 0 ? "None yet" : `${confirmed} of ${total} confirmed`, tone: total === 0 ? "muted" : confirmed === total ? "ok" : "redline" });
    cells.push({ label: "Publishing", value: role.can_publish ? "Ready" : "Not ready", tone: role.can_publish ? "ok" : "redline" });
  } else {
    cells.push({ label: "Requirements", value: total, tone: "muted" });
    if (role.opened_at) cells.push({ label: "Opened", value: day(role.opened_at), tone: "muted" });
    if (role.closed_at) cells.push({ label: "Closed", value: day(role.closed_at), tone: "muted" });
  }
  return cells;
}

function Intake({ role }: { role: RoleView }) {
  const draft = role.status === "draft";
  const { membership } = useMembership();
  const patch = usePatchRole(role.id);
  const close = useCloseRole(role.id, role.organization_id);
  const [description, setDescription] = useState(role.description);
  const [extraction, setExtraction] = useState(role.extraction ?? null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const source = role.requirements.find((item) => item.id === sourceId)?.span ?? null;
  const state = extraction?.state;

  return (
    <div className={styles.page}>
      <TitleBlock
        title={role.title}
        lines={
          <>
            {membership && <p className={styles.company}>{membership.name}</p>}
            <p className="t-small">{STATE_LINE[role.status]}</p>
          </>
        }
        cells={titleCells(role)}
        actions={
          role.status === "draft" ? undefined : (
            <>
              <LinkButton href={`/hire/shortlist/${role.id}`} size="sm">
                Shortlist
              </LinkButton>
              {role.status === "open" && (
                <ConfirmDialog trigger="Close role" title="Close this role?" confirm="Close role" onConfirm={() => close.mutateAsync()}>
                  Closing withdraws every ask that is still waiting for a reply. People who already accepted keep their answer due.
                </ConfirmDialog>
              )}
            </>
          )
        }
      />

      <div className={styles.sheet}>
        <Zone id="describe-heading" number={1} title="Describe the role">
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
        </Zone>

        {draft ? (
          <RequirementsEditor roleId={role.id} items={role.requirements} onShowSource={setSourceId} />
        ) : (
          <Zone id="requirements-heading" number={2} title="Requirements">
            <ul className={styles.items}>
              {role.requirements.map((item) => (
                <li key={item.id} className={`${styles.item} ${styles.words}`}>
                  <span>
                    {item.text} <span className={styles.muted}>({IMPORTANCE_LABELS[item.importance]})</span>
                  </span>
                </li>
              ))}
            </ul>
          </Zone>
        )}

        {draft ? <PayRange role={role} /> : <PayRangeReadOnly role={role} />}
      </div>

      {draft ? (
        <div className={styles.outside}>
          <PreviewPanel role={role} />
          <PublishBar role={role} />
        </div>
      ) : (
        <PreviewPanel role={role} />
      )}
    </div>
  );
}
