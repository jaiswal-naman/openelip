"use client";

import { useQuery } from "@tanstack/react-query";
import ledger from "@/components/ui/ledger.module.css";
import { AiOffNotice, ErrorState } from "@/components/ui/States";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { useApi } from "@/lib/api/context";
import type { Subprocessor } from "@/lib/api/types";
import { subprocessorsQuery } from "./queries";
import styles from "./trust.module.css";

const KIND_LABELS: Record<string, string> = { model_provider: "Model provider", email: "Email host" };

const kindLabel = (kind: string) => KIND_LABELS[kind] ?? kind; // a kind this page does not know is shown as it came

/** The title block: what this page is, and how many of each kind receive data, counted from the list the API sent. */
function Head({ items }: { items?: Subprocessor[] }) {
  const cells: TitleCell[] = [];
  if (items !== undefined) {
    const providers = items.filter((item) => item.kind === "model_provider").length;
    const emailHosts = items.filter((item) => item.kind === "email").length;
    cells.push({ label: "Model providers", value: providers, tone: providers === 0 ? "muted" : undefined });
    cells.push({ label: "Email hosts", value: emailHosts, tone: emailHosts === 0 ? "muted" : undefined });
  }
  return (
    <TitleBlock
      title="Trust"
      lines={<p>The model providers and other services this server sends data to, read from its running configuration.</p>}
      cells={cells}
    />
  );
}

/**
 * Who receives data from this server (TRU-04, PLT-05). The API builds the list from the running configuration, so a
 * model provider appears only when a task would really be sent to it, and with no model key none is listed.
 */
export function TrustView() {
  const api = useApi();
  const list = useQuery(subprocessorsQuery(api));

  if (list.isError) {
    return (
      <>
        <Head />
        <ErrorState what="Could not load the list of providers." error={list.error} onRetry={() => void list.refetch()} />
      </>
    );
  }
  if (list.data === undefined) {
    return (
      <>
        <Head />
        <div className={styles.skeleton} role="status" aria-label="Loading the list of providers" />
      </>
    );
  }

  const { items, outside_copies_statement } = list.data;
  const hasModelProvider = items.some((item) => item.kind === "model_provider");

  return (
    <>
      <Head items={items} />
      <div className={styles.region}>
        {!hasModelProvider && (
          <AiOffNotice>
            No model provider is configured on this server, so nothing is sent to one. AI features are not set up. Search and
            everything already saved still work.
          </AiOffNotice>
        )}

        <table className={`${ledger.table} ${styles.table}`}>
          <caption className="sr-only">Providers and sub-processors this server sends data to</caption>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Purpose</th>
              <th scope="col">Data sent</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <ProcessorRow key={`${item.kind}:${item.name}`} item={item} />
            ))}
          </tbody>
        </table>

        <p className={styles.statement}>{outside_copies_statement}</p>
      </div>
    </>
  );
}

function ProcessorRow({ item }: { item: Subprocessor }) {
  return (
    <tr>
      <th scope="row">
        <span className={styles.name}>{item.name}</span>
        <span className={`t-label ${styles.kind}`}>{kindLabel(item.kind)}</span>
      </th>
      <td data-label="Purpose">
        {item.purpose}
        {item.tasks.length > 0 && <span className={`t-data ${styles.tasks}`}>Tasks: {item.tasks.join(", ")}</span>}
      </td>
      <td data-label="Data sent">{item.receives}</td>
    </tr>
  );
}
