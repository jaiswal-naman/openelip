"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { LinkButton } from "@/components/ui/LinkButton";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { AiOffNotice, EmptyState, ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useMe } from "@/features/auth/queries";
import { BriefCampaignItems, campaignItems, matchDecisions } from "@/features/brief/BriefCampaignItems";
import { BriefIntroductions, introductionItems } from "@/features/brief/BriefIntroductions";
import { isOpenAsk, useIntroductions } from "@/features/introductions/api";
import { TodayIntroductions } from "@/features/introductions/TodayIntroductions";
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
 * Today (FRONTEND_UI 3.3): the morning brief as a review queue of decisions waiting, and what was done, today's matches, and everything held
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
  // Introductions (Phase 6): each open ask is a card of its own, read from the introductions, not the brief; the brief's
  // introduction items are listed as lines only when their ask is no longer open, so nothing shows twice.
  const decisions = matchDecisions(brief.data?.decisions ?? []);
  const agentItems = campaignItems(brief.data?.decisions ?? []);
  const introductions = useIntroductions();
  const openAsks = (introductions.data ?? []).filter((item) => isOpenAsk(item));
  const briefAsks = introductionItems(brief.data?.decisions ?? []).filter((item) => !openAsks.some((ask) => ask.id === item.introduction_id));
  const waiting = decisions.length + openAsks.length + agentItems.filter((item) => item.kind === "approval").length;
  const asked = params.get("view");
  const openId = params.get("decision");
  // Decisions come first when there are any, while the brief is still loading, and when it could not be loaded (so the
  // failure is seen); a brief that holds none, or none yet, opens on the matches.
  const briefKnown = brief.data !== undefined && !brief.isError;
  const firstUseful: View = openId !== null || waiting > 0 || !briefKnown ? "decisions" : "matches";
  // Chosen once, when the brief and the introductions have both arrived: accepting the last open ask must not move the
  // person to Matches while the sheet closes, because the introduction they just accepted is listed under Decisions.
  const [opened, setOpened] = useState<View | null>(null);
  if (opened === null && briefKnown && (introductions.data !== undefined || introductions.isError)) {
    setOpened(firstUseful);
  }
  const view: View = (VIEWS as readonly string[]).includes(asked ?? "") ? (asked as View) : (opened ?? firstUseful);
  const held = useQuery({ ...whyNotQuery(api, date ?? ""), enabled: view === "why-not" && date !== null });

  const matches = day.data?.matches ?? [];
  const aiOn = me.data ? me.data.capabilities.includes(EVIDENCE_TASK) : true; // not known yet: say nothing is off
  const paused = me.data?.brief.frequency === "paused";

  function go(next: View) {
    router.push(`/app/today?view=${next}`, { scroll: false });
  }

  return (
    <div className={styles.today}>
      <TitleBlock
        eyebrow="Today"
        title={date === null ? "Today" : `Today, ${dayLabel(date, viewer.locale)}`}
        lines={
          <>
            <p className={styles.lead}>
              {brief.data === undefined
                ? "Loading your brief."
                : waiting === 0
                  ? "Nothing needs you today."
                  : `${waiting} ${waiting === 1 ? "decision needs" : "decisions need"} you.`}
            </p>
            {brief.data?.delivery.email === "sent" && brief.data.delivery.email_sent_at && (
              <p className="t-small">
                Brief emailed <LocalTime iso={brief.data.delivery.email_sent_at} />
              </p>
            )}
          </>
        }
        cells={[
          { label: "Needs you", value: brief.data === undefined ? "…" : waiting, tone: waiting > 0 ? "redline" : "muted" },
          { label: "Matches", value: day.data === undefined ? "…" : matches.length },
          { label: "Brief", value: paused ? "Paused" : brief.data ? "Delivered" : "Not yet", tone: paused || !brief.data ? "muted" : "ok" },
        ]}
      />

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
            Decisions{brief.data ? ` (${waiting})` : ""}
          </Tab>
          <Tab id="matches" className={styles.tab}>
            Matches{day.data ? ` (${matches.length})` : ""}
          </Tab>
          <Tab id="why-not" className={styles.tab}>
            Why not
          </Tab>
        </TabList>

        <TabPanel id="decisions" className={styles.panel}>
          <TodayIntroductions openId={openId !== null && UUID.test(openId) ? openId : null} />
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
                openAsks.length === 0 && agentItems.length === 0 && <EmptyState>Nothing needs you today.</EmptyState>
              ) : (
                <>
                  <p className={`t-label t-eyebrow ${styles.queueLabel}`}>Review queue</p>
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
                </>
              )}
              <BriefIntroductions items={briefAsks} />
              <BriefCampaignItems items={agentItems} undoable={brief.data.undoable} />
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
          {held.isError || day.isError ? (
            <ErrorState
              what="Could not load what was held back."
              error={held.error ?? day.error}
              onRetry={() => {
                void day.refetch();
                void held.refetch();
              }}
            />
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
