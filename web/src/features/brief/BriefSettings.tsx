"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Label, Radio, RadioGroup, Switch } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { ErrorState } from "@/components/ui/States";
import { authKeys, useMe } from "@/features/auth/queries";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import type { Me } from "@/lib/api/types";
import { BRIEF_FREQUENCY_LABELS } from "@/lib/labels";
import { NativeSelect } from "@/ui/patterns/NativeSelect";
import { TimeZoneSelect } from "@/ui/patterns/TimeZoneSelect";
import styles from "./brief.module.css";
import { type BriefChoices, type BriefFrequency, DEFAULT_WEEKDAY, saveBriefSettings } from "./queries";

const SIGN_IN = `/sign-in?next=${encodeURIComponent("/app/settings?section=brief")}`;
const FREQUENCIES: BriefFrequency[] = ["daily", "weekly", "paused"];
// ISO weekdays, as the API counts them: 1 is Monday and 7 is Sunday.
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((name, index) => ({ value: String(index + 1), label: name }));

/** The Brief section of Settings (FRONTEND_UI 3.10, BRF-03): how often, which time zone, and whether it is also emailed. */
export function BriefSettings() {
  const router = useRouter();
  const me = useMe();

  // A session that ended while the page was open: back to sign in, and then back here.
  useEffect(() => {
    if (me.data === null) router.replace(SIGN_IN);
  }, [me.data, router]);

  if (me.error) return <ErrorState what="Could not load your brief settings." error={me.error} onRetry={() => void me.refetch()} />;
  if (!me.data) {
    return (
      <p role="status" className="muted">
        Loading your brief settings.
      </p>
    );
  }
  return <BriefForm me={me.data} />;
}

const emailIsOn = (me: Me) => me.consents.some((consent) => consent.purpose === "brief_email" && consent.granted);

/**
 * The choices are kept here until Save, which sends them in one request and says it is saved. A weekday picker shows only
 * for a weekly brief. The email switch is the `brief_email` consent: turning it off here, or with the link in any email,
 * ends the same consent, so the page cannot tell the two apart and does not claim to.
 */
function BriefForm({ me }: { me: Me }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const emailWas = emailIsOn(me);
  const [choices, setChoices] = useState<BriefChoices>({
    timezone: me.timezone,
    frequency: me.brief.frequency,
    weekday: me.brief.weekday ?? DEFAULT_WEEKDAY,
    emailOn: emailWas,
  });
  const save = useMutation({
    mutationFn: () => saveBriefSettings(api, choices, choices.emailOn !== emailWas),
    onSuccess: (updated) => queryClient.setQueryData(authKeys.me, updated),
  });

  // Any change takes the "Saved." away: it said the old choices were kept.
  function change(next: Partial<BriefChoices>) {
    setChoices((current) => ({ ...current, ...next }));
    save.reset();
  }

  return (
    <section className={styles.section} aria-labelledby="brief-title">
      <h2 id="brief-title" className="t-section">
        Morning brief
      </h2>
      <p className={styles.lead}>
        One brief, ready by 07:00 in the time zone below: the decisions waiting for you and what was done. It always appears on Today; the email is
        optional.
      </p>

      <RadioGroup value={choices.frequency} onChange={(value) => change({ frequency: value as BriefFrequency })} className={styles.group}>
        <Label className={field.label}>How often</Label>
        <div className={styles.segmented}>
          {FREQUENCIES.map((frequency) => (
            <Radio key={frequency} value={frequency} className={styles.segment}>
              {BRIEF_FREQUENCY_LABELS[frequency]}
            </Radio>
          ))}
        </div>
      </RadioGroup>

      {choices.frequency === "weekly" && (
        <NativeSelect label="Day of the week" value={String(choices.weekday)} onChange={(value) => change({ weekday: Number(value) })} options={WEEKDAYS} />
      )}
      {choices.frequency === "paused" && <p className="muted">While it is paused no brief is made and no email is sent. Decisions already waiting stay on Today.</p>}

      <TimeZoneSelect value={choices.timezone} onChange={(timezone) => change({ timezone })} />

      <div className={styles.switchRow}>
        <Switch isSelected={choices.emailOn} onChange={(emailOn) => change({ emailOn })} className={styles.switch}>
          <span className={styles.track}>
            <span className={styles.thumb} />
          </span>
          Email me the brief
        </Switch>
        <p className={`t-small ${styles.hint}`}>
          {choices.emailOn
            ? "On. Every email has a link that turns this off in one click."
            : "Off. Decisions still appear on Today, and nothing is emailed."}
        </p>
      </div>

      <div className={styles.buttons}>
        <Button variant="primary" isPending={save.isPending} onPress={() => save.mutate()}>
          Save
        </Button>
      </div>
      <p role="status" className={styles.saved}>
        {save.isSuccess ? "Saved." : ""}
      </p>
      {save.isError && (
        <p role="alert" className={styles.problem}>
          {describeFailure(save.error)} Nothing was changed.
        </p>
      )}
    </section>
  );
}
