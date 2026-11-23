"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import ledger from "@/components/ui/ledger.module.css";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { Count } from "@/components/ui/Viewer";
import { useMe } from "@/features/auth/queries";
import { useApi } from "@/lib/api/context";
import { isFailure } from "@/lib/api/errors";
import type { Costs } from "@/lib/api/types";
import { formatLatency, formatMoney, formatMonth, formatPercent } from "@/lib/format";
import { useViewer } from "@/lib/viewer";
import styles from "./costs.module.css";
import { costsQuery } from "./queries";
import { ShareBar } from "./ShareBar";

const SIGN_IN = `/sign-in?next=${encodeURIComponent("/control/costs")}`;

/**
 * The signed-in person's own model cost, by month and by task (GRD-05, PLT-04). Every figure is read from the cost
 * ledger and says so: it is measured operational data, not a quality result (invariant 8).
 */
export function CostsView() {
  const api = useApi();
  const router = useRouter();
  const me = useMe();
  const byMonth = useQuery(costsQuery(api, "month"));
  const byTask = useQuery(costsQuery(api, "task"));

  // A session that ended while the page was open: back to sign-in, and return here afterwards.
  const sessionEnded = isFailure(byMonth.error, "unauthenticated") || isFailure(byTask.error, "unauthenticated");
  useEffect(() => {
    if (sessionEnded) router.replace(SIGN_IN);
  }, [sessionEnded, router]);

  const failed = byMonth.error ?? byTask.error;
  const retry = () => {
    void byMonth.refetch();
    void byTask.refetch();
  };

  return (
    <>
      <div className={styles.head}>
        <div className={styles.title}>
          <h1 className="t-page">My model cost</h1>
          <p className="muted">Measured operational data from the cost ledger.</p>
        </div>
        {byMonth.data && <Total costs={byMonth.data} />}
      </div>

      {sessionEnded ? (
        <p role="status">Your session has ended. Taking you to sign in.</p>
      ) : failed ? (
        <ErrorState what="Could not load your model costs." error={failed} onRetry={retry} />
      ) : !byMonth.data || !byTask.data ? (
        <p role="status" className="muted">
          Loading your model costs.
        </p>
      ) : byMonth.data.model_calls === 0 ? (
        <EmptyState>
          No model calls have been recorded for your account yet. A row appears here after the first one.{" "}
          <span className={styles.hint}>
            To record one now, the person who runs this server can run{" "}
            <code>python -m app.platform.llm ping --email {me.data?.email ?? "you@example.com"}</code>.
          </span>
        </EmptyState>
      ) : (
        <>
          <CostTable title="By month" keyLabel="Month" costs={byMonth.data} />
          <CostTable title="By task" keyLabel="Task" costs={byTask.data} />
        </>
      )}
    </>
  );
}

function Total({ costs }: { costs: Costs }) {
  const { locale } = useViewer();
  return (
    <p className={styles.total}>
      <span className="t-label muted">Total, all recorded calls</span>
      <span className={styles.totalFigure}>{formatMoney(costs.total_usd, locale)}</span>
      <span className="t-data muted">
        <Count value={costs.model_calls} /> model {costs.model_calls === 1 ? "call" : "calls"}
      </span>
    </p>
  );
}

function CostTable({ title, keyLabel, costs }: { title: string; keyLabel: string; costs: Costs }) {
  const { locale } = useViewer();
  const total = Number(costs.total_usd);
  const id = `costs-${costs.group_by}`;
  return (
    <section className={styles.section} aria-labelledby={id}>
      <h2 id={id} className="t-section">
        {title}
      </h2>
      <table className={ledger.table}>
        <caption className="sr-only">Model cost {title.toLowerCase()}, from the cost ledger</caption>
        <thead>
          <tr>
            <th scope="col">{keyLabel}</th>
            <th scope="col">Models</th>
            <th scope="col" className={ledger.num}>
              Calls
            </th>
            <th scope="col" className={ledger.num}>
              Average latency
            </th>
            <th scope="col" className={ledger.num}>
              Tokens in / out
            </th>
            <th scope="col" className={ledger.num}>
              Cost
            </th>
            <th scope="col">Share of total</th>
          </tr>
        </thead>
        <tbody>
          {costs.rows.map((row) => {
            // With no cost at all there is nothing to take a share of, so no bar and no percentage
            const share = total > 0 ? Number(row.cost_usd) / total : null;
            const key = costs.group_by === "month" ? (row.period ? formatMonth(row.period, locale) : "Unknown") : (row.task ?? "Unknown");
            return (
              <tr key={row.period ?? row.task ?? "unknown"}>
                <th scope="row">{key}</th>
                <td className={`t-data ${styles.models}`} data-label="Models">
                  {row.models.join(", ")}
                </td>
                <td className={ledger.num} data-label="Calls">
                  <Count value={row.calls} />
                </td>
                <td className={ledger.num} data-label="Average latency">
                  {formatLatency(row.avg_latency_ms, locale)}
                </td>
                <td className={ledger.num} data-label="Tokens in / out">
                  <Count value={row.input_tokens} /> / <Count value={row.output_tokens} />
                </td>
                <td className={ledger.num} data-label="Cost">
                  {formatMoney(row.cost_usd, locale)}
                </td>
                <td data-label="Share of total">
                  {share === null ? (
                    <span className="t-data muted">No cost recorded</span>
                  ) : (
                    <span className={styles.share}>
                      <ShareBar fraction={share} />
                      <span className={`t-data ${styles.percent}`}>{formatPercent(share, locale)}</span>
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
