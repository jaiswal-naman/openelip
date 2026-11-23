"use client";

import { useState } from "react";
import { Button as AriaButton, Dialog, Heading, Menu, MenuItem, MenuTrigger, Modal, ModalOverlay, Popover } from "react-aria-components";
import { ChevronDownIcon } from "@/components/icons/icons";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import menu from "@/components/ui/menu.module.css";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { APPLICATION_STATUS_LABELS } from "@/lib/labels";
import { type ApplicationStatus, useRecordStatus } from "./queries";
import styles from "./applications.module.css";

const STATUSES = Object.keys(APPLICATION_STATUS_LABELS) as ApplicationStatus[];

/** The local time now in the form a datetime-local field takes. */
function nowLocal(): string {
  const now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  return now.toISOString().slice(0, 16);
}

function problemText(error: unknown): string {
  if (isFailure(error, "conflict") && error.details.state === "same_status") return "This application already has that status.";
  if (isFailure(error, "validation_failed")) return "That time is not allowed: a new status cannot be dated before the current one, or in the future.";
  return describeFailure(error);
}

/** "Record status...": a menu of the five statuses; the chosen one opens a dialog for when it happened (APP-01). */
export function RecordStatusMenu({ applicationId, title }: { applicationId: string; title: string }) {
  const record = useRecordStatus(applicationId);
  const [chosen, setChosen] = useState<ApplicationStatus | null>(null);
  const [when, setWhen] = useState(nowLocal);

  function save() {
    if (chosen === null || when === "") return;
    record.mutate({ status: chosen, occurred_at: new Date(when).toISOString() }, { onSuccess: () => setChosen(null) });
  }

  return (
    <>
      <MenuTrigger>
        <AriaButton className={buttonClass("secondary", "sm")} aria-label={`Record status for ${title}`}>
          Record status...
          <ChevronDownIcon />
        </AriaButton>
        <Popover className={menu.popover} placement="bottom start">
          <Menu
            className={menu.menu}
            aria-label={`Status for ${title}`}
            onAction={(key) => {
              record.reset();
              setWhen(nowLocal());
              setChosen(key as ApplicationStatus);
            }}
          >
            {STATUSES.map((status) => (
              <MenuItem key={status} id={status} textValue={APPLICATION_STATUS_LABELS[status]} className={menu.item}>
                <span>{APPLICATION_STATUS_LABELS[status]}</span>
              </MenuItem>
            ))}
          </Menu>
        </Popover>
      </MenuTrigger>
      <ModalOverlay className={styles.overlay} isOpen={chosen !== null} onOpenChange={(open) => !open && setChosen(null)} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog}>
            {({ close }) => (
              <>
                <Heading slot="title" className="t-section">
                  {chosen === null ? "" : `Record: ${APPLICATION_STATUS_LABELS[chosen]}`}
                </Heading>
                <p className="muted">{title}</p>
                <label className={styles.field}>
                  <span className="t-card">When did it happen?</span>
                  <input type="datetime-local" value={when} onChange={(event) => setWhen(event.target.value)} className={styles.input} />
                </label>
                {record.error && (
                  <p role="alert" className={styles.problem}>
                    {problemText(record.error)} Nothing was recorded.
                  </p>
                )}
                <div className={styles.buttons}>
                  <Button variant="primary" isPending={record.isPending} onPress={save}>
                    Record
                  </Button>
                  <Button onPress={close}>Cancel</Button>
                </div>
              </>
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </>
  );
}
