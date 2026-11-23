"use client";

import { useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Heading, Label, Modal, ModalOverlay, Radio, RadioGroup } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { TextField } from "@/components/ui/TextField";
import { describeFailure } from "@/lib/api/problems";
import { DISMISS_REASON_LABELS } from "@/lib/labels";
import type { DismissReason } from "./queries";
import styles from "./match.module.css";

const REASONS = Object.keys(DISMISS_REASON_LABELS) as DismissReason[];
const MAX_COMMENT = 500; // the API's limit on the comment

/**
 * "Dismiss..." (MAT-08): a dialog that asks why before it records anything, because the reason is what the next matches
 * learn from. The comment is optional. `onDismiss` resolves when the server has recorded the dismissal, which closes the
 * dialog, and rejects with the failure, which the dialog says in words and keeps itself open so nothing typed is lost.
 */
export function DismissDialog({
  onDismiss,
  isPending,
}: {
  onDismiss: (reason: DismissReason, comment: string) => Promise<unknown>;
  isPending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<DismissReason | null>(null);
  const [comment, setComment] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  async function submit() {
    if (reason === null) return;
    setProblem(null);
    try {
      await onDismiss(reason, comment);
      setOpen(false);
      setReason(null);
      setComment("");
    } catch (error) {
      setProblem(describeFailure(error));
    }
  }

  return (
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <AriaButton className={buttonClass("secondary")}>Dismiss...</AriaButton>
      <ModalOverlay className={styles.overlay} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog}>
            {({ close }) => (
              <>
                <Heading slot="title" className="t-section">
                  Dismiss this job?
                </Heading>
                <p>
                  Pick the reason that fits best. It is kept with your feedback. Some reasons suggest a change to your record; a suggestion
                  changes nothing until you confirm it.
                </p>
                <RadioGroup value={reason} onChange={(value) => setReason(value as DismissReason)} isRequired className={styles.reasons}>
                  <Label className="t-card">Why are you dismissing it?</Label>
                  {REASONS.map((code) => (
                    <Radio key={code} value={code} className={styles.reason}>
                      {DISMISS_REASON_LABELS[code]}
                    </Radio>
                  ))}
                </RadioGroup>
                <TextField label="Anything to add? (optional)" value={comment} onChange={setComment} maxLength={MAX_COMMENT} />
                {problem && (
                  <p role="alert" className={styles.problem}>
                    {problem} Nothing was dismissed.
                  </p>
                )}
                <div className={styles.buttons}>
                  <Button variant="primary" isDisabled={reason === null} isPending={isPending} onPress={submit}>
                    Dismiss job
                  </Button>
                  <Button onPress={close}>Cancel</Button>
                </div>
              </>
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </DialogTrigger>
  );
}
