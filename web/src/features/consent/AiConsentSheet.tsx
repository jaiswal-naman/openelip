"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { LocalTime } from "@/components/ui/Viewer";
import { authKeys } from "@/features/auth/queries";
import { trustKeys } from "@/features/trust/queries";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import { describeFailure, problemOf } from "@/lib/api/problems";
import styles from "./consent.module.css";
import { useAiConsent } from "./useAiConsent";

type Props = {
  /** Called once the agreement is recorded. */
  onAgreed?: () => void;
  /** Shown as a button only when given: onboarding continues without AI, settings has nothing to continue to. */
  onNotNow?: () => void;
};

/**
 * The AI processing consent sheet (FRONTEND_UI 4.6, TRU-05): every model provider this installation would send text to,
 * what each receives and why, then Agree or Not now. Agree sends the wording version this sheet displayed, so an agreement
 * can never cover a list the person did not see: if the providers changed meanwhile the API answers 409 and the sheet
 * reloads the list. Shown only when an agreement is needed; whether there is anything to agree to is the caller's call.
 */
export function AiConsentSheet({ onAgreed, onNotNow }: Props) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { gate, consent, providers } = useAiConsent();
  const [listChanged, setListChanged] = useState(false);

  const agree = useMutation({
    mutationFn: (textVersion: string) =>
      unwrap(api.PATCH("/v1/me", { body: { consents: [{ purpose: "ai_processing", granted: true, text_version: textVersion }] } })),
    onSuccess: (me) => {
      queryClient.setQueryData(authKeys.me, me);
      setListChanged(false);
      onAgreed?.();
    },
    onError: (error) => {
      if (problemOf(error).kind !== "consent_text_changed") return;
      // The wording changed under the sheet: nothing was recorded. Read the list again and let the person look at it.
      setListChanged(true);
      void queryClient.invalidateQueries({ queryKey: authKeys.me });
      void queryClient.invalidateQueries({ queryKey: trustKeys.subprocessors });
    },
  });

  if (gate === "loading") {
    return (
      <p role="status" className="muted">
        Loading the list of AI providers.
      </p>
    );
  }

  const shownVersion = consent?.current_text_version ?? null;
  const agreedBefore = gate === "providers_changed" ? consent?.granted_at : null;

  return (
    <section className={styles.sheet} aria-label="AI processing consent">
      <p>
        {agreedBefore ? (
          <>
            You agreed to AI processing on <LocalTime iso={agreedBefore} />, for a different list of providers. Agree again to keep
            using AI features; until you do, they stay off.
          </>
        ) : (
          "To read your resume and help with your record, this installation sends text to the AI model providers below. Nothing is sent until you agree."
        )}
      </p>

      <ul className={styles.providers} aria-label="AI model providers">
        {providers.map((provider) => (
          <li key={provider.name} className={styles.provider}>
            <span className={`t-card ${styles.name}`}>{provider.name}</span>
            <span className={styles.cell}>
              <span className="t-label muted">Receives</span>
              <span>{provider.receives}</span>
            </span>
            <span className={styles.cell}>
              <span className="t-label muted">Used for</span>
              <span>{provider.purpose}</span>
            </span>
          </li>
        ))}
      </ul>

      {listChanged && (
        <p role="alert" className={styles.alert}>
          The list of providers changed while you were reading. Nothing was recorded. Review the list above, then agree again.
        </p>
      )}
      {agree.isError && !listChanged && (
        <p role="alert" className={styles.alert}>
          {describeFailure(agree.error)}
        </p>
      )}

      <p role="status" className="sr-only">
        {agree.isPending ? "Saving your choice." : ""}
      </p>
      <div className={styles.actions}>
        <Button variant="primary" isPending={agree.isPending} isDisabled={shownVersion === null} onPress={() => shownVersion && agree.mutate(shownVersion)}>
          Agree
        </Button>
        {onNotNow && (
          <Button onPress={onNotNow} isDisabled={agree.isPending}>
            Not now
          </Button>
        )}
      </div>
      <p className={`t-small ${styles.note}`}>
        You can withdraw this at any time in Settings. AI features then turn off; the record, search and what you have already saved keep working.
      </p>
    </section>
  );
}
