"use client";

import Link from "next/link";
import { useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { TextField } from "@/components/ui/TextField";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { FINISH_REASON_LABELS, messageFor } from "@/lib/labels";
import { useChatContext, setChatContext } from "./context";
import { MessageText } from "./MessageText";
import { ProposalCard } from "./ProposalCard";
import type { Reply } from "./reply";
import { useChat } from "./useChat";
import styles from "./chat.module.css";

const MAX_TEXT = 4000; // the API's limit on one message

/** What to say when a message could not be sent, in the words of API.md 1.11, with the next step where there is one. */
function Refusal({ error }: { error: unknown }) {
  if (isFailure(error, "ai_disabled")) {
    return (
      <p role="alert" className={styles.problem}>
        AI features are not set up on this server. You can still <Link href="/jobs">search jobs</Link>.
      </p>
    );
  }
  if (isFailure(error, "consent_required")) {
    return (
      <p role="alert" className={styles.problem}>
        Chat needs your agreement to AI processing. <Link href="/app/settings?section=privacy">Review and agree</Link>.
      </p>
    );
  }
  if (isFailure(error, "conflict") && error.details.state === "reply_running") {
    return (
      <p role="alert" className={styles.problem}>
        A reply is still being written. Wait for it to finish, or stop it.
      </p>
    );
  }
  return (
    <p role="alert" className={styles.problem}>
      {isFailure(error, "rate_limited") ? messageFor(error) : `${describeFailure(error)} Your message was not sent.`}
    </p>
  );
}

function ReplyView({ reply }: { reply: Reply }) {
  const ended = reply.finishReason !== null && reply.finishReason !== "complete" ? FINISH_REASON_LABELS[reply.finishReason] : "";
  return (
    <div className={styles.assistant}>
      {reply.status !== null && <p className={styles.status}>{reply.status}</p>}
      {reply.constraintsApplied && <p className="t-small muted">Your hard constraints were applied</p>}
      {reply.text !== "" && <MessageText text={reply.text} citations={reply.citations} />}
      {reply.error !== null && (
        <p className={styles.problem}>{reply.error.code === "ai_disabled" ? "AI features are not set up on this server." : "Something went wrong while writing this reply."}</p>
      )}
      {ended !== "" && <p className="t-small muted">{ended}</p>}
      {reply.proposals.map((proposal) => (
        <ProposalCard key={proposal.event_id} proposal={proposal} />
      ))}
    </div>
  );
}

/**
 * "Ask the agent" (CHT-01 to CHT-04): a dialog with the conversation, the composer and Stop. Escape closes it and focus
 * returns to the button. From a page about a job it says "context: {company}" and sends that job with the message. The
 * reply is announced once, when it ends; the words arriving are not in a live region.
 */
export function ChatPanel() {
  const chat = useChat();
  const context = useChatContext();
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);

  async function submit() {
    const text = draft.trim();
    if (text === "" || chat.sending || chat.running) return;
    if (await chat.send(text, context?.jobId ?? null)) setDraft("");
  }

  return (
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <AriaButton className={buttonClass("secondary", "sm")}>Ask the agent</AriaButton>
      <ModalOverlay className={styles.overlay} isDismissable>
        <Modal className={styles.modal}>
          <Dialog className={styles.dialog} aria-label="Ask the agent">
            {({ close }) => (
              <>
                <header className={styles.head}>
                  <Heading slot="title" className="t-section">
                    Ask the agent
                  </Heading>
                  <Button size="sm" variant="quiet" onPress={close}>
                    Close
                  </Button>
                </header>
                {context !== null && (
                  <p className={styles.context}>
                    <span>context: {context.company.trim() === "" ? "this job" : context.company}</span>
                    <Button size="sm" variant="quiet" aria-label="Clear the job context" onPress={() => setChatContext(null)}>
                      Clear
                    </Button>
                  </p>
                )}
                <div className={styles.log} tabIndex={0} role="log" aria-label="Conversation" aria-live="off">
                  {chat.messages.length === 0 && (
                    <p className="muted">Ask about your record, your matches, or why a job was ruled out. Nothing changes in your record unless you confirm it.</p>
                  )}
                  {chat.messages.map((message) =>
                    message.role === "user" ? (
                      <p key={message.id} className={styles.user}>
                        {message.text}
                      </p>
                    ) : (
                      <ReplyView key={message.id} reply={message.reply} />
                    ),
                  )}
                </div>
                <div role="status" className="sr-only">
                  {chat.announcement}
                </div>
                {chat.sendError !== null && <Refusal error={chat.sendError} />}
                <form
                  className={styles.composer}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submit();
                  }}
                >
                  <TextField label="Your message" value={draft} onChange={setDraft} maxLength={MAX_TEXT} />
                  <div className={styles.buttons}>
                    <Button type="submit" variant="primary" isPending={chat.sending} isDisabled={chat.running || draft.trim() === ""}>
                      Send
                    </Button>
                    {chat.running && <Button onPress={() => void chat.stop()}>Stop</Button>}
                  </div>
                </form>
              </>
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </DialogTrigger>
  );
}
