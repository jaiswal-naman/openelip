"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useMe } from "@/features/auth/queries";
import { useAiConsent } from "@/features/consent/useAiConsent";
import { useApi } from "@/lib/api/context";
import { useViewer } from "@/lib/viewer";
import { answerProposal, type RecordDocument, type RecordEvent, proposalsQuery, unconfirmedFactsQuery, useRecordWrite } from "./api";
import { AiOffNotice } from "./AiOffNotice";
import { ChangeLog } from "./ChangeLog";
import { ConfirmedFacts } from "./ConfirmedFacts";
import { ConstraintCards } from "./ConstraintCards";
import { FactReview } from "./FactReview";
import { constraintSubject, documentStatusLabel, entityLabel, parseProblemLabel, sizeLabel, writeProblemText } from "./labels";
import { PreferenceInput } from "./PreferenceInput";
import styles from "./record.module.css";
import { useResume } from "./useResume";

const SIGN_IN = `/sign-in?next=${encodeURIComponent("/app/record")}`;

/**
 * The whole Career Record on one page, every part of it editable, with the change log last (REC-05, FRONTEND_UI 3.8).
 * Sections are native disclosures, so on a phone each can be folded away. Nothing here needs the AI processing consent
 * except reading a resume, and that says why it is off.
 */
export function RecordPage() {
  const router = useRouter();
  const me = useMe();
  const { record, resume } = useResume();
  const unconfirmed = useQuery(unconfirmedFactsQuery(useApi()));

  // A session that ended while the page was open: back to sign in, and then back here.
  useEffect(() => {
    if (me.data === null) router.replace(SIGN_IN);
  }, [me.data, router]);

  const failed = record.error ?? unconfirmed.error;
  if (failed) {
    return (
      <ErrorState
        what="Could not load your record."
        error={failed}
        onRetry={() => {
          void record.refetch();
          void unconfirmed.refetch();
        }}
      />
    );
  }
  if (!record.data || !unconfirmed.data) {
    return (
      <p role="status" className="muted">
        Loading your record.
      </p>
    );
  }
  const view = record.data;
  const toReview = unconfirmed.data;

  return (
    <div className={styles.page}>
      <header className={styles.pageHead}>
        <h1 className="t-page">Your record</h1>
        <p className="muted">Only what you confirm is used. Every change is logged at the bottom of this page.</p>
      </header>

      {view.open_proposals > 0 && <Proposals count={view.open_proposals} />}

      <RecordSection title="Hard constraints (enforced in code)">
        <ConstraintCards mode="record" />
      </RecordSection>

      <RecordSection title="Preferences (used for ranking, not guaranteed)">
        <PreferenceInput />
      </RecordSection>

      {toReview.length > 0 && (
        <RecordSection title={`Facts to review (${toReview.length})`}>
          <FactReview facts={toReview} document={resume.data?.lines ? { id: resume.data.id, lines: resume.data.lines } : undefined} />
        </RecordSection>
      )}

      <RecordSection title="Experience, skills and education">
        <ConfirmedFacts facts={view.facts} />
      </RecordSection>

      <RecordSection title="Documents">
        <Documents documents={view.documents} />
      </RecordSection>

      <RecordSection title="Change log">
        <ChangeLog />
      </RecordSection>
    </div>
  );
}

function RecordSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className={styles.section} open>
      <summary className={styles.sectionHead}>
        <h2 className="t-section">{title}</h2>
      </summary>
      <div className={styles.sectionBody}>{children}</div>
    </details>
  );
}

/** What was proposed by someone other than the person, which changes nothing until they confirm it (CHT-03). */
function Proposals({ count }: { count: number }) {
  const api = useApi();
  const proposals = useQuery(proposalsQuery(api));
  const answer = useRecordWrite(answerProposal);

  return (
    <section className={styles.proposals} aria-label="Proposed changes">
      <h2 className="t-section">
        {count} {count === 1 ? "change" : "changes"} proposed by the agent
      </h2>
      <p className="muted">Nothing changes until you confirm it.</p>
      {proposals.isError ? (
        <ErrorState what="Could not load the proposed changes." error={proposals.error} onRetry={() => void proposals.refetch()} />
      ) : (
        <ul className={styles.facts}>
          {(proposals.data ?? []).map((event) => (
            <li key={event.id} className={styles.confirmed}>
              <p className={styles.statement}>{describeProposal(event)}</p>
              <div className={styles.actions}>
                <Button size="sm" variant="primary" isDisabled={answer.isPending} onPress={() => answer.mutate({ id: event.id, answer: "confirm" })}>
                  Confirm
                </Button>
                <Button size="sm" isDisabled={answer.isPending} onPress={() => answer.mutate({ id: event.id, answer: "decline" })}>
                  Decline
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {answer.isError && (
        <p role="alert" className={styles.problem}>
          {writeProblemText(answer.error)}
        </p>
      )}
    </section>
  );
}

const PROPOSAL_VERBS: Record<string, string> = { create: "Add", update: "Change", remove: "Remove" };

/** "Add excluded company: Acme Co.": what the proposal would do, from the words it carries. */
function describeProposal(event: RecordEvent): string {
  const snapshot = event.after ?? event.before;
  const subject = event.entity === "constraint" ? constraintSubject(String(snapshot?.kind ?? "")) : entityLabel(event.entity).toLowerCase();
  return `${PROPOSAL_VERBS[event.operation] ?? event.operation} ${subject}: ${String(snapshot?.statement ?? "")}`;
}

/** The resumes uploaded, how each was read, and what was removed from it. Reading a new one is the one AI feature on this page. */
function Documents({ documents }: { documents: RecordDocument[] }) {
  const ai = useAiConsent();
  const { locale } = useViewer();
  return (
    <div className={styles.group}>
      {documents.length === 0 ? (
        <EmptyState>No resume uploaded yet.</EmptyState>
      ) : (
        <ul className={styles.facts} aria-label="Uploaded documents">
          {documents.map((document) => (
            <li key={document.id} className={styles.confirmed}>
              <div className={styles.what}>
                <p className={styles.statement}>{document.original_filename ?? "Resume"}</p>
                <p className="t-small muted">
                  {sizeLabel(document.byte_size, locale)} · <LocalTime iso={document.created_at} />
                  {document.hidden_text_chars > 0 && ` · ${document.hidden_text_chars} characters of hidden text removed`}
                  {document.parse_error_code && ` · ${parseProblemLabel(document.parse_error_code)}`}
                </p>
              </div>
              <Pill tone={document.status === "succeeded" ? "ok" : "unk"}>{documentStatusLabel(document.status)}</Pill>
            </li>
          ))}
        </ul>
      )}
      {ai.gate === "ready" ? (
        <div>
          <LinkButton href="/onboarding?step=upload">Upload new</LinkButton>
        </div>
      ) : (
        ai.gate !== "loading" && <AiOffNotice reason={ai.gate} />
      )}
    </div>
  );
}
