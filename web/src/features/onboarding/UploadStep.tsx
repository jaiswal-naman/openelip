"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, useState } from "react";
import { FileTrigger } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { ErrorState, ProgressLine } from "@/components/ui/States";
import { useAiConsent } from "@/features/consent/useAiConsent";
import { authKeys } from "@/features/auth/queries";
import { documentQuery, uploadDocument } from "@/features/record/api";
import { AiOffNotice, type AiOffReason } from "@/features/record/AiOffNotice";
import { HiddenTextPanel } from "@/features/record/HiddenTextPanel";
import { useApi } from "@/lib/api/context";
import { describeFailure, problemOf } from "@/lib/api/problems";
import styles from "./onboarding.module.css";

const MAX_BYTES = 10 * 1024 * 1024;
const MAX_LABEL = "10 MB";
const ACCEPT = [".pdf", ".docx", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"];
const POLL_MS = 2000; // the API asks for 2 seconds between polls (Retry-After on an unfinished document)

/** What the browser can check before sending. The API checks the real bytes, so this is only to save a wait. */
function precheck(file: File): string | null {
  if (!/\.(pdf|docx)$/i.test(file.name)) return "Choose a PDF or a DOCX file.";
  if (file.size === 0) return "That file is empty. Choose a PDF or a DOCX file.";
  if (file.size > MAX_BYTES) return `That file is over ${MAX_LABEL}. Choose a smaller PDF or DOCX.`;
  return null;
}

type Props = {
  /** The resume being read, from the address, so a reload keeps polling instead of uploading again. */
  documentId: string | null;
  /** Called with the id of the document just queued, or null to start again with another file. */
  onDocumentChange: (documentId: string | null) => void;
  /** The facts are ready to review. */
  onDone: (documentId: string) => void;
  /** Carry on without a resume. */
  onSkip: () => void;
  pollMs?: number;
};

/**
 * Step 2: upload a PDF or DOCX and watch it being read (FRONTEND_UI 3.2). The API reports `queued` or `running` while the
 * worker reads the text and extracts the facts, and that is all it knows, so that is all this says: no percentage, no
 * stage it cannot see. Needs the AI processing consent and a configured model; when either is missing it says which.
 */
export function UploadStep({ documentId, onDocumentChange, onDone, onSkip, pollMs = POLL_MS }: Props) {
  const api = useApi();
  const queryClient = useQueryClient();
  const ai = useAiConsent();
  const [refused, setRefused] = useState<string | null>(null);
  const [off, setOff] = useState<AiOffReason | null>(null);

  const upload = useMutation({
    mutationFn: (file: File) => uploadDocument(api, file),
    onSuccess: (queued) => onDocumentChange(queued.id),
    onError: (error) => {
      const problem = problemOf(error);
      if (problem.kind === "payload_too_large") setRefused(`That file is over ${MAX_LABEL}. Choose a smaller PDF or DOCX.`);
      else if (problem.kind === "unsupported_media_type") setRefused("This is not a PDF or a DOCX. Choose a PDF or a DOCX file.");
      else if (problem.kind === "ai_disabled") setOff("ai_disabled");
      else if (problem.kind === "consent_required") {
        setOff(problem.reason === "providers_changed" ? "providers_changed" : "consent_required");
        void queryClient.invalidateQueries({ queryKey: authKeys.me }); // the agreement is not what this page thought
      } else setRefused(describeFailure(error));
    },
  });

  const reading = useQuery({
    ...documentQuery(api, documentId ?? ""),
    enabled: documentId !== null,
    // keep asking until it is read or has failed; a call that itself failed is shown with Retry, not hammered every few seconds
    refetchInterval: (query) => (query.state.status === "error" || ["succeeded", "failed"].includes(query.state.data?.status ?? "") ? false : pollMs),
  });

  function pick(file: File | undefined) {
    if (!file) return;
    const problem = precheck(file);
    setRefused(problem);
    setOff(null);
    if (problem === null) upload.mutate(file);
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    pick(event.dataTransfer.files[0]);
  }

  const gateReason: AiOffReason | null = off ?? (ai.gate === "ready" || ai.gate === "loading" ? null : ai.gate);
  if (gateReason !== null && documentId === null) {
    return (
      <div className={styles.stack}>
        <AiOffNotice reason={gateReason} reviewHref="/onboarding?step=consent" />
        <div>
          <Button onPress={onSkip}>Continue without a resume</Button>
        </div>
      </div>
    );
  }

  const status = reading.data?.status;
  return (
    <div className={styles.stack}>
      {documentId === null ? (
        <>
          <div className={styles.drop} onDragOver={(event) => event.preventDefault()} onDrop={drop}>
            <FileTrigger acceptedFileTypes={ACCEPT} onSelect={(files) => pick(files?.[0])}>
              <Button variant="primary" isPending={upload.isPending}>
                Choose a PDF or DOCX, up to {MAX_LABEL}
              </Button>
            </FileTrigger>
            <p className="muted">
              or drop the file here. Text a person cannot see on the page is removed, and you are shown what it said.
            </p>
          </div>
          <p role="status" className="sr-only">
            {upload.isPending ? "Uploading your resume." : ""}
          </p>
          {refused && (
            <p role="alert" className={styles.problem}>
              {refused}
            </p>
          )}
          <div>
            <Button onPress={onSkip}>
              Skip this step
            </Button>
          </div>
        </>
      ) : reading.isError ? (
        <ErrorState what="Could not check how your resume is going." error={reading.error} onRetry={() => void reading.refetch()} />
      ) : status === undefined || status === "queued" || status === "running" ? (
        <div className={styles.progress}>
          <ProgressLine active />
          <p role="status">
            {status === "running" ? "Reading text and extracting facts." : status === "queued" ? "Waiting to start. Your resume is queued." : "Checking how your resume is going."}
          </p>
          <p className="muted t-small">This usually takes under a minute. You can stay on this page.</p>
        </div>
      ) : status === "failed" ? (
        <div className={styles.stack}>
          <p role="alert" className={styles.problem}>
            {reading.data?.message ?? "We could not read this file. Try another one."}
          </p>
          <div className={styles.row}>
            <Button variant="primary" onPress={() => onDocumentChange(null)}>
              Choose another file
            </Button>
            <Button onPress={onSkip}>Skip this step</Button>
          </div>
        </div>
      ) : (
        <div className={styles.stack}>
          <p role="status">
            Read {reading.data?.original_filename ?? "your resume"}: {reading.data ? reading.data.facts.kept + reading.data.facts.unverified : 0} facts found,{" "}
            {reading.data?.facts.to_review ?? 0} to review.
          </p>
          {reading.data?.parse_error_code === "partial_extraction" && reading.data.message && <p>{reading.data.message}</p>}
          {reading.data && <HiddenTextPanel chars={reading.data.hidden_text_chars} flags={reading.data.hidden_text_flags} />}
          <div>
            <Button variant="primary" onPress={() => documentId && onDone(documentId)}>
              Review the facts
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
