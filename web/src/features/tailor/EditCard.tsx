"use client";

import { Label, Radio, RadioGroup } from "react-aria-components";
import balloon from "@/components/ui/balloon.module.css";
import { describeFailure } from "@/lib/api/problems";
import { wordDiff } from "@/lib/wordDiff";
import type { EditDecision, EditView } from "./queries";
import styles from "./tailor.module.css";

const SECTION_LABELS: Record<string, string> = {
  experience: "Experience",
  skills: "Skills",
  education: "Education",
  certifications: "Certifications",
  projects: "Projects",
};

const DECISION_LABELS: Record<string, string> = { pending: "Not decided yet", approved: "Approved", rejected: "Rejected" };

/** The text with removed words struck through and added words underlined; the words themselves say which, for a screen reader. */
function Marked({ before, after, side }: { before: string; after: string; side: "before" | "after" }) {
  const runs = wordDiff(before, after);
  return (
    <p className={styles.diff}>
      {runs.map((run, index) => {
        if (run.kind === "equal") return <span key={index}>{run.text} </span>;
        if (run.kind === "removed") {
          return side === "after" ? null : (
            <del key={index} className={styles.removed}>
              <span className="sr-only">Removed: </span>
              {run.text}{" "}
            </del>
          );
        }
        return side === "before" ? null : (
          <ins key={index} className={styles.added}>
            <span className="sr-only">Added: </span>
            {run.text}{" "}
          </ins>
        );
      })}
    </p>
  );
}

/** One verified edit: what changes, the facts it rests on, and the person's decision. Discarded edits never reach this. */
export function EditCard({
  edit,
  onDecide,
  error,
}: {
  edit: EditView;
  onDecide: (decision: EditDecision) => void;
  error: unknown;
}) {
  const decided = edit.decision !== "pending";
  const reorder = edit.op === "reorder";
  return (
    <li className={styles.edit} data-op={edit.op}>
      <div className={styles.editHead}>
        <span className={`${balloon.balloon} ${styles.itemNo}`} aria-hidden="true">
          {edit.ordinal}
        </span>
        <p className="t-label">{SECTION_LABELS[edit.section] ?? edit.section}</p>
      </div>
      {reorder ? (
        <>
          <p>Reordered to match the job&apos;s emphasis. No new skills.</p>
          <ol className={styles.lines}>
            {edit.after_text.split("\n").map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ol>
        </>
      ) : (
        <div className={styles.sides}>
          <div className={styles.side}>
            <div className={styles.sideHead}>
              <span className={styles.letter} aria-hidden="true">
                A
              </span>
              <p className="t-label">Before</p>
            </div>
            <Marked before={edit.before_text} after={edit.after_text} side="before" />
          </div>
          <div className={styles.side}>
            <div className={styles.sideHead}>
              <span className={styles.letter} aria-hidden="true">
                B
              </span>
              <p className="t-label">After</p>
            </div>
            <Marked before={edit.before_text} after={edit.after_text} side="after" />
          </div>
        </div>
      )}
      {edit.supporting_fact_ids.length > 0 && (
        <p className="t-small muted">
          Supported by your record:{" "}
          {edit.supporting_fact_ids.map((id, index) => (
            <a key={id} href="/app/record" className={styles.chip}>
              fact {index + 1}
            </a>
          ))}
        </p>
      )}
      <RadioGroup
        orientation="horizontal"
        value={decided ? edit.decision : ""}
        isDisabled={decided}
        onChange={(value) => onDecide(value as EditDecision)}
        className={styles.decision}
      >
        <Label className="t-card">Your decision on this edit</Label>
        <Radio value="approved" className={styles.radio}>
          Approve
        </Radio>
        <Radio value="rejected" className={styles.radio}>
          Reject
        </Radio>
      </RadioGroup>
      <p role="status" className="t-small">
        {DECISION_LABELS[edit.decision] ?? edit.decision}
      </p>
      {error !== null && error !== undefined && (
        <p role="alert" className={styles.problem}>
          {describeFailure(error)} Your decision was not saved.
        </p>
      )}
    </li>
  );
}
