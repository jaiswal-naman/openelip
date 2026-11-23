"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { CrewMark, CrewPlate } from "@/components/ui/CrewPlate";
import ledger from "@/components/ui/ledger.module.css";
import { Pill } from "@/components/ui/Pill";
import { ErrorState } from "@/components/ui/States";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { LocalTime } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { ACTION_STATUS_LABELS, ACTION_TYPE_LABELS, AGENT_ROLE_LABELS, CHECK_KIND_LABELS, POLICY_DECISION_LABELS, reasonText } from "@/lib/labels";
import { blockedReasonCode, recorderEntryQuery, recorderQuery, type RecorderRow } from "./queries";
import styles from "./recorder.module.css";

const KIND_LABELS: Record<RecorderRow["kind"], string> = {
  model_call: "Model call",
  tool_call: "Tool call",
  state_change: "State change",
  outside_action: "Outside action",
};

const usd = (value: string) => `USD ${Number(value).toFixed(6)}`;

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Entry({ id }: { id: number }) {
  const api = useApi();
  const entry = useQuery(recorderEntryQuery(api, id));
  const draft = entry.data?.payload;
  const to = typeof draft?.to === "string" ? draft.to : null;
  if (entry.isPending) return <p role="status">Loading the step...</p>;
  if (entry.isError) return <ErrorState what="Could not load this step." error={entry.error} onRetry={() => void entry.refetch()} />;
  const data = entry.data;
  // The Guardian runs its checks when an action is proposed and again before it is released, so one step can carry several
  // rounds. The latest verdict of each check is the one that decided (checks arrive oldest first).
  const latestChecks = [...new Map(data.checks.map((check) => [check.check, check])).values()];
  const checkRounds = Math.max(0, ...latestChecks.map((kind) => data.checks.filter((check) => check.check === kind.check).length));
  return (
    <article className={styles.stack} aria-label="Selected step">
      <h2 className={`t-section ${styles.heading}`}>
        <CrewPlate role={data.agent_role} />
        <span>{data.summary}</span>
      </h2>
      <dl className={styles.defs}>
        <Fact label="Proposed">
          <LocalTime iso={data.created_at} />
        </Fact>
        <Fact label="Kind">{KIND_LABELS[data.kind]}</Fact>
        {data.action_type !== null && <Fact label="Action">{ACTION_TYPE_LABELS[data.action_type] ?? data.action_type}</Fact>}
        <Fact label="Cost">
          <span className={styles.figure}>{usd(data.cost_usd)}</span>
        </Fact>
        {data.model_calls.length > 0 && <Fact label="Model">{data.model_calls.map((call) => call.task).join(", ")}</Fact>}
        {to !== null && <Fact label="To">{to} (you typed)</Fact>}
        {data.policy_decision !== null && <Fact label="Policy decision">{POLICY_DECISION_LABELS[data.policy_decision] ?? data.policy_decision}</Fact>}
        <Fact label="Outcome">
          {ACTION_STATUS_LABELS[data.status] ?? data.status}
          {data.outcome_code !== null && `: ${reasonText(data.outcome_code)}`}
        </Fact>
      </dl>
      {data.payload !== null && typeof data.payload.body === "string" && (
        <details>
          <summary>Show the draft</summary>
          {typeof data.payload.subject === "string" && <p className="t-card">{data.payload.subject}</p>}
          <p className={styles.draft}>{data.payload.body}</p>
        </details>
      )}
      {latestChecks.length > 0 && (
        <section aria-labelledby="checks-heading" className={styles.checks}>
          <h3 id="checks-heading" className="t-card">
            Guardian checks
          </h3>
          {checkRounds > 1 && <p className="t-small muted">Checked {checkRounds} times on this step. The latest verdict of each check is shown.</p>}
          <table className={ledger.table}>
            <caption className="sr-only">Guardian checks and their verdicts</caption>
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Verdict</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {latestChecks.map((check) => (
                <tr key={check.id}>
                  <th scope="row">{CHECK_KIND_LABELS[check.check] ?? check.check}</th>
                  <td data-label="Verdict">{check.passed ? <Pill tone="ok">Passed</Pill> : <Pill tone="bad">Blocked</Pill>}</td>
                  <td data-label={!check.passed && check.reason_code !== null ? "Why" : undefined}>
                    {!check.passed && check.reason_code !== null ? reasonText(check.reason_code) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {data.approval !== null && (
        <p>
          Approval: {data.approval.status}
          {data.approval.decided_at !== null && (
            <>
              {" "}
              (<LocalTime iso={data.approval.decided_at} />)
            </>
          )}
        </p>
      )}
    </article>
  );
}

/** The title block's cells, counted from the rows the recorder holds. */
function cellsOf(list: RecorderRow[]): TitleCell[] {
  const blocked = list.filter((row) => row.status === "blocked").length;
  const outside = list.filter((row) => row.kind === "outside_action").length;
  const cost = list.reduce((sum, row) => sum + Number(row.cost_usd), 0);
  return [
    { label: "Steps", value: list.length },
    { label: "Outside actions", value: outside },
    { label: "Blocked", value: blocked, tone: blocked === 0 ? "muted" : undefined },
    { label: "Model cost", value: usd(String(cost)) },
  ];
}

/**
 * The flight recorder (GRD-03, FRONTEND_UI 3.19): the campaign's steps in order, one selected in `?step=`, with first, previous,
 * next and last. A blocked row carries a Blocked stamp and the reason in words. On a phone the list is the first screen.
 * ponytail: no duration bars. A row carries one moment (`created_at`); an action's `executed_at` includes the hours it waited
 * for the person, so there is no real start and end per step to draw to scale.
 */
export function RecorderView({ campaignId }: { campaignId: string }) {
  const api = useApi();
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const rows = useQuery(recorderQuery(api, campaignId));
  const title = (
    <TitleBlock
      title="Flight recorder"
      lines={
        <p>
          <Link href={`/app/campaigns/${campaignId}`}>Back to the campaign</Link>
        </p>
      }
      cells={rows.data !== undefined && rows.data.length > 0 ? cellsOf(rows.data) : []}
    />
  );
  if (rows.isPending)
    return (
      <>
        {title}
        <p role="status">Loading the flight recorder...</p>
      </>
    );
  if (rows.isError)
    return (
      <>
        {title}
        <ErrorState what="Could not load the flight recorder." error={rows.error} onRetry={() => void rows.refetch()} />
      </>
    );
  const list = rows.data;
  if (list.length === 0)
    return (
      <>
        {title}
        <p className="muted">Nothing was recorded for this campaign yet.</p>
      </>
    );

  const asked = Number(params.get("step"));
  const index = Math.max(0, list.findIndex((row) => row.id === asked));
  const selected = list[index];

  function go(next: number) {
    const target = list[Math.min(list.length - 1, Math.max(0, next))];
    const query = new URLSearchParams(params.toString());
    query.set("step", String(target.id));
    router.replace(`${path}?${query.toString()}`, { scroll: false });
  }

  return (
    <>
      {title}
      <div className={styles.recorder}>
        <nav aria-label="Steps">
          <ol className={styles.entries}>
            {list.map((row, n) => {
              const reason = blockedReasonCode(row);
              return (
                <li key={row.id}>
                  <Link
                    className={styles.entryLink}
                    href={`${path}?campaign_id=${campaignId}&step=${row.id}`}
                    aria-current={n === index ? "true" : undefined}
                    scroll={false}
                  >
                    <CrewMark role={row.agent_role} />
                    <span className={styles.entryText}>
                      <span className={styles.entryHead}>
                        <span className="t-card">
                          {n + 1}. {AGENT_ROLE_LABELS[row.agent_role] ?? row.agent_role}
                        </span>
                        <span className="t-small muted">{KIND_LABELS[row.kind]}</span>
                        {row.status === "blocked" && <Pill tone="bad">Blocked</Pill>}
                      </span>
                      <span className="t-small">{row.summary}</span>
                      {reason !== null && <span className="t-small">{reasonText(reason)}</span>}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ol>
        </nav>
        <div className={styles.stack}>
          <div className={styles.row} role="group" aria-label="Move between steps">
            <Button size="sm" isDisabled={index === 0} onPress={() => go(0)}>
              First
            </Button>
            <Button size="sm" isDisabled={index === 0} onPress={() => go(index - 1)}>
              Previous
            </Button>
            <span aria-live="polite">
              Step {index + 1} of {list.length}
            </span>
            <Button size="sm" isDisabled={index === list.length - 1} onPress={() => go(index + 1)}>
              Next
            </Button>
            <Button size="sm" isDisabled={index === list.length - 1} onPress={() => go(list.length - 1)}>
              Last
            </Button>
          </div>
          <Entry id={selected.id} />
        </div>
      </div>
    </>
  );
}
