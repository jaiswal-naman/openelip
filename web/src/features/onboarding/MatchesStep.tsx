"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { LinkButton } from "@/components/ui/LinkButton";
import { AiOffNotice, ErrorState } from "@/components/ui/States";
import { authKeys, useMe } from "@/features/auth/queries";
import { saveTimeZone } from "@/features/brief/queries";
import { MatchCard } from "@/features/matches/MatchCard";
import { todayQuery } from "@/features/matches/queries";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { useViewer } from "@/lib/viewer";
import { TimeZoneSelect } from "@/ui/patterns/TimeZoneSelect";
import styles from "./onboarding.module.css";

const POLL_MS = 5_000;
// The first run is queued by a sweep that looks once a minute and then scores jobs one after another, so it can take a
// while. Two minutes of looking, then the page says where the answer will be instead of waiting for ever.
const POLL_LIMIT = 24;
const EVIDENCE_TASK = "match.evidence.score"; // the model task that makes the requirement tables
const TODAY_CARDS = 5; // the most a day can show (MAT-02)

/**
 * Step 6 (FRONTEND_UI 3.2): the time zone the 07:00 brief is read in, detected from the browser and confirmed, and then the
 * first matches, the same cards as Today, which count toward today's five. Confirming saves the zone and starts looking: the
 * page asks for today's matches until a run has finished for the day. Zero is a valid answer and is said plainly.
 */
export function MatchesStep() {
  const api = useApi();
  const queryClient = useQueryClient();
  const viewer = useViewer();
  const me = useMe();
  const [chosen, setChosen] = useState<string | null>(null); // null until the person picks another: the detected zone stands
  const [editing, setEditing] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const zone = chosen ?? viewer.timeZone;

  const save = useMutation({
    mutationFn: () => saveTimeZone(api, zone),
    onSuccess: (updated) => {
      queryClient.setQueryData(authKeys.me, updated);
      setConfirmed(true);
    },
  });
  const today = useQuery({
    ...todayQuery(api),
    enabled: confirmed,
    refetchInterval: (query) => ((query.state.data?.run ?? null) === null && query.state.dataUpdateCount < POLL_LIMIT ? POLL_MS : false),
  });

  const aiOn = me.data ? me.data.capabilities.includes(EVIDENCE_TASK) : true;
  const matches = today.data?.matches ?? [];
  const finished = today.data !== undefined && today.data.run !== null;
  // How many times the answer has come back, read from the cache. `dataUpdatedAt` is read so this renders again on every
  // answer: an answer equal to the last one leaves `data` as it was, and a render that did not read it would be skipped.
  const looks = today.dataUpdatedAt > 0 ? (queryClient.getQueryState(todayQuery(api).queryKey)?.dataUpdateCount ?? 0) : 0;
  const stillLooking = confirmed && !today.isError && !finished && looks < POLL_LIMIT;

  return (
    <div className={styles.stack}>
      <section aria-labelledby="zone-heading" className={`${styles.stack} ${styles.part}`}>
        <h2 id="zone-heading" className="t-section">
          Your brief
        </h2>
        <p>
          Your brief arrives by 07:00 in <strong>{zone}</strong>.{" "}
          <Button variant="quiet" size="sm" aria-expanded={editing} onPress={() => setEditing((open) => !open)}>
            Change
          </Button>
        </p>
        {editing && <TimeZoneSelect value={zone} onChange={setChosen} />}
        {!confirmed && (
          <Button variant="primary" isPending={save.isPending} onPress={() => save.mutate()}>
            Confirm and look for my first matches
          </Button>
        )}
        {save.isError && (
          <p role="alert" className={styles.problem}>
            {describeFailure(save.error)} Your time zone was not saved.
          </p>
        )}
      </section>

      {confirmed && (
        <section aria-labelledby="first-heading" className={`${styles.stack} ${styles.part}`}>
          <h2 id="first-heading" className="t-section">
            Your first matches
          </h2>
          {!aiOn && (
            <AiOffNotice>
              Requirement tables need a model key, and this server has none set. Jobs are still found and your hard constraints still apply, but none can
              be checked against your record, so none is shown as a match.
            </AiOffNotice>
          )}
          {today.isError ? (
            <ErrorState what="Could not load your first matches." error={today.error} onRetry={() => void today.refetch()} />
          ) : stillLooking ? (
            <p role="status" className="muted">
              Looking for your first matches. This can take a minute.
            </p>
          ) : !finished ? (
            <p>Your first run has not finished yet. Its matches will appear on Today, and your first brief arrives tomorrow by 07:00.</p>
          ) : matches.length > 0 ? (
            <ul className={styles.cards}>
              {matches.slice(0, TODAY_CARDS).map((match) => (
                <li key={match.decision_id}>
                  <MatchCard match={match} headingLevel={3} />
                </li>
              ))}
            </ul>
          ) : (
            aiOn && <p>Nothing in the current index meets your constraints well enough yet. Your first brief arrives tomorrow by 07:00.</p>
          )}
          <LinkButton href="/app/today" variant="primary">
            Go to Today
          </LinkButton>
        </section>
      )}
    </div>
  );
}
