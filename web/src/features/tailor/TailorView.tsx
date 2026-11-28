"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { AiOffNotice, EmptyState, ErrorState } from "@/components/ui/States";
import { type TitleCell, TitleBlock } from "@/components/ui/TitleBlock";
import { useApi } from "@/lib/api/context";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { DISCARD_REASON_LABELS } from "@/lib/labels";
import { EditCard } from "./EditCard";
import { PacketSection } from "./PacketSection";
import { fileUrl, type TailoredView, tailoredForJobQuery, useDecide, useRequestTailoring } from "./queries";
import styles from "./tailor.module.css";

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/** The title block's cells, counted from the edits the page holds: what needs a decision stays in red, never a guess. */
function editCounts(view: TailoredView): TitleCell[] {
  const undecided = view.edits.filter((edit) => edit.decision === "pending").length;
  const approved = view.edits.filter((edit) => edit.decision === "approved").length;
  return [
    { label: "Edits proposed", value: view.edits.length },
    { label: "Undecided", value: undecided, tone: undecided > 0 ? "redline" : "muted" },
    { label: "Approved edits", value: approved, tone: approved > 0 ? "ok" : "muted" },
    { label: "Discarded", value: view.discarded_count, tone: "muted" },
  ];
}

/**
 * The tailoring page for one job: the page header (when the page names the job) over the body. The body and the block read
 * the same query, so one request serves both.
 */
export function TailorView({ jobId, header }: { jobId: number; header?: { title: string; company: string } }) {
  const api = useApi();
  const tailored = useQuery(tailoredForJobQuery(api, jobId));
  const ready = tailored.data?.status === "ready" ? tailored.data : null;
  return (
    <div className={styles.sheet}>
      {header && (
        <TitleBlock
          title={`Tailor your resume: ${header.title}`}
          lines={<p>{header.company.trim() === "" ? "Company not stated" : header.company}</p>}
          cells={ready === null ? [] : editCounts(ready)}
        />
      )}
      <TailorBody jobId={jobId} />
    </div>
  );
}

/** The tailoring page for one job (TLR-01 to TLR-03): request, review every verified edit, decide, export, then the packet. */
function TailorBody({ jobId }: { jobId: number }) {
  const api = useApi();
  const tailored = useQuery(tailoredForJobQuery(api, jobId));
  const request = useRequestTailoring(jobId);

  if (tailored.isPending) return <p role="status">Loading...</p>;
  if (tailored.isError) return <ErrorState what="Could not load your tailored resume." error={tailored.error} onRetry={() => void tailored.refetch()} />;
  const data = tailored.data;

  const requestProblem = request.error;
  const notice = isFailure(requestProblem, "ai_disabled") ? (
    <AiOffNotice />
  ) : isFailure(requestProblem, "consent_required") ? (
    <p role="alert" className={styles.problem}>
      Agree to AI processing first. <a href="/app/settings?section=privacy">Review and agree</a>.
    </p>
  ) : requestProblem ? (
    <p role="alert" className={styles.problem}>
      {isFailure(requestProblem, "conflict") && requestProblem.details.state === "no_resume"
        ? "Upload a resume first: tailoring needs facts taken from it."
        : describeFailure(requestProblem)}{" "}
      No resume was tailored.
    </p>
  ) : null;

  if (data === null) {
    return (
      <div className={styles.view}>
        <p>Tailoring reorders and trims your resume to fit this job, using only what your record already says. You approve every edit.</p>
        <div className={styles.row}>
          <Button variant="primary" isPending={request.isPending} onPress={() => request.mutate()}>
            Tailor my resume
          </Button>
        </div>
        {notice}
      </div>
    );
  }
  if (data.status === "generating") return <p role="status">Tailoring your resume for {data.job_title}...</p>;
  if (data.status === "failed") {
    return (
      <div className={styles.view}>
        <p role="alert" className={styles.problem}>
          Your resume could not be tailored this time.
        </p>
        <div className={styles.row}>
          <Button variant="primary" isPending={request.isPending} onPress={() => request.mutate()}>
            Try again
          </Button>
        </div>
        {notice}
      </div>
    );
  }
  return <ReadyView jobId={jobId} view={data} />;
}

function ReadyView({ jobId, view }: { jobId: number; view: TailoredView }) {
  const decide = useDecide(jobId, view.id);
  const [pdfProblem, setPdfProblem] = useState<"unsupported" | "failed" | null>(null);
  const reasons = Object.entries(view.discarded_by_reason).filter(([, count]) => count > 0);
  const approved = view.edits.filter((edit) => edit.decision === "approved").length;
  const undecided = view.edits.filter((edit) => edit.decision === "pending").length;
  const everyEditDiscarded = view.edits.length === 0 && view.discarded_count > 0;

  async function downloadPdf() {
    setPdfProblem(null);
    try {
      const response = await fetch(fileUrl(view.id, "pdf"), { credentials: "same-origin" });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const text = JSON.stringify(body);
        setPdfProblem(response.status === 422 && text.includes("pdf_unsupported_characters") ? "unsupported" : "failed");
        return;
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `tailored-resume-job-${jobId}.pdf`;
      link.click();
      URL.revokeObjectURL(url);
    } catch {
      setPdfProblem("failed");
    }
  }

  return (
    <div className={styles.view}>
      <section aria-labelledby="edits-heading" className={styles.panel}>
        <h2 id="edits-heading" className="t-section">
          {plural(view.edits.length, "edit", "edits")} proposed. {view.discarded_count} discarded before you saw them
        </h2>
        {reasons.length > 0 && (
          <details className={styles.why}>
            <summary>Why were {view.discarded_count === 1 ? "it" : "they"} discarded?</summary>
            <ul>
              {reasons.map(([code, count]) => (
                <li key={code}>
                  {DISCARD_REASON_LABELS[code] ?? code}: {count}
                </li>
              ))}
            </ul>
          </details>
        )}
        {everyEditDiscarded ? (
          <EmptyState>Every proposed edit added something your record does not contain, so none are shown. Your resume is unchanged.</EmptyState>
        ) : view.edits.length === 0 ? (
          <EmptyState>No edits were proposed. Your resume is unchanged.</EmptyState>
        ) : (
          <ul className={styles.edits}>
            {view.edits.map((edit) => (
              <EditCard
                key={edit.id}
                edit={edit}
                error={decide.variables?.edit_id === edit.id ? decide.error : null}
                onDecide={(decision) => decide.mutate({ edit_id: edit.id, decision })}
              />
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="export-heading" className={styles.panel}>
        <h2 id="export-heading" className="t-section">
          Export your resume
        </h2>
        <p>
          {plural(approved, "approved edit is", "approved edits are")} used. {undecided === 0 ? "None are" : `${undecided} undecided ${undecided === 1 ? "is" : "are"}`} left out.
        </p>
        <div className={styles.row}>
          <Button onPress={() => void downloadPdf()}>Export PDF</Button>
          <a href={fileUrl(view.id, "docx")} download className={buttonClass("secondary")}>
            Export DOCX
          </a>
        </div>
        {pdfProblem === "unsupported" && (
          <p role="alert" className={styles.problem}>
            This resume has characters the PDF cannot hold. Export the DOCX instead.
          </p>
        )}
        {pdfProblem === "failed" && (
          <p role="alert" className={styles.problem}>
            The PDF could not be made. Try again, or export the DOCX.
          </p>
        )}
      </section>

      <PacketSection jobId={jobId} tailoredId={view.id} />
    </div>
  );
}
