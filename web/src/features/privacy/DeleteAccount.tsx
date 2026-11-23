"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { TextField } from "@/components/ui/TextField";
import { authKeys } from "@/features/auth/queries";
import { subprocessorsQuery } from "@/features/trust/queries";
import { useApi } from "@/lib/api/context";
import { describeFailure, problemOf } from "@/lib/api/problems";
import { waitPhrase } from "@/lib/labels";
import { deleteAccount, sendDeletionCode } from "./api";
import styles from "./privacy.module.css";

// What a deletion removes in this phase, in the words of the screens that show it. A later phase adds its own to this list.
const REMOVED = [
  "Your record, which holds the rest of this list",
  "Your uploaded documents (resumes)",
  "Your facts, confirmed or not",
  "Your hard constraints",
  "Your preferences",
  "The change log",
  "Your consents",
  "Your sessions: you are signed out everywhere",
];
const FALLBACK_OUTSIDE =
  "Copies made outside this system, for example an email that has already been delivered to someone else, cannot be reached by deletion.";
export const DELETED_PATH = "/app/settings/deleted";

/** The sentence under the code field for a code that did not work: how many tries are left, or that a new code is needed. */
function wrongCode(attemptsRemaining: number | null): string {
  if (attemptsRemaining === 0) return "That code can no longer be used. Send a new one.";
  const left = attemptsRemaining === null ? "" : ` ${attemptsRemaining} ${attemptsRemaining === 1 ? "try" : "tries"} left.`;
  return `That code does not match or has expired. Nothing was deleted.${left}`;
}

/**
 * Delete account (TRU-02). The dialog lists what is removed and what cannot be reached, then asks for a fresh code sent to
 * the account's own address: the API checks it, and a wrong one changes nothing, so the dialog cannot skip it. On success
 * the person is signed out and taken to a page that says so.
 */
export function DeleteAccount({ email }: { email: string }) {
  const api = useApi();
  const router = useRouter();
  const queryClient = useQueryClient();
  const outside = useQuery(subprocessorsQuery(api)).data?.outside_copies_statement ?? FALLBACK_OUTSIDE;
  const [validFor, setValidFor] = useState<number | null>(null);
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: () => sendDeletionCode(api, email),
    onSuccess: (sent) => {
      setValidFor(sent.expires_in_seconds);
      setCode("");
      setProblem(null);
    },
  });
  const erase = useMutation({
    mutationFn: (digits: string) => deleteAccount(api, digits),
    onSuccess: () => {
      // The session is gone: forget the account and everything it read, then say so on a page that needs no one signed in.
      queryClient.removeQueries();
      queryClient.setQueryData(authKeys.me, null);
      router.replace(DELETED_PATH);
      router.refresh();
    },
    onError: (error) => {
      const found = problemOf(error);
      setProblem(found.kind === "code_invalid" ? wrongCode(found.attemptsRemaining) : describeFailure(error));
    },
  });

  const digits = code.replace(/\s/g, "");
  const reset = () => {
    setValidFor(null);
    setCode("");
    setProblem(null);
    send.reset();
    erase.reset();
  };

  return (
    <section className={styles.card} aria-labelledby="delete-title">
      <h2 id="delete-title" className="t-section">
        Delete your account
      </h2>
      <p className={styles.lead}>Removes your account and everything in it. This cannot be undone.</p>
      <DialogTrigger onOpenChange={(open) => !open && reset()}>
        <AriaButton className={`${buttonClass("secondary")} ${styles.danger}`}>Delete account</AriaButton>
        <ModalOverlay className={styles.overlay} isDismissable>
          <Modal className={styles.modal}>
            <Dialog className={styles.dialog}>
              {({ close }) => (
                <>
                  <Heading slot="title" className="t-section">
                    Delete your account?
                  </Heading>
                  <p>This removes, now:</p>
                  <ul className={styles.removed}>
                    {REMOVED.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                  <p>{outside}</p>

                  {validFor === null ? (
                    <>
                      <p>To make sure it is you, we send a code to {email}.</p>
                      {send.isError && (
                        <p role="alert" className={styles.problem}>
                          {describeFailure(send.error)}
                        </p>
                      )}
                      <div className={styles.buttons}>
                        <Button variant="primary" isPending={send.isPending} onPress={() => send.mutate()}>
                          Send code
                        </Button>
                        <Button onPress={close}>Cancel</Button>
                      </div>
                    </>
                  ) : (
                    <>
                      <p>
                        We sent a six-digit code to <strong>{email}</strong>. It is valid for {waitPhrase(validFor)}.
                      </p>
                      <TextField
                        label="Code from the email"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        description="Six digits. Pasting is fine."
                        value={code}
                        onChange={setCode}
                        error={problem}
                        code
                        autoFocus
                      />
                      <div className={styles.buttons}>
                        <AriaButton
                          className={`${buttonClass("primary")} ${styles.dangerFill}`}
                          isDisabled={!/^\d{6}$/.test(digits) || erase.isPending}
                          onPress={() => erase.mutate(digits)}
                        >
                          Delete my account
                        </AriaButton>
                        <Button isDisabled={send.isPending || erase.isPending} onPress={() => send.mutate()}>
                          Send a new code
                        </Button>
                        <Button variant="quiet" onPress={close}>
                          Cancel
                        </Button>
                      </div>
                      <p role="status" className="sr-only">
                        {erase.isPending ? "Deleting your account." : ""}
                      </p>
                    </>
                  )}
                </>
              )}
            </Dialog>
          </Modal>
        </ModalOverlay>
      </DialogTrigger>
    </section>
  );
}
