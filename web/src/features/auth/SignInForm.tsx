"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useActionState, useEffect, useRef, useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { ApiFailure, unwrap } from "@/lib/api/errors";
import type { Me } from "@/lib/api/types";
import { messageFor, waitPhrase } from "@/lib/labels";
import { authKeys } from "./queries";
import styles from "./signin.module.css";

type State = {
  step: "email" | "code";
  email: string;
  code: string;
  /** How long the emailed code is valid, in seconds, as the API said. */
  validFor: number | null;
  /** When the last code was sent (ms since the epoch), so the resend cooldown restarts with each. */
  sentAt: number | null;
  error: string | null;
  signedIn: boolean;
};

const START: State = { step: "email", email: "", code: "", validFor: null, sentAt: null, error: null, signedIn: false };
const RESEND_COOLDOWN_SECONDS = 45;
const DEFAULT_LANDING = "/jobs"; // what safeNextPath returns when the person followed no link

/**
 * Where to go after signing in (FRONTEND_UI 2.4). A deep link always wins. With none, an organization member who has no
 * confirmed Career Record lands on the hiring desk instead of the job index; everyone else goes where `next` says.
 */
async function landingFor(api: Api, me: Me, next: string): Promise<string> {
  if (next !== DEFAULT_LANDING || (me.organizations ?? []).length === 0) return next;
  try {
    const record = await unwrap(api.GET("/v1/me/record"));
    return record.facts.length === 0 ? "/hire/roles" : next;
  } catch {
    return next; // not knowing is not a reason to send someone away from where they were going
  }
}

/** The sentence under a field for a failed call. Each step words the "not valid" case for its own field. */
function problem(error: unknown, invalidInput: string): string {
  if (!(error instanceof ApiFailure)) return messageFor({ code: "unknown", requestId: null, retryAfterSeconds: null });
  if (error.code === "validation_failed") return invalidInput;
  if (error.code === "code_invalid") {
    const left = error.details.attempts_remaining;
    if (left === 0) return "That code can no longer be used. Send a new one.";
    if (typeof left === "number") return `${messageFor(error)} ${left} ${left === 1 ? "try" : "tries"} left.`;
  }
  return messageFor(error);
}

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

/**
 * Sign in with an emailed one-time code (FRONTEND_UI 3.1). Step 1 asks for the email and always moves on to step 2, with
 * the same words for every address, so the page never says whether an account exists. Step 2 takes the code.
 * `next` is already checked by the page: it is a path on this site.
 */
export function SignInForm({ next }: { next: string }) {
  const api = useApi();
  const router = useRouter();
  const queryClient = useQueryClient();
  const codeBox = useRef<HTMLInputElement>(null);
  const emailBox = useRef<HTMLInputElement>(null);

  async function run(previous: State, formData: FormData): Promise<State> {
    const intent = String(formData.get("intent"));
    if (intent === "change") return { ...START, email: previous.email };

    if (intent === "send") {
      const email = String(formData.get("email") ?? "").trim();
      if (email === "") return { ...previous, email, error: "Enter your email address." };
      try {
        const sent = await unwrap(api.POST("/v1/auth/codes", { body: { email } }));
        return { ...START, step: "code", email, validFor: sent.expires_in_seconds, sentAt: Date.now() };
      } catch (error) {
        return { ...previous, email, error: problem(error, "Enter a valid email address.") };
      }
    }

    const code = String(formData.get("code") ?? "").replace(/\s/g, ""); // a pasted "123 456" is fine
    if (!/^\d{6}$/.test(code)) return { ...previous, code, error: "Enter the six digits from the email." };
    try {
      const me = await unwrap(
        api.POST("/v1/auth/sessions", {
          body: { email: previous.email, code, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone },
        }),
      );
      queryClient.setQueryData(authKeys.me, me);
      router.replace(await landingFor(api, me, next));
      router.refresh(); // pages already in the router cache were rendered for nobody
      return { ...previous, code, error: null, signedIn: true };
    } catch (error) {
      return { ...previous, code, error: problem(error, "Enter the six digits from the email.") };
    }
  }

  const [state, action, isPending] = useActionState(run, START);

  // Resend is allowed after a cooldown that restarts with each code sent. The seconds left are worked out from the time the
  // code was sent, so a tick that is late or skipped cannot make the count wrong. Counted down in view, not announced.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (state.sentAt === null) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [state.sentAt]);
  const waited = state.sentAt === null ? RESEND_COOLDOWN_SECONDS : Math.floor(Math.max(0, now - state.sentAt) / 1000);
  const secondsLeft = Math.max(0, RESEND_COOLDOWN_SECONDS - waited);

  // After a failed attempt the cursor goes back to the field the message is about
  useEffect(() => {
    if (state.error) (state.step === "email" ? emailBox : codeBox).current?.focus();
  }, [state]);

  const working = isPending || state.signedIn;

  return (
    // validationBehavior "aria": the API's answer is shown beside the field but never blocks pressing the button again,
    // so after a network error or a rate limit the person can simply retry. The API is the one source of validation truth.
    <Form action={action} validationBehavior="aria" className={styles.form}>
      <p role="status" className="sr-only">
        {working ? "Please wait." : ""}
      </p>
      {state.step === "email" ? (
        <>
          <TextField
            name="email"
            type="email"
            label="Email"
            autoComplete="email"
            defaultValue={state.email}
            inputRef={emailBox}
            error={state.error}
            isRequired
            autoFocus
          />
          <Button type="submit" name="intent" value="send" variant="primary" isPending={isPending}>
            Send code
          </Button>
        </>
      ) : (
        <>
          <p className={styles.sent}>
            Code sent to <strong>{state.email}</strong>.
            {state.validFor !== null && ` It is valid for ${waitPhrase(state.validFor)}.`}
          </p>
          <input type="hidden" name="email" value={state.email} />
          <TextField
            name="code"
            label="Code"
            inputMode="numeric"
            autoComplete="one-time-code"
            description="Six digits. Pasting is fine."
            defaultValue={state.code}
            inputRef={codeBox}
            error={state.error}
            isRequired
            autoFocus
            code
          />
          {/* Sign in comes first in the page, so it is the button Enter presses */}
          <Button type="submit" name="intent" value="verify" variant="primary" isPending={working}>
            Sign in
          </Button>
          <div className={styles.secondary}>
            <Button
              type="submit"
              name="intent"
              value="send"
              variant="quiet"
              size="sm"
              formNoValidate
              isDisabled={secondsLeft > 0 || isPending || state.signedIn}
            >
              {secondsLeft > 0 ? `Resend in ${clock(secondsLeft)}` : "Send a new code"}
            </Button>
            <Button type="submit" name="intent" value="change" variant="quiet" size="sm" formNoValidate isDisabled={state.signedIn}>
              Use a different email
            </Button>
          </div>
        </>
      )}
    </Form>
  );
}
