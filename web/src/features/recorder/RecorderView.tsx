"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { ACTION_STATUS_LABELS, ACTION_TYPE_LABELS, AGENT_ROLE_LABELS, CHECK_KIND_LABELS, POLICY_DECISION_LABELS, reasonText } from "@/lib/labels";
import { blockedReasonCode, recorderEntryQuery, recorderQuery, type RecorderRow } from "./queries";
import styles from "@/features/campaigns/campaigns.module.css";

const KIND_LABELS: Record<RecorderRow["kind"], string> = {
  model_call: "Model call",
  tool_call: "Tool call",
  state_change: "State change",
  outside_action: "Outside action",
};

const usd = (value: string) => `USD ${Number(value).toFixed(6)}`;

function Entry({ id }: { id: number }) {
  const api = useApi();
  const entry = useQuery(recorderEntryQuery(api, id));
  const draft = entry.data?.payload;
  const to = typeof draft?.to === "string" ? draft.to : null;
  if (entry.isPending) return <p role="status">Loading the step...</p>;
  if (entry.isError) return <ErrorState what="Could not load this step." error={entry.error} onRetry={() => void entry.refetch()} />;
  const data = entry.data;
  return (
    <article className={styles.stack} aria-label="Selected step">
      <h2 className="t-section">
        {AGENT_ROLE_LABELS[data.agent_role] ?? data.agent_role}: {data.summary}
      </h2>
      <dl className={styles.defs}>
        <dt>Proposed</dt>
        <dd>
          <LocalTime iso={data.created_at} />
        </dd>
        <dt>Kind</dt>
        <dd>{KIND_LABELS[data.kind]}</dd>
        {data.action_type !== null && (
          <>
            <dt>Action</dt>
            <dd>{ACTION_TYPE_LABELS[data.action_type] ?? data.action_type}</dd>
          </>
        )}
        <dt>Cost</dt>
        <dd>{usd(data.cost_usd)}</dd>
        {data.model_calls.length > 0 && (
          <>
            <dt>Model</dt>
            <dd>{data.model_calls.map((call) => call.task).join(", ")}</dd>
          </>
        )}
        {to !== null && (
          <>
            <dt>To</dt>
            <dd>{to} (you typed)</dd>
          </>
        )}
        {data.policy_decision !== null && (
          <>
            <dt>Policy decision</dt>
            <dd>{POLICY_DECISION_LABELS[data.policy_decision] ?? data.policy_decision}</dd>
          </>
        )}
        <dt>Outcome</dt>
        <dd>
          {ACTION_STATUS_LABELS[data.status] ?? data.status}
          {data.outcome_code !== null && `: ${reasonText(data.outcome_code)}`}
        </dd>
      </dl>
      {data.payload !== null && typeof data.payload.body === "string" && (
        <details>
          <summary>Show the draft</summary>
          {typeof data.payload.subject === "string" && <p className="t-card">{data.payload.subject}</p>}
          <p className={styles.draft}>{data.payload.body}</p>
        </details>
      )}
      {data.checks.length > 0 && (
        <section aria-labelledby="checks-heading" className={styles.stack}>
          <h3 id="checks-heading" className="t-card">
            Guardian checks
          </h3>
          <ul className={styles.list}>
            {data.checks.map((check) => (
              <li key={check.id}>
                <strong>{CHECK_KIND_LABELS[check.check] ?? check.check}: {check.passed ? "passed" : "blocked"}</strong>
                {!check.passed && check.reason_code !== null && <> {reasonText(check.reason_code)}</>}
              </li>
            ))}
          </ul>
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

/**
 * The flight recorder (GRD-03, FRONTEND_UI 3.19): the campaign's steps in order, one selected in `?step=`, with first, previous,
 * next and last. A blocked row carries a filled Blocked pill and the reason in words. On a phone the list is the first screen.
 */
export function RecorderView({ campaignId }: { campaignId: string }) {
  const api = useApi();
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const rows = useQuery(recorderQuery(api, campaignId));
  if (rows.isPending) return <p role="status">Loading the flight recorder...</p>;
  if (rows.isError) return <ErrorState what="Could not load the flight recorder." error={rows.error} onRetry={() => void rows.refetch()} />;
  const list = rows.data;
  if (list.length === 0) return <p className="muted">Nothing was recorded for this campaign yet.</p>;

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
                  <span className={styles.row}>
                    <span className="t-card">
                      {n + 1}. {AGENT_ROLE_LABELS[row.agent_role] ?? row.agent_role}
                    </span>
                    <span className="t-small muted">{KIND_LABELS[row.kind]}</span>
                    {row.status === "blocked" && <span className={styles.blocked}>Blocked</span>}
                  </span>
                  <span className="t-small">{row.summary}</span>
                  {reason !== null && <span className="t-small">{reasonText(reason)}</span>}
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
  );
}
