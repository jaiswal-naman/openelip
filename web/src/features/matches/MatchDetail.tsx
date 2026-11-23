"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { ExternalLinkIcon } from "@/components/icons/icons";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { ChatContext } from "@/features/chat/ChatContext";
import { ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { arrangementsText, locationText, payText, safeHttpUrl, sourceLabel, unplacedWords } from "@/features/jobs/jobs";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { MATCH_REASON_LABELS } from "@/lib/labels";
import { useViewer } from "@/lib/viewer";
import { citationResolves } from "@/ui/patterns/HighlightedText";
import { ConstraintChecks } from "./ConstraintChecks";
import { DismissDialog } from "./DismissDialog";
import { EvidenceTable } from "./EvidenceTable";
import { highlightAnnouncement, lineNumberAt } from "./lines";
import { type EvidenceItem, type MatchData, resumeTextQuery } from "./queries";
import styles from "./match.module.css";
import { TextPane } from "./TextPane";
import { useFeedback } from "./useFeedback";

const REQUIREMENT_ID = /^\d{1,15}$/; // a requirement id is a bigint: anything else in ?req= is not one

/** The requirement the address names, if this match has it. An id that matches none selects nothing. */
function requirementFromAddress(raw: string | null, evidence: EvidenceItem[]): number | null {
  if (raw === null || !REQUIREMENT_ID.test(raw)) return null;
  const id = Number(raw);
  return evidence.some((item) => item.requirement.id === id) ? id : null;
}

/**
 * One match: the job, every requirement with its verdict and the record evidence for it, and the two texts the selected
 * row points into (MAT-03, MAT-04). Selecting a row marks the exact job sentence and the cited resume words, scrolls both
 * panes to them, announces it, and keeps the row in `?req=` so a reload or a shared link lands on the same highlight. Posting
 * and resume text are shown as text, never as markup (threat T-04-31). Save and dismiss are MAT-08; a dismissal only ever
 * proposes a record change.
 */
export function MatchDetail({ match }: { match: MatchData }) {
  const api = useApi();
  const viewer = useViewer();
  const search = useSearchParams();
  const feedback = useFeedback(match.decision_id, match.feedback);
  const [selected, setSelected] = useState<number | null>(() => requirementFromAddress(search.get("req"), match.evidence));
  const { job } = match;

  const active = match.evidence.find((item) => item.requirement.id === selected) ?? null;

  // The job side: marked only while the words are still at the stored span of the posting as it reads now.
  const jobPoints = useMemo(() => Array.from(match.description), [match.description]);
  const jobCitation = active?.requirement.job_citation ?? null;
  const jobResolves = active !== null && jobCitation !== null && citationResolves(jobPoints, jobCitation.span, active.requirement.text);

  // The record side: the first cited fact that lives in a document. A fact the person wrote has no document and no line.
  const cited = active?.record_citations.find((citation) => citation.source !== null) ?? null;
  const source = cited?.source ?? null;
  const addedByYou = active !== null && active.record_citations.length > 0 && cited === null; // cited, but not from any document
  const resume = useQuery({ ...resumeTextQuery(api, source?.document_id ?? ""), enabled: source !== null });
  const resumeText = resume.data?.text ?? null;
  const resumePoints = useMemo(() => (resumeText === null ? null : Array.from(resumeText)), [resumeText]);
  const resumeResolves = source !== null && cited !== null && resumePoints !== null && citationResolves(resumePoints, source.span, cited.quote);
  const resumeLine = resumeResolves && resumeText !== null && source !== null ? lineNumberAt(resumeText, source.span.start) : null;

  // One sentence for both panes: the job pane says it when the job is marked, else the resume pane does.
  const sentence = highlightAnnouncement(jobResolves, resumeLine);

  function select(requirementId: number | null) {
    setSelected(requirementId);
    // The address is state a person may reload or share (FRONTEND_UI 9.4). It is written with the history API, which
    // Next.js folds into its router: no request to the server, no scroll to the top, and the focus stays on the row.
    const url = new URL(window.location.href);
    if (requirementId === null) url.searchParams.delete("req");
    else url.searchParams.set("req", String(requirementId));
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }

  const closed = job.status === "closed";
  const applyUrl = safeHttpUrl(job.apply_url);
  const words = unplacedWords(job);
  const changed = match.reason_codes.includes("material_change");

  return (
    <article className={styles.detail}>
      <ChatContext jobId={job.id} company={job.company_name} />
      <header className={styles.head}>
        <h1 className={`t-page ${styles.title}`}>{job.title}</h1>
        <p className={styles.company}>{job.company_name.trim() === "" ? "Company not stated" : job.company_name}</p>

        {closed && (
          <p className={styles.banner}>
            <span className={`t-label ${styles.bannerLabel}`}>Closed</span>
            <span>This job has closed. It is kept here for your records.</span>
          </p>
        )}
        {changed && (
          <p className={styles.banner}>
            <span className={`t-label ${styles.bannerLabel}`}>{MATCH_REASON_LABELS.material_change}</span>
            <span>{match.change_note ?? "The posting changed."}</span>
          </p>
        )}

        <ul className={styles.facts}>
          <li>{locationText(job)}</li>
          {words !== null && <li>Posting says: {words}</li>}
          <li>{arrangementsText(job.work_arrangements)}</li>
          <li>Pay: {payText(job.pay, viewer.locale)}</li>
          <li>Source: {sourceLabel(job.source.ats)}</li>
          <li>
            {closed ? "Last verified open" : "Verified open"} <LocalTime iso={job.last_verified_at} />
          </li>
        </ul>
        <p className={styles.summary}>{match.summary}</p>

        <div className={styles.actions}>
          {!closed && applyUrl !== null && (
            <a
              href={applyUrl}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className={buttonClass("primary", "md")}
              aria-label={`View posting for ${job.title} (opens in a new tab)`}
            >
              View posting
              <ExternalLinkIcon />
            </a>
          )}
          <LinkButton href={`/app/matches/${match.decision_id}/tailor`}>Tailor resume</LinkButton>
          <Button isPending={feedback.isPending} onPress={feedback.toggleSave}>
            {feedback.isSaved ? "Unsave" : "Save"}
          </Button>
          <DismissDialog onDismiss={feedback.dismiss} isPending={feedback.isPending} />
          {feedback.isSaved && <Pill tone="ok">Saved</Pill>}
        </div>
        {feedback.isDismissed && (
          <p role="status" className={styles.notice}>
            You dismissed this job. Save it to take that back.
          </p>
        )}
        {feedback.hasProposal && (
          <p role="status" className={styles.notice}>
            We suggested a change to your record. It changes nothing until you confirm it: see <Link href="/app/record">your record</Link>.
          </p>
        )}
        {feedback.error !== null && (
          <p role="alert" className={styles.problem}>
            {describeFailure(feedback.error)} Your choice was not saved.
          </p>
        )}
      </header>

      <section aria-labelledby="evidence-heading" className={styles.panel}>
        <h2 id="evidence-heading" className="t-section">
          Requirements and your evidence
        </h2>
        {match.evidence.length === 0 ? (
          <p>No requirements were recorded for this match.</p>
        ) : (
          <>
            <p className="t-small muted">Select a requirement, with a click, Enter or Space, to see where it is in the posting and in your resume.</p>
            <EvidenceTable evidence={match.evidence} selected={active === null ? null : active.requirement.id} onSelect={select} />
          </>
        )}
      </section>

      <section aria-labelledby="texts-heading" className={styles.panel}>
        <h2 id="texts-heading" className="t-section">
          Job text and your resume
        </h2>
        <div className={styles.panes}>
          <TextPane
            title="Job text"
            text={match.description.trim() === "" ? null : match.description}
            span={jobResolves && jobCitation !== null ? jobCitation.span : null}
            announcement={jobResolves ? sentence : ""}
            note={active !== null && !jobResolves && jobCitation !== null && <>This posting has changed since: &ldquo;{jobCitation.quote}&rdquo;</>}
            empty="The posting text is not available."
          />
          <TextPane
            title="Your resume"
            text={resumeText}
            span={resumeResolves && source !== null ? source.span : null}
            announcement={jobResolves ? "" : sentence}
            note={resumeText !== null && !resumeResolves && "Withheld: citation did not resolve"}
            empty={resumeEmpty(active, addedByYou, resume.error, () => void resume.refetch())}
          />
        </div>
      </section>

      <section aria-labelledby="checks-heading" className={styles.panel}>
        <h2 id="checks-heading" className="t-section">
          Your hard constraints
        </h2>
        <ConstraintChecks checks={match.constraint_checks} />
      </section>
    </article>
  );
}

/** What stands in for the resume text: why there is none to show for the row selected, or that it is still coming. */
function resumeEmpty(active: EvidenceItem | null, addedByYou: boolean, failure: unknown, retry: () => void) {
  if (active === null) return <p>Select a requirement to see where your resume states it.</p>;
  if (active.verdict === "no_evidence") return <p>No evidence in your record for this requirement.</p>;
  if (addedByYou) return <p>This evidence was added by you, so it has no line in a resume.</p>;
  if (failure !== null) return <ErrorState what="Could not load your resume text." error={failure} onRetry={retry} />;
  return (
    <p role="status" className="muted">
      Loading your resume text.
    </p>
  );
}
