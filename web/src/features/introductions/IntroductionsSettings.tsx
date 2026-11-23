"use client";

import Link from "next/link";
import { Switch } from "react-aria-components";
import { ErrorState } from "@/components/ui/States";
import { useMe } from "@/features/auth/queries";
import { describeFailure } from "@/lib/api/problems";
import { CANDIDATE_INTRODUCTION_LABELS as WORDS } from "@/lib/labels";
import { useOpenToIntroductions } from "./api";
import styles from "./introductions.module.css";

/**
 * The Introductions section of Settings (FRONTEND_UI 3.10, HIR-04, HIR-07): whether employers may ask at all, and where to
 * block named ones. The switch is the `open_to_introductions` consent; it saves as soon as it is flipped, like a light switch.
 */
export function IntroductionsSettings() {
  const me = useMe();
  const save = useOpenToIntroductions();
  if (me.error) return <ErrorState what="Could not load your settings." error={me.error} onRetry={() => void me.refetch()} />;
  if (!me.data) {
    return (
      <p role="status" className="muted">
        Loading your settings.
      </p>
    );
  }
  const on = me.data.consents.some((consent) => consent.purpose === "open_to_introductions" && consent.granted);
  return (
    <section className={styles.settings} aria-labelledby="introductions-title">
      <h2 id="introductions-title" className="t-section">
        Introductions
      </h2>
      <p>
        When this is on, an employer whose role fits your record can ask whether you would like to talk. You see exactly what
        would be shared, and nothing is shared until you accept.
      </p>
      <Switch isSelected={on} isDisabled={save.isPending} onChange={(next) => save.mutate(next)} className={styles.switch}>
        <span className={styles.track}>
          <span className={styles.thumb} />
        </span>
        {WORDS.openToIntroductions}
      </Switch>
      <p className="t-small muted">
        {on
          ? "On. Turn it off at any time."
          : "Off. Asks you have already received stay open, and no new ones arrive."}
      </p>
      <p>
        Employers you block never see you. <Link href="/app/record">Block employers in your constraints</Link>.
      </p>
      {save.isError && (
        <p role="alert" className={styles.problem}>
          {describeFailure(save.error)} Nothing was changed.
        </p>
      )}
    </section>
  );
}
