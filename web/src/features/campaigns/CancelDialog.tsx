"use client";

import { useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Heading, Modal, ModalOverlay, Radio, RadioGroup, Label } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { describeFailure } from "@/lib/api/problems";
import { useCampaignControl } from "./queries";
import styles from "./campaigns.module.css";

/**
 * "Cancel...": a dialog that lists what stops and what cannot be undone before anything happens (CMP-07). The reason is asked
 * only at the offer stage, where an offer can have been accepted or declined.
 */
export function CancelDialog({ campaignId, stage, label = "Cancel..." }: { campaignId: string; stage: string; label?: string }) {
  const { cancel } = useCampaignControl(campaignId);
  const [reason, setReason] = useState<"cancelled" | "offer_accepted" | "offer_declined">(stage === "offer" ? "offer_accepted" : "cancelled");
  return (
    <DialogTrigger>
      <AriaButton className={buttonClass("secondary", "sm")}>{label}</AriaButton>
      <ModalOverlay className={styles.overlay} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog}>
            {({ close }) => (
              <>
                <Heading slot="title" className="t-section">
                  Cancel this campaign?
                </Heading>
                <p>This stops now:</p>
                <ul>
                  <li>The agents stop researching and drafting.</li>
                  <li>Every approval waiting for you expires.</li>
                  <li>A follow-up that is scheduled but not yet sent is cancelled.</li>
                </ul>
                <p>This cannot be undone: a closed campaign cannot be reopened. Anything already sent stays sent. You can start a new campaign for the job.</p>
                {stage === "offer" && (
                  <RadioGroup value={reason} onChange={(value) => setReason(value as typeof reason)} className={styles.stack}>
                    <Label className="t-card">What happened to the offer?</Label>
                    <Radio value="offer_accepted" className={styles.radio}>
                      I accepted it
                    </Radio>
                    <Radio value="offer_declined" className={styles.radio}>
                      I declined it
                    </Radio>
                    <Radio value="cancelled" className={styles.radio}>
                      Neither, I am just closing it
                    </Radio>
                  </RadioGroup>
                )}
                {cancel.error && (
                  <p role="alert" className={styles.problem}>
                    {describeFailure(cancel.error)} The campaign was not cancelled.
                  </p>
                )}
                <div className={styles.row}>
                  <Button variant="primary" isPending={cancel.isPending} onPress={() => cancel.mutate(stage === "offer" && reason !== "cancelled" ? reason : null, { onSuccess: close })}>
                    Cancel the campaign
                  </Button>
                  <Button onPress={close}>Keep it</Button>
                </div>
              </>
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </DialogTrigger>
  );
}
