"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Cell, Column, Dialog, Heading, Modal, ModalOverlay, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { Pill } from "@/components/ui/Pill";
import ledger from "@/components/ui/ledger.module.css";
import { VerdictPill } from "@/features/matches/VerdictPill";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { CANDIDATE_INTRODUCTION_LABELS, IMPORTANCE_LABELS, messageFor } from "@/lib/labels";
import {
  type IntroductionDetail,
  introKeys,
  useAcceptIntroduction,
  useCandidateSignals,
  useDeclineIntroduction,
  useIntroduction,
  useSendInterest,
} from "./api";
import styles from "./introductions.module.css";

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const money = (value: number) => value.toLocaleString("en-GB");

/** "INR 30,00,000 to 45,00,000 a year", or the one end that is known; null when the role states no pay. */
export function payText(pay: IntroductionDetail["role"]["pay_range"]): string | null {
  if (pay === null || (pay.min === null && pay.max === null)) return null;
  const range = pay.min !== null && pay.max !== null ? `${money(pay.min)} to ${money(pay.max)}` : pay.min !== null ? `from ${money(pay.min)}` : `up to ${money(pay.max ?? 0)}`;
  return `${pay.currency ?? ""} ${range} a year`.trim();
}

export type SheetOutcome = "accepted" | "declined";

/**
 * The introduction consent sheet (FRONTEND_UI 4.6, HIR-04): exactly what the employer would receive, rendered from the
 * server's frozen `share_preview` (never assembled here), then one step: Accept and share, Not now, or Decline. Nothing is
 * pre-selected, and focus starts on the title so that Accept is never the first thing a keypress can press. Accepting sends
 * the preview's hash and the wording version from the same GET the person read; if the server says the preview changed, the
 * sheet reads it again and accepts nothing. The optional serious-interest signal is sent only after the accept succeeded.
 */
