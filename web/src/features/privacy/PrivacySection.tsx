"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { authKeys, useMe } from "@/features/auth/queries";
import { AiConsentSheet } from "@/features/consent/AiConsentSheet";
import { useAiConsent } from "@/features/consent/useAiConsent";
import { AiOffNotice } from "@/features/record/AiOffNotice";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { withdrawAiProcessing } from "./api";
import { DeleteAccount } from "./DeleteAccount";
import { ExportCard } from "./ExportCard";
import styles from "./privacy.module.css";

const SIGN_IN = `/sign-in?next=${encodeURIComponent("/app/settings?section=privacy")}`;

/** The privacy parts of Settings (FRONTEND_UI 3.10): AI processing, export of everything held, and deleting the account. */
export function PrivacySection() {
  return (
    <>
      <AiProcessing />
      <ExportCard />
      <AccountDeletion />
    </>
  );
}

/** Deleting the account, which needs the address the code is sent to. A session that ended while the page was open goes to sign in and back. */
export function AccountDeletion() {
  const router = useRouter();
  const me = useMe();

  useEffect(() => {
    if (me.data === null) router.replace(SIGN_IN);
  }, [me.data, router]);

  return me.data ? <DeleteAccount email={me.data.email} /> : null;
}

/**
 * Whether AI features may read this person's text, and by which providers (TRU-05). Withdrawing turns AI features off
 * and keeps everything already saved. When the list of providers changes the agreement no longer covers it, and this says so.
 */
export function AiProcessing() {
  const api = useApi();
  const queryClient = useQueryClient();
  const ai = useAiConsent();
  const withdraw = useMutation({
    mutationFn: () => withdrawAiProcessing(api),
    onSuccess: (me) => queryClient.setQueryData(authKeys.me, me),
  });
  const { gate, consent } = ai;

  return (
    <section className={styles.section} aria-labelledby="ai-title">
      <h2 id="ai-title" className="t-section">
        AI processing
      </h2>
      {gate === "loading" ? (
        ai.error ? (
          <ErrorState what="Could not load the AI processing settings." error={ai.error} onRetry={() => void ai.refetch()} />
        ) : (
          <p role="status" className="muted">
            Loading.
          </p>
        )
      ) : (
        <>
          {gate === "ai_disabled" && <AiOffNotice reason="ai_disabled" />}
          {consent?.granted && consent.granted_at ? (
            <div className={styles.stack}>
              <p>
                You agreed on <LocalTime iso={consent.granted_at} />
                {consent.providers && consent.providers.length > 0 && <>. Providers: {consent.providers.join(", ")}</>}.
              </p>
              {gate !== "providers_changed" && (
                <p className="muted">AI features can read your resume and help with your record. Withdrawing turns them off; what you saved stays.</p>
              )}
              <div>
                <Button isPending={withdraw.isPending} onPress={() => withdraw.mutate()}>
                  Withdraw
                </Button>
              </div>
            </div>
          ) : (
            gate !== "ai_disabled" && <p className="muted">You have not agreed to AI processing, so AI features are off. Search, your record and everything already saved still work.</p>
          )}
          {withdraw.isSuccess && !consent?.granted && (
            <p role="status">AI processing is withdrawn. AI features are off.</p>
          )}
          {withdraw.isError && (
            <p role="alert" className={styles.problem}>
              {describeFailure(withdraw.error)}
            </p>
          )}
          {gate === "providers_changed" && <p className="t-card">Providers changed, agree again</p>}
          {(gate === "providers_changed" || gate === "consent_required") && <AiConsentSheet />}
        </>
      )}
    </section>
  );
}
