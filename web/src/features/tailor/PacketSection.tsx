"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { ErrorState } from "@/components/ui/States";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { PACKET_QUESTION_LABELS } from "@/lib/labels";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api/context";
import { packetFileUrl, packetForJobQuery, type PacketView, useRecordApplied, useRequestPacket } from "./queries";
import styles from "./tailor.module.css";

const YOU_ANSWER = "You answer this";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className={styles.row}>
      <Button
        size="sm"
        aria-label={label}
        onPress={() => {
          navigator.clipboard.writeText(text).then(
            () => setState("copied"),
            () => setState("failed"),
          );
        }}
      >
        Copy
      </Button>
      <span role="status" className="t-small">
        {state === "copied" ? "Copied" : state === "failed" ? "Could not copy. Select the text and copy it." : ""}
      </span>
    </div>
  );
}

/** The packet for this job (APP-02): a short note and an answer to each question, ready to copy. Nothing is sent anywhere. */
export function PacketSection({ jobId, tailoredId }: { jobId: number; tailoredId: string }) {
  const api = useApi();
  const packet = useQuery(packetForJobQuery(api, jobId));
  const request = useRequestPacket(jobId, tailoredId);
  const data = packet.data ?? null;

  return (
    <section aria-labelledby="packet-heading" className={styles.panel}>
      <h2 id="packet-heading" className="t-section">
        Application packet
      </h2>
      <p className="t-small muted">A note and answers drafted from your record, to copy into the application. Questions about pay, notice, start date and work authorisation are left for you. Nothing is sent.</p>
      {packet.isError && <ErrorState what="Could not load the packet." error={packet.error} onRetry={() => void packet.refetch()} />}
      {data === null || data.status === "superseded" ? (
        <div className={styles.row}>
          <Button variant="primary" isPending={request.isPending} onPress={() => request.mutate()}>
            Prepare packet
          </Button>
        </div>
      ) : data.status === "draft" ? (
        <p role="status">Preparing your packet...</p>
      ) : data.status === "failed" ? (
        <div className={styles.row}>
          <p role="alert" className={styles.problem}>The packet could not be prepared.</p>
          <Button isPending={request.isPending} onPress={() => request.mutate()}>
            Try again
          </Button>
        </div>
      ) : (
        <ReadyPacket jobId={jobId} packet={data} />
      )}
      {request.error && (
        <p role="alert" className={styles.problem}>
          {isFailure(request.error, "rate_limited") ? describeFailure(request.error) : `${describeFailure(request.error)} The packet was not prepared.`}
        </p>
      )}
    </section>
  );
}

function ReadyPacket({ jobId, packet }: { jobId: number; packet: PacketView }) {
  const applied = useRecordApplied(jobId, packet.id);
  const exists = isFailure(applied.error, "conflict");
  return (
    <div className={styles.packet}>
      {packet.cover_note !== null && packet.cover_note !== "" && (
        <div className={styles.note}>
          <h3 className="t-card">Short note</h3>
          <p className={styles.answer}>{packet.cover_note}</p>
          <CopyButton text={packet.cover_note} label="Copy the short note" />
        </div>
      )}
      <ul className={styles.answers}>
        {packet.answers.map((answer) => {
          const question = PACKET_QUESTION_LABELS[answer.question_id] ?? answer.question;
          return (
            <li key={answer.question_id}>
              <h3 className="t-card">{question}</h3>
              {answer.status === "candidate_must_answer" ? (
                <p className={styles.mine}>{YOU_ANSWER}</p>
              ) : (
                <>
                  <p className={styles.answer}>{answer.answer}</p>
                  <CopyButton text={answer.answer} label={`Copy the answer to: ${question}`} />
                </>
              )}
            </li>
          );
        })}
      </ul>
      <p className="t-small">
        {packet.removed_sentences === 1
          ? "1 drafted sentence was removed because your record does not support it."
          : `${packet.removed_sentences} drafted sentences were removed because your record does not support them.`}
      </p>
      <div className={styles.row}>
        <a href={packetFileUrl(packet.id)} download className={buttonClass("secondary")}>
          Download packet PDF
        </a>
        {applied.isSuccess ? (
          <p role="status">
            Recorded. See <Link href="/app/campaigns">your applications</Link>.
          </p>
        ) : (
          <Button variant="primary" isPending={applied.isPending} onPress={() => applied.mutate()}>
            Record that I applied
          </Button>
        )}
      </div>
      {exists && (
        <p role="alert" className={styles.problem}>
          You already recorded applying to this job. See <Link href="/app/campaigns">your applications</Link>.
        </p>
      )}
      {applied.error && !exists && (
        <p role="alert" className={styles.problem}>
          {describeFailure(applied.error)} Nothing was recorded.
        </p>
      )}
    </div>
  );
}
