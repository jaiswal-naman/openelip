"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { buttonClass } from "@/components/ui/buttonStyles";
import { ErrorState } from "@/components/ui/States";
import { LocalTime } from "@/components/ui/Viewer";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { requestExport, requestQuery } from "./api";
import styles from "./privacy.module.css";

const POLL_MS = 5000; // the API asks for 5 seconds between polls (Retry-After on an unfinished request)

/**
 * Export all data (TRU-01): ask for the archive, watch it being made, then a link to download it. The archive is built by
 * the worker and kept for 7 days. The request is remembered only on this page: nothing is stored in the browser, and
 * asking again while one is being made returns that one. The download is the API's own owner-only route.
 */
export function ExportCard({ pollMs = POLL_MS }: { pollMs?: number }) {
  const api = useApi();
  const [requestId, setRequestId] = useState<string | null>(null);
  const start = useMutation({ mutationFn: () => requestExport(api), onSuccess: (request) => setRequestId(request.id) });
  const request = useQuery({
    ...requestQuery(api, requestId ?? ""),
    enabled: requestId !== null,
    // keep asking until it is read or has failed; a call that itself failed is shown with Retry, not hammered every few seconds
    refetchInterval: (query) => (query.state.status === "error" || ["succeeded", "failed"].includes(query.state.data?.status ?? "") ? false : pollMs),
  });

  const status = request.data?.status;
  const download = request.data?.download ?? null;
  const working = requestId !== null && status !== "succeeded" && status !== "failed" && !request.isError;

  return (
    <section className={styles.card} aria-labelledby="export-title">
      <h2 id="export-title" className="t-section">
        Export your data
      </h2>
      <p className={styles.lead}>
        Download everything this server holds about you as one ZIP file: your account, record, documents, consents and the log of
        what was done with them. The file is kept for 7 days.
      </p>

      {request.isError ? (
        <ErrorState what="Could not check your export." error={request.error} onRetry={() => void request.refetch()} />
      ) : working ? (
        <p role="status">{status === "running" ? "Preparing your export. This can take a minute." : "Your export is queued."}</p>
      ) : status === "succeeded" && download ? (
        <div className={styles.stack}>
          <a className={buttonClass("primary")} href={download.path} download>
            Download your data (ZIP)
          </a>
          <p className="muted">
            Available until <LocalTime iso={download.available_until} />. After that it is deleted and you can ask for a new one.
          </p>
        </div>
      ) : (
        <div className={styles.stack}>
          {status === "succeeded" && <p role="status">This export has expired. Ask for a new one.</p>}
          {status === "failed" && <p role="alert" className={styles.problem}>The export failed. Nothing was lost. Try again.</p>}
          {start.isError && (
            <p role="alert" className={styles.problem}>
              {describeFailure(start.error)}
            </p>
          )}
          <div>
            <Button isPending={start.isPending} onPress={() => start.mutate()}>
              {status === undefined ? "Export all data" : "Export again"}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
