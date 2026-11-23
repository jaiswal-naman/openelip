"use client";

import { useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Heading, Modal, ModalOverlay, TextArea } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { ANSWER_PRESETS } from "@/lib/labels";
import styles from "./hiring.module.css";

const MAX_REASON = 1000; // the API's limit (06-04)

const WORDS = {
  yes: { trigger: "Yes, let's talk", title: "Say yes", submit: "Send yes", note: "The candidate sees your reason, and your answer shares their email with you." },
  no: { trigger: "No, with reason", title: "Say no", submit: "Send no", note: "The candidate sees your reason. This closes the introduction." },
} as const;

/**
 * Yes or no, always with a reason (HIR-06). A preset fills the box and stays editable. It answers once: the button is pending
 * while the call runs, and a 409 already_resolved closes the dialog and asks the page to reload that card.
 */
export function AnswerDialog({
  answer,
  candidate,
  onSubmit,
  onStale,
}: {
  answer: "yes" | "no";
  candidate: string;
  onSubmit: (reason: string) => Promise<unknown>;
  onStale: () => void;
}) {
  const words = WORDS[answer];
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function submit() {
    if (reason.trim() === "" || pending) return;
    setProblem(null);
    setPending(true);
    try {
      await onSubmit(reason.trim());
      setOpen(false);
      setReason("");
    } catch (error) {
      if (isFailure(error, "already_resolved")) {
        setOpen(false);
        onStale();
      } else setProblem(describeFailure(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <AriaButton className={buttonClass(answer === "yes" ? "primary" : "secondary", "sm")} aria-label={`${words.trigger} (${candidate})`}>
        {words.trigger}
      </AriaButton>
      <ModalOverlay className={styles.overlay} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog}>
            {({ close }) => (
              <>
                <Heading slot="title" className="t-section">
                  {words.title}: {candidate}
                </Heading>
                <p>{words.note}</p>
                <div className={styles.presets} role="group" aria-label="Reasons to start from">
                  {ANSWER_PRESETS.map((preset) => (
                    <Button key={preset.label} size="sm" onPress={() => setReason(preset.text)}>
                      {preset.label}
                    </Button>
                  ))}
                </div>
                <label htmlFor={`reason-${answer}`} className="t-card">
                  Reason (required)
                </label>
                <TextArea
                  id={`reason-${answer}`}
                  className={styles.textarea}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  maxLength={MAX_REASON}
                  required
                />
                {problem && (
                  <p role="alert" className={styles.problem}>
                    {problem}
                  </p>
                )}
                <div className={styles.row}>
                  <Button variant="primary" isPending={pending} isDisabled={reason.trim() === ""} onPress={submit}>
                    {words.submit}
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
