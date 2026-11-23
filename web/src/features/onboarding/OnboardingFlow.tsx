"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { LocalTime } from "@/components/ui/Viewer";
import { useMe } from "@/features/auth/queries";
import { AiConsentSheet } from "@/features/consent/AiConsentSheet";
import { useAiConsent } from "@/features/consent/useAiConsent";
import { unconfirmedFactsQuery } from "@/features/record/api";
import { AiOffNotice } from "@/features/record/AiOffNotice";
import { ConstraintCards } from "@/features/record/ConstraintCards";
import { FactReview } from "@/features/record/FactReview";
import { HiddenTextPanel } from "@/features/record/HiddenTextPanel";
import { PreferenceInput } from "@/features/record/PreferenceInput";
import { useResume } from "@/features/record/useResume";
import { useApi } from "@/lib/api/context";
import styles from "./onboarding.module.css";
import { MatchesStep } from "./MatchesStep";
import { UploadStep } from "./UploadStep";

// The six steps of FRONTEND_UI 3.2. The last, time zone and first matches, is step 6.
const STEPS = ["consent", "upload", "facts", "constraints", "preferences", "matches"] as const;
type Step = (typeof STEPS)[number];

const TITLES: Record<Step, string> = {
  consent: "AI processing",
  upload: "Upload your resume",
  facts: "Check what we read",
  constraints: "Your hard constraints",
  preferences: "Your preferences",
  matches: "Time zone and first matches",
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SIGN_IN = `/sign-in?next=${encodeURIComponent("/onboarding")}`;
// Where a person goes when they skip the resume: past the upload and the fact review, to the intake.
const INTAKE: Step = "constraints";

function parseStep(raw: string | null): Step {
  return (STEPS as readonly string[]).includes(raw ?? "") ? (raw as Step) : "consent";
}

/**
 * Onboarding (FRONTEND_UI 3.2): consent to AI processing, upload a resume, confirm the facts read from it, then the
 * intake, and last the time zone the morning brief is read in and the first matches. The step is in the address (?step=),
 * and so is the resume being read (&doc=), so a reload or the back button lands where the person was. Every step works
 * without AI: a person who says "Not now" skips the two that need it.
 */
export function OnboardingFlow() {
  const router = useRouter();
  const params = useSearchParams();
  const me = useMe();
  const step = parseStep(params.get("step"));
  const rawDoc = params.get("doc");
  const documentId = rawDoc !== null && UUID.test(rawDoc) ? rawDoc : null;

  // A session that ended while the page was open: back to sign in, and then back here.
  useEffect(() => {
    if (me.data === null) router.replace(SIGN_IN);
  }, [me.data, router]);

  // The heading takes focus when the step changes, so a screen reader starts at the top of the new step. The title block
  // draws the heading, so it is found from the sheet's own element.
  const sheet = useRef<HTMLDivElement>(null);
  const shown = useRef(step);
  useEffect(() => {
    const heading = sheet.current?.querySelector("h1");
    if (!heading) return;
    heading.tabIndex = -1;
    if (shown.current === step) return;
    shown.current = step;
    heading.focus();
  }, [step]);

  const address = (next: Step, doc: string | null) => `/onboarding?step=${next}${doc ? `&doc=${doc}` : ""}`;
  const go = (next: Step, doc: string | null = documentId) => router.push(address(next, doc));
  const index = STEPS.indexOf(step);
  const after = STEPS[index + 1];
  const before = index > 0 ? STEPS[index - 1] : null;
  // The first two steps carry their own buttons (Agree, Choose a file); from the facts on, one Continue moves along.
  const hasNext = step === "facts" || step === "constraints" || step === "preferences";

  return (
    <div className={styles.flow} ref={sheet}>
      <TitleBlock
        title={TITLES[step]}
        lines={<p>Setting up your career record.</p>}
        cells={[{ label: "Progress", value: `Step ${index + 1} of ${STEPS.length}` }, ...(after ? [{ label: "Next", value: TITLES[after] }] : [])]}
      />

      <div className={styles.body}>
        {step === "consent" && <ConsentStep onContinue={() => go("upload")} onSkip={() => go(INTAKE)} />}
        {step === "upload" && (
          <UploadStep
            documentId={documentId}
            onDocumentChange={(id) => router.replace(address("upload", id))}
            onDone={(id) => go("facts", id)}
            onSkip={() => go(INTAKE)}
          />
        )}
        {step === "facts" && <FactsStep documentId={documentId} onUpload={() => go("upload")} />}
        {step === "constraints" && (
          <>
            <SkippedNote />
            <p className={styles.lead}>
              A hard constraint is a rule, enforced in code: a job that breaks it is never shown to you. Each card reads your answer
              back before it is saved. You can skip any of them; a skipped card is not enforced.
            </p>
            <ConstraintCards mode="intake" />
          </>
        )}
        {step === "preferences" && (
          <>
            <p className={styles.lead}>
              Preferences are softer. They are used to rank jobs, never to hide them. Anything we cannot check in job data is a
              preference, not a guarantee.
            </p>
            <PreferenceInput />
          </>
        )}
        {step === "matches" && <MatchesStep />}
      </div>

      {(before !== null || hasNext) && (
        <div className={styles.bar}>
          {before !== null ? (
            <Button onPress={() => go(before)}>
              Back
            </Button>
          ) : (
            <span />
          )}
          {hasNext && (
            <Button variant="primary" onPress={() => go(after)}>
              {step === "preferences" ? "Finish" : "Continue"}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** Said at the start of the intake to someone who skipped the resume: why it was not read, and where facts can be typed. */
function SkippedNote() {
  const ai = useAiConsent();
  if (ai.gate === "ready" || ai.gate === "loading") return null;
  return (
    <p className={styles.note}>
      {ai.gate === "ai_disabled"
        ? "AI features are off on this installation, so your resume cannot be read automatically."
        : "Your resume cannot be read automatically without AI processing, so the upload and the fact review were skipped."}{" "}
      You can type your facts yourself on <Link href="/app/record">your record page</Link>.
    </p>
  );
}

/** Step 1. The consent sheet, or what stands in its place when there is nothing to agree to or it is already agreed. */
function ConsentStep({ onContinue, onSkip }: { onContinue: () => void; onSkip: () => void }) {
  const ai = useAiConsent();
  if (ai.gate === "loading") {
    return ai.error ? (
      <ErrorState what="Could not load the AI providers." error={ai.error} onRetry={() => void ai.refetch()} />
    ) : (
      <p role="status" className="muted">
        Loading the list of AI providers.
      </p>
    );
  }
  if (ai.gate === "ai_disabled") {
    return (
      <>
        <AiOffNotice reason="ai_disabled" />
        <div>
          <Button variant="primary" onPress={onSkip}>
            Continue without AI
          </Button>
        </div>
      </>
    );
  }
  if (ai.gate === "ready") {
    return (
      <>
        <p>
          You agreed to AI processing{ai.consent?.granted_at && (
            <>
              {" "}
              on <LocalTime iso={ai.consent.granted_at} />
            </>
          )}
          . You can withdraw this in Settings at any time.
        </p>
        <div>
          <Button variant="primary" onPress={onContinue}>
            Continue
          </Button>
        </div>
      </>
    );
  }
  return <AiConsentSheet onAgreed={onContinue} onNotNow={onSkip} />;
}

/**
 * Step 3. The facts read from the resume, each with the words it came from. Only the facts the person confirms are used
 * anywhere (REC-02), and anything the file hid is listed (REC-06). Which resume: the one in the address, else the newest
 * that was read.
 */
function FactsStep({ documentId, onUpload }: { documentId: string | null; onUpload: () => void }) {
  const api = useApi();
  const ai = useAiConsent();
  const { record, latest, resume } = useResume(documentId);
  const unconfirmed = useQuery(unconfirmedFactsQuery(api));

  const failed = record.error ?? unconfirmed.error ?? resume.error;
  if (failed) {
    return (
      <ErrorState
        what="Could not load the facts."
        error={failed}
        onRetry={() => {
          void record.refetch();
          void unconfirmed.refetch();
          void resume.refetch();
        }}
      />
    );
  }
  if (!record.data || !unconfirmed.data || (latest !== null && !resume.data)) {
    return (
      <p role="status" className="muted">
        Loading the facts.
      </p>
    );
  }

  const facts = [...unconfirmed.data, ...record.data.facts];
  if (latest === null && facts.length === 0) {
    return ai.gate === "ready" || ai.gate === "loading" ? (
      <EmptyState action={<Button onPress={onUpload}>Upload a resume</Button>}>You have not uploaded a resume yet.</EmptyState>
    ) : (
      <>
        <AiOffNotice reason={ai.gate} reviewHref="/onboarding?step=consent" />
        <p>Your resume cannot be read automatically, so there is nothing to check here. You can type your facts on your record page.</p>
      </>
    );
  }
  return (
    <>
      {resume.data && <HiddenTextPanel chars={resume.data.hidden_text_chars} flags={resume.data.hidden_text_flags} />}
      <FactReview facts={facts} document={resume.data && resume.data.lines ? { id: resume.data.id, lines: resume.data.lines } : undefined} />
    </>
  );
}
