"use client";

import { type ReactNode, useState } from "react";
import { Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { Button as AriaButton } from "react-aria-components";
import { describeFailure } from "@/lib/api/problems";
import styles from "./hiring.module.css";

/**
 * A confirmation before something that cannot be taken back quietly (Phase 1 dialog pattern). `onConfirm` resolves when the
 * server has done it, which closes the dialog, and rejects with the failure, which the dialog says in words and stays open.
 */
export function ConfirmDialog({
  trigger,
  title,
  children,
  confirm,
  onConfirm,
}: {
  trigger: string;
  title: string;
  children: ReactNode;
  confirm: string;
  onConfirm: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function run() {
    setProblem(null);
    setPending(true);
    try {
      await onConfirm();
      setOpen(false);
    } catch (error) {
      setProblem(describeFailure(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <AriaButton className={buttonClass("secondary", "sm")}>{trigger}</AriaButton>
      <ModalOverlay className={styles.overlay} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog}>
            {({ close }) => (
              <>
                <Heading slot="title" className="t-section">
                  {title}
                </Heading>
                <div>{children}</div>
                {problem && (
                  <p role="alert" className={styles.problem}>
                    {problem}
                  </p>
                )}
                <div className={styles.row}>
                  <Button variant="primary" isPending={pending} onPress={run}>
                    {confirm}
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
