"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { authKeys, useMe } from "@/features/auth/queries";
import { useApi } from "@/lib/api/context";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import styles from "./brief.module.css";
import { chooseWeeklyBrief, turnBriefEmailOn, unsubscribeWithToken } from "./queries";

const BRIEF_SETTINGS = "/app/settings?section=brief";
const SIGN_IN_TO_SETTINGS = `/sign-in?next=${encodeURIComponent(BRIEF_SETTINGS)}`;

type Phase = "ready" | "stopped" | "expired";
type Then = "email_on" | "weekly" | null;

/**
 * The page an email's "Stop this email" link opens (BRF-03, FRONTEND_UI 3.25). Nothing happens when it loads, so a mail
 * system that opens every link to scan it unsubscribes no one (threat T-04-32); only the button sends anything, and it
 * sends the token alone, with no session (threat T-04-33). The person may be signed out, so everything after the click works
 * either way: Undo and "Choose weekly instead" act for a signed-in person and lead to sign-in, then the Brief settings,
 * for a signed-out one.
 */
export function UnsubscribeView({ token }: { token: string | null }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const me = useMe();
  const signedIn = Boolean(me.data);
  const [phase, setPhase] = useState<Phase>(token === null ? "expired" : "ready");
  const [then, setThen] = useState<Then>(null);

  const stop = useMutation({
    mutationFn: () => unsubscribeWithToken(api, token ?? ""),
    onSuccess: () => setPhase("stopped"),
    // A token that is wrong, altered or too old is the same answer: this link cannot be used. Any other failure can be tried again.
    onError: (error) => isFailure(error, "not_found") && setPhase("expired"),
  });
  const undo = useMutation({
    mutationFn: () => turnBriefEmailOn(api),
    onSuccess: (updated) => {
      queryClient.setQueryData(authKeys.me, updated);
      setThen("email_on");
    },
  });
  const weekly = useMutation({
    mutationFn: () => chooseWeeklyBrief(api),
    onSuccess: (updated) => {
      queryClient.setQueryData(authKeys.me, updated);
      setThen("weekly");
    },
  });

  // The button that was pressed is gone, so the heading takes the focus: a keyboard or screen-reader user lands on the result.
  const heading = useRef<HTMLHeadingElement>(null);
  const state = `${phase}:${then}`;
  const shown = useRef(state);
  useEffect(() => {
    if (shown.current === state) return; // the first render, which must not take the focus, and a repeat of the same one
    shown.current = state;
    heading.current?.focus();
  }, [state]);

  const title = phase === "expired" ? "This link has expired" : phase === "stopped" ? "Morning brief email stopped" : "Stop the morning brief email";
  const failure = stop.error ?? undo.error ?? weekly.error;

  return (
    <div className={styles.unsubscribe}>
      <h1 ref={heading} tabIndex={-1} className="t-page">
        {title}
      </h1>

      {phase === "ready" && (
        <>
          <p>Nothing has changed yet. Press the button to stop the morning brief email. Decisions will still appear in the app.</p>
          <Button variant="primary" isPending={stop.isPending} onPress={() => stop.mutate()}>
            Stop the morning brief email
          </Button>
        </>
      )}

      {phase === "expired" && (
        <>
          <p>This unsubscribe link has expired or is not complete. You can turn the morning brief email off in your settings.</p>
          <LinkButton href={BRIEF_SETTINGS} variant="primary">
            Open the brief settings
          </LinkButton>
        </>
      )}

      {phase === "stopped" && then === null && (
        <>
          <p>You will not receive the morning brief by email. Decisions still appear in the app.</p>
          <div className={styles.buttons}>
            {signedIn ? (
              <>
                <Button isPending={undo.isPending} onPress={() => undo.mutate()}>
                  Undo
                </Button>
                <Button isPending={weekly.isPending} onPress={() => weekly.mutate()}>
                  Choose weekly instead
                </Button>
              </>
            ) : (
              <>
                <LinkButton href={SIGN_IN_TO_SETTINGS}>Undo</LinkButton>
                <LinkButton href={SIGN_IN_TO_SETTINGS}>Choose weekly instead</LinkButton>
              </>
            )}
          </div>
          {!signedIn && <p className="t-small muted">Both ask you to sign in first, because they change your settings.</p>}
        </>
      )}

      {phase === "stopped" && then === "email_on" && (
        <p>
          The morning brief email is on again. You can change how often it comes in your <Link href={BRIEF_SETTINGS}>settings</Link>.
        </p>
      )}
      {phase === "stopped" && then === "weekly" && (
        <p>
          You will get the morning brief by email once a week, on Mondays. You can change the day in your <Link href={BRIEF_SETTINGS}>settings</Link>.
        </p>
      )}

      {failure !== null && phase !== "expired" && (
        <p role="alert" className={styles.problem}>
          {describeFailure(failure)}
        </p>
      )}
    </div>
  );
}
