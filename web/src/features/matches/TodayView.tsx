"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { LinkButton } from "@/components/ui/LinkButton";
import { AiOffNotice, EmptyState, ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useMe } from "@/features/auth/queries";
import { useApi } from "@/lib/api/context";
import { useViewer } from "@/lib/viewer";
import { dayLabel } from "./day";
import { DecisionCard, decisionIdOf } from "./DecisionCard";
import { MatchCard } from "./MatchCard";
import { latestBriefQuery, todayQuery, whyNotQuery } from "./queries";
import styles from "./today.module.css";
import { WhyNotList } from "./WhyNotList";

const VIEWS = ["decisions", "matches", "why-not"] as const;
type View = (typeof VIEWS)[number];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SIGN_IN = `/sign-in?next=${encodeURIComponent("/app/today")}`;
const EVIDENCE_TASK = "match.evidence.score"; // the model task that makes the requirement tables

/**
 * Today (FRONTEND_UI 3.3): the morning brief as decisions waiting and what was done, today's matches, and everything held
 * back. The tab is `?view=` and the decision an email link opens is `?decision=`, so a reload, a shared link and the back
 * button all land where the person was. With decisions waiting the page opens on them; with none it opens on the matches.
 * Zero is a valid day, and the page says why it is empty instead of showing nothing.
 */
export function TodayView() {
  const api = useApi();
  const router = useRouter();
  const params = useSearchParams();
  const viewer = useViewer();
  const me = useMe();
  const day = useQuery(todayQuery(api));
  const brief = useQuery(latestBriefQuery(api));

  // A session that ended while the page was open: back to sign in, and then back here.
  useEffect(() => {
    if (me.data === null) router.replace(SIGN_IN);
  }, [me.data, router]);

  const date = day.data?.local_date ?? null;
  const decisions = brief.data?.decisions ?? [];
  const asked = params.get("view");
  const openId = params.get("decision");
  // Decisions come first when there are any, while the brief is still loading, and when it could not be loaded (so the
  // failure is seen); a brief that holds none, or none yet, opens on the matches.
  const briefKnown = brief.data !== undefined && !brief.isError;
  const view: View = (VIEWS as readonly string[]).includes(asked ?? "")
    ? (asked as View)
    : openId !== null || decisions.length > 0 || !briefKnown
      ? "decisions"
      : "matches";
  const held = useQuery({ ...whyNotQuery(api, date ?? ""), enabled: view === "why-not" && date !== null });

  const matches = day.data?.matches ?? [];
  const aiOn = me.data ? me.data.capabilities.includes(EVIDENCE_TASK) : true; // not known yet: say nothing is off
  const paused = me.data?.brief.frequency === "paused";

  function go(next: View) {
    router.push(`/app/today?view=${next}`, { scroll: false });
  }

  return (
    <div className={styles.today}>
      <header className={styles.head}>
        <h1 className="t-page">{date === null ? "Today" : `Today, ${dayLabel(date, viewer.locale)}`}</h1>
        <p className={styles.lead}>
          {brief.data === undefined
            ? "Loading your brief."
            : decisions.length === 0
              ? "Nothing needs you today."
              : `${decisions.length} ${decisions.length === 1 ? "decision needs" : "decisions need"} you.`}
        </p>
        {brief.data?.delivery.email === "sent" && brief.data.delivery.email_sent_at && (
          <p className="t-small muted">
            Brief emailed <LocalTime iso={brief.data.delivery.email_sent_at} />
          </p>
        )}
      </header>

      {paused && (
        <p className={styles.banner}>
          <span className={`t-label ${styles.bannerLabel}`}>Paused</span>
          <span>Your morning brief is paused. </span>
          <Link href="/app/settings?section=brief">Resume brief</Link>
        </p>
      )}

      <Tabs selectedKey={view} onSelectionChange={(key) => go(String(key) as View)}>
        <TabList aria-label="Today" className={styles.tabList}>
          <Tab id="decisions" className={styles.tab}>
            Decisions{brief.data ? ` (${decisions.length})` : ""}
          </Tab>
          <Tab id="matches" className={styles.tab}>
            Matches{day.data ? ` (${matches.length})` : ""}
          </Tab>
          <Tab id="why-not" className={styles.tab}>
            Why not
          </Tab>
        </TabList>

        <TabPanel id="decisions" className={styles.panel}>
          {brief.isError ? (
            <ErrorState what="Could not load your brief." error={brief.error} onRetry={() => void brief.refetch()} />
          ) : brief.data === undefined ? (
            <p role="status" className="muted">
              Loading your brief.
            </p>
          ) : brief.data === null ? (
            <EmptyState>Your first brief arrives by 07:00 tomorrow. Today&apos;s matches, if there are any, are on the Matches tab.</EmptyState>
          ) : (
            <>
              {decisions.length === 0 ? (
                <EmptyState>Nothing needs you today.</EmptyState>
              ) : (
                <ul className={styles.list}>
                  {decisions.map((decision) => {
                    const id = decisionIdOf(decision);
                    return (
                      <li key={id ?? decision.kind}>
                        <DecisionCard
                          decision={decision}
                          feedback={matches.find((match) => match.decision_id === id)?.feedback}
                          isOpen={id !== null && id === openId && UUID.test(id)}
                        />
                      </li>
                    );
                  })}
                </ul>
              )}
              {brief.data.done_since_last_brief.length > 0 && (
                <section aria-labelledby="done-heading" className={styles.done}>
                  <h2 id="done-heading" className="t-section">
                    What was done since your last brief
                  </h2>
                  <p className="t-small muted">Counted from your match runs.</p>
                  <ul className={styles.doneList}>
                    {brief.data.done_since_last_brief.map((line) => (
                      <li key={`${line.at}:${line.text}`}>
                        <span className="t-data muted">
                          <LocalTime iso={line.at} />
                        </span>{" "}
                        {line.text}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </TabPanel>

        <TabPanel id="matches" className={styles.panel}>
          {!aiOn && (
            <AiOffNotice>
              Requirement tables need a model key, and this server has none set. Jobs are still found and your hard constraints still
              apply, but none can be checked against your record, so none is shown as a match.
            </AiOffNotice>
          )}
          {day.isError ? (
            <ErrorState what="Could not load today's matches." error={day.error} onRetry={() => void day.refetch()} />
          ) : day.data === undefined ? (
            <p role="status" className="muted">
              Loading today&apos;s matches.
            </p>
          ) : matches.length > 0 ? (
            <ul className={styles.list}>
              {matches.map((match) => (
                <li key={match.decision_id}>
                  <MatchCard match={match} />
                </li>
              ))}
            </ul>
          ) : (
            aiOn && (
              <EmptyState action={<LinkButton href="/app/today?view=why-not">See what was held back</LinkButton>}>
                {day.data.run === null
                  ? "No match run has finished for this day yet. Runs start after 03:00 in your time zone."
                  : `Nothing in the current index meets your constraints well enough today. Checked the index: ${day.data.run.shown} shown, ${day.data.run.withheld} held back, ${day.data.run.filtered} filtered by your constraints. The next brief arrives by 07:00 tomorrow.`}
              </EmptyState>
            )
          )}
        </TabPanel>

        <TabPanel id="why-not" className={styles.panel}>
          {held.isError ? (
            <ErrorState what="Could not load what was held back." error={held.error} onRetry={() => void held.refetch()} />
          ) : day.data === undefined || held.data === undefined ? (
            <p role="status" className="muted">
              Loading what was held back.
            </p>
          ) : (
            <WhyNotList rows={held.data} run={day.data.run} />
          )}
        </TabPanel>
      </Tabs>
    </div>
  );
}