export function IntroductionConsentSheet({
  introductionId,
  isOpen,
  onClose,
  onDone,
}: {
  introductionId: string;
  isOpen: boolean;
  onClose: () => void;
  onDone: (outcome: SheetOutcome) => void;
}) {
  return (
    <ModalOverlay className={styles.overlay} isOpen={isOpen} onOpenChange={(open) => !open && onClose()} isDismissable>
      <Modal className={styles.modal}>
        <Dialog className={styles.dialog} aria-label="Introduction consent">
          <SheetBody introductionId={introductionId} onClose={onClose} onDone={onDone} />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function SheetBody({ introductionId, onClose, onDone }: { introductionId: string; onClose: () => void; onDone: (outcome: SheetOutcome) => void }) {
  const queryClient = useQueryClient();
  const detail = useIntroduction(introductionId);
  const signals = useCandidateSignals();
  const accept = useAcceptIntroduction(introductionId);
  const decline = useDeclineIntroduction(introductionId);
  const interest = useSendInterest();
  const [wantsInterest, setWantsInterest] = useState(false);
  const [confirmingDecline, setConfirmingDecline] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const headingId = useId();
  const loaded = detail.data !== undefined;

  // Focus starts on the title, once the content is there (before that, on the loading line's heading).
  useEffect(() => {
    titleRef.current?.focus();
  }, [loaded]);

  const busy = accept.isPending || decline.isPending || interest.isPending;

  async function onAccept(d: IntroductionDetail) {
    setProblem(null);
    setNotice(null);
    try {
      await accept.mutateAsync({ share_preview_hash: d.share_preview_hash, consent_text_version: d.consent_text_version });
    } catch (error) {
      if (isFailure(error, "preview_changed")) {
        // Nothing was accepted. Read the ask again and let the person look at the new content; the tick is cleared.
        setWantsInterest(false);
        setNotice(messageFor(error));
        await queryClient.invalidateQueries({ queryKey: introKeys.detail(introductionId) });
        return;
      }
      if (isFailure(error, "already_resolved") || (isFailure(error, "conflict") && (error.details.state === "expired" || error.details.state === "closed"))) {
        setGone(goneText(error.details));
        await queryClient.invalidateQueries({ queryKey: introKeys.all });
        return;
      }
      setProblem(describeFailure(error));
      return;
    }
    if (wantsInterest) await interest.mutateAsync(introductionId).catch(() => undefined); // the accepted list offers the signal again
    onDone("accepted");
  }

  async function onDecline() {
    setProblem(null);
    try {
      await decline.mutateAsync();
      onDone("declined");
    } catch (error) {
      if (isFailure(error, "already_resolved") || (isFailure(error, "conflict") && (error.details.state === "expired" || error.details.state === "closed"))) {
        setGone(goneText(error.details));
        await queryClient.invalidateQueries({ queryKey: introKeys.all });
      } else setProblem(describeFailure(error));
    }
  }

  if (detail.isError) {
    return (
      <>
        <Heading slot="title" ref={titleRef} tabIndex={-1} className={`t-section ${styles.title}`}>
          This introduction could not be opened
        </Heading>
        <p role="alert">{describeFailure(detail.error)}</p>
        <div className={styles.buttons}>
          <Button onPress={() => void detail.refetch()}>Retry</Button>
          <Button onPress={onClose}>Close</Button>
        </div>
      </>
    );
  }
  if (detail.data === undefined) {
    return (
      <>
        <Heading slot="title" ref={titleRef} tabIndex={-1} className={`t-section ${styles.title}`}>
          Loading the introduction
        </Heading>
        <p role="status" className="muted">
          Loading what would be shared.
        </p>
      </>
    );
  }

  const d = detail.data;
  const preview = d.share_preview.on_accept;
  const pay = payText(d.role.pay_range);
  const quota = signals.data?.quota;
  const left = quota === undefined ? null : Math.max(0, quota.limit - quota.used);
  const alreadySent = (signals.data?.sent ?? []).some((signal) => signal.introduction_id === d.id);
  const canInterest = left !== null && left > 0 && !alreadySent;
  const facts = preview.facts;

  if (gone !== null) {
    return (
      <>
        <Heading slot="title" ref={titleRef} tabIndex={-1} className={`t-section ${styles.title}`}>
          {d.organization.name}: {d.role.title}
        </Heading>
        <p role="alert" className={styles.notice}>
          {gone}
        </p>
        <div className={styles.buttons}>
          <Button variant="primary" onPress={onClose}>
            Close
          </Button>
        </div>
      </>
    );
  }

  return (
    <>
      <Heading slot="title" id={headingId} ref={titleRef} tabIndex={-1} className={`t-section ${styles.title}`}>
        Share with {d.organization.name} for {d.role.title}?
      </Heading>
      {notice !== null && (
        <p role="status" className={styles.notice}>
          {notice}
        </p>
      )}

      <dl className={styles.spec}>
        <div>
          <dt className="t-label">Shared with</dt>
          <dd>
            {d.organization.name} {d.organization.is_synthetic && <Pill tone="unk">{CANDIDATE_INTRODUCTION_LABELS.demo}</Pill>}
          </dd>
        </div>
        <div>
          <dt className="t-label">For the role</dt>
          <dd>{d.role.title}</dd>
        </div>
        {pay !== null && (
          <div>
            <dt className="t-label">Their pay range</dt>
            <dd>{pay}</dd>
          </div>
        )}
      </dl>

      <p className={styles.words}>{d.terms}</p>

      <section className={styles.block} aria-labelledby={`${headingId}-receive`}>
        <h3 id={`${headingId}-receive`} className="t-label">
          What they will receive (exactly this)
        </h3>
        <p className={styles.words}>
          <strong>{preview.name ?? "Your name is not set"}</strong>
          {preview.headline ? `: ${preview.headline}` : ""}
        </p>
        <Table aria-label="Requirements and your evidence that would be shared" className={ledger.table}>
          <TableHeader>
            <Column isRowHeader>Requirement</Column>
            <Column>Verdict</Column>
            <Column>Your words that would be shared</Column>
          </TableHeader>
          <TableBody>
            {preview.evidence.map((row, index) => (
              <Row key={index} id={String(index)} textValue={row.requirement}>
                <Cell className={styles.words}>
                  {row.requirement} <span className={styles.muted}>({IMPORTANCE_LABELS[row.importance]})</span>
                </Cell>
                <Cell data-label="Verdict">
                  <VerdictPill verdict={row.verdict} />
                </Cell>
                <Cell className={styles.words} data-label="Your words that would be shared">
                  {row.record_quotes.length === 0 ? "No quote shared" : row.record_quotes.map((quote) => `“${quote}”`).join(" ")}
                </Cell>
              </Row>
            ))}
          </TableBody>
        </Table>
        {facts.length > 0 && (
          <div>
            <h4 className="t-label">Confirmed roles and education ({facts.length})</h4>
            <ul className={styles.facts}>
              {facts.map((fact, index) => (
                <li key={index}>{fact.statement}</li>
              ))}
            </ul>
          </div>
        )}
        <p>Your email is shared only if they answer yes: then they receive {d.share_preview.on_employer_yes.email}.</p>
      </section>

      <section className={styles.block} aria-labelledby={`${headingId}-not`}>
        <h3 id={`${headingId}-not`} className="t-label">
          What they will not receive
        </h3>
        <p>Files, current pay, other contact details.</p>
      </section>

      <div className={styles.actions}>
        <label className={styles.interest}>
          <input type="checkbox" checked={wantsInterest} disabled={!canInterest || busy} onChange={(event) => setWantsInterest(event.target.checked)} />
          <span>Send serious interest{left !== null ? ` (${left} of ${quota?.limit} left this week)` : ""}</span>
        </label>

        {problem !== null && (
          <p role="alert" className={styles.problem}>
            {problem} Nothing was shared.
          </p>
        )}

        {confirmingDecline ? (
          <div className={styles.block} role="group" aria-label="Confirm decline">
            <p>Decline this introduction? The employer is not told, and it cannot be undone.</p>
            <div className={styles.buttons}>
              <Button variant="primary" isPending={decline.isPending} onPress={() => void onDecline()}>
                Yes, decline
              </Button>
              <Button onPress={() => setConfirmingDecline(false)}>Keep it open</Button>
            </div>
          </div>
        ) : (
          <div className={styles.buttons}>
            <Button variant="primary" isPending={accept.isPending || interest.isPending} onPress={() => !busy && void onAccept(d)}>
              Accept and share
            </Button>
            <Button onPress={onClose}>Not now</Button>
            <Button onPress={() => setConfirmingDecline(true)}>Decline</Button>
          </div>
        )}
        <p className={styles.fine}>Accepting cannot be undone. No reply by {day(d.expires_at)} counts as no.</p>
      </div>
    </>
  );
}

function goneText(details: Record<string, unknown>): string {
  if (details.state === "expired") return "This ask has expired, so it can no longer be accepted.";
  if (details.state === "closed") return "The employer closed this role, so the ask was withdrawn.";
  return details.resolution === "declined" ? "You already declined this ask." : "You already answered this ask.";
}
