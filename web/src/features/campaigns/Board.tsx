"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { applicationsQuery } from "@/features/applications/queries";
import { useApi } from "@/lib/api/context";
import { CAMPAIGN_STAGE_LABELS, type CampaignStage } from "@/lib/labels";
import { CampaignCard } from "./CampaignCard";
import { boardQuery } from "./queries";
import styles from "./campaigns.module.css";


/**
 * The board (FRONTEND_UI 3.6): six columns by stage, a seventh for closed ones when asked. "Show closed" and the filter are
 * URL state. On a phone one column shows at a time, chosen from a list; on a wider screen all of them show. Each card with
 * an application also carries the Phase 5 "Record status..." menu.
 */
export function Board() {
  const api = useApi();
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const showClosed = params.get("closed") === "1";
  const filter = params.get("filter") === "needs-you" ? "needs-you" : "all";
  const board = useQuery(boardQuery(api, showClosed));
  const applications = useQuery(applicationsQuery(api));
  const [phoneStage, setPhoneStage] = useState<CampaignStage>("researching");

  function setParam(name: string, value: string | null) {
    const next = new URLSearchParams(params.toString());
    if (value === null) next.delete(name);
    else next.set(name, value);
    const query = next.toString();
    router.replace(query === "" ? path : `${path}?${query}`, { scroll: false });
  }

  if (board.isPending) return <p role="status">Loading your campaigns...</p>;
  if (board.isError) return <ErrorState what="Could not load your campaigns." error={board.error} onRetry={() => void board.refetch()} />;

  const columns = board.data.columns;
  const total = columns.reduce((sum, column) => sum + column.campaigns.length, 0);
  const applicationFor = (title: string, company: string) => {
    const found = (applications.data ?? []).filter((a) => a.job_title === title && a.company_name === company);
    return found.length === 1 ? found[0].id : null;
  };

  return (
    <section aria-labelledby="board-heading" className={styles.page}>
      <h2 id="board-heading" className="t-section">
        Campaigns
      </h2>
      <div className={styles.toolbar}>
        <label className={styles.row}>
          <span>Filter</span>
          <select className={styles.select} value={filter} onChange={(event) => setParam("filter", event.target.value === "all" ? null : event.target.value)}>
            <option value="all">All</option>
            <option value="needs-you">Needs you</option>
          </select>
        </label>
        <label className={styles.row}>
          <input type="checkbox" checked={showClosed} onChange={(event) => setParam("closed", event.target.checked ? "1" : null)} />
          <span>Show closed</span>
        </label>
        <label className={`${styles.row} ${styles.phoneOnly}`}>
          <span>Stage</span>
          <select className={styles.select} value={phoneStage} onChange={(event) => setPhoneStage(event.target.value as CampaignStage)}>
            {columns.map((column) => (
              <option key={column.stage} value={column.stage}>
                {CAMPAIGN_STAGE_LABELS[column.stage]} ({column.campaigns.length})
              </option>
            ))}
          </select>
        </label>
      </div>
      {total === 0 ? (
        <EmptyState>No campaigns yet. Open a match and press Start campaign, and the agents research the company and prepare your application for your approval.</EmptyState>
      ) : (
        <div className={styles.columns}>
          {columns.map((column) => {
            const cards = column.campaigns.filter((card) => filter === "all" || card.pending_approvals > 0);
            return (
              <section
                key={column.stage}
                aria-labelledby={`col-${column.stage}`}
                className={`${styles.column} ${column.stage === phoneStage ? "" : styles.hiddenOnPhone}`}
              >
                <div className={styles.columnHead}>
                  <h3 id={`col-${column.stage}`} className="t-card">
                    {CAMPAIGN_STAGE_LABELS[column.stage]}
                  </h3>
                  <span className="muted">{column.total ?? cards.length}</span>
                </div>
                {cards.length === 0 ? (
                  <p className="t-small muted">Nothing here.</p>
                ) : (
                  <ul className={styles.list}>
                    {cards.map((card) => (
                      <li key={card.id}>
                        <CampaignCard card={card} stage={column.stage} applicationId={applicationFor(card.title, card.company_name)} />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}
