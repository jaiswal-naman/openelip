"use client";

import { useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { ExternalLinkIcon } from "@/components/icons/icons";
import { buttonClass } from "@/components/ui/buttonStyles";
import { type TitleCell, TitleBlock } from "@/components/ui/TitleBlock";
import { LocalTime } from "@/components/ui/Viewer";
import { EMPLOYMENT_TYPE_LABELS, NOT_STATED } from "@/lib/labels";
import { useViewer } from "@/lib/viewer";
import { citationResolves, HighlightedText } from "@/ui/patterns/HighlightedText";
import styles from "./detail.module.css";
import { arrangementsText, locationText, payText, safeHttpUrl, sourceLabel, unplacedWords } from "./jobs";
import { RequirementList, requirementNumbers } from "./RequirementList";
import type { JobDetail as JobDetailData } from "./types";

const ORDINAL = /^\d{1,5}$/; // an ordinal is a smallint: anything else in ?req= is not one

/**
 * One job: its facts, the requirements the posting states, and the posting text with the selected requirement's exact
 * words marked (IDX-05, IDX-06, IDX-08). The selected requirement is `?req=<ordinal>` in the address, so a reload or a
 * shared link lands on the same highlight. All posting text is shown as text, never as markup (threat T-02-34).
 */
export function JobDetail({ job }: { job: JobDetailData }) {
  const viewer = useViewer();
  const search = useSearchParams();
  const points = useMemo(() => Array.from(job.description), [job.description]);
  // A requirement is shown as highlightable only while its words are found at its span in this text.
  const resolved = useMemo(
    () => new Set(job.requirements.filter((r) => citationResolves(points, r.span, r.text)).map((r) => r.ordinal)),
    [job.requirements, points],
  );
  const [selected, setSelected] = useState<number | null>(() => {
    const asked = search.get("req");
    return asked !== null && ORDINAL.test(asked) ? Number(asked) : null;
  });

  // What is marked is only ever a requirement that exists and whose citation resolves. An ordinal in the address that
  // matches none, or one that is withheld, marks nothing (threat T-02-37).
  const active = selected !== null && resolved.has(selected) ? job.requirements.find((r) => r.ordinal === selected) : undefined;
  const extracted = job.requirements_version === job.content_version;
  const closed = job.status === "closed";
  const applyUrl = safeHttpUrl(job.apply_url);
  const words = unplacedWords(job);
  const numbers = requirementNumbers(job.requirements);

  function select(ordinal: number | null) {
    setSelected(ordinal);
    // The address is state a person may reload or share (FRONTEND_UI 9.4). It is written with the history API, which
    // Next.js folds into its router: no request to the server, no scroll to the top, and the focus stays on the button.
    const url = new URL(window.location.href);
    if (ordinal === null) url.searchParams.delete("req");
    else url.searchParams.set("req", String(ordinal));
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }

  const shown = extracted && job.requirements.length > 0;
  const cells: TitleCell[] = [];
  if (shown) {
    const required = job.requirements.filter((requirement) => requirement.importance === "required").length;
    cells.push({ label: "Required", value: required });
    cells.push({ label: "Preferred", value: job.requirements.length - required });
  }

  return (
    <article className={styles.detail}>
      <TitleBlock
        title={<span className={styles.title}>{job.title}</span>}
        lines={
          <>
            <p className={styles.company}>{job.company_name.trim() === "" ? "Company not stated" : job.company_name}</p>
            <p className={styles.place}>{locationText(job)}</p>
          </>
        }
        cells={cells}
        actions={
          !closed &&
          (applyUrl === null ? (
            <p className="t-small">No link in the posting</p>
          ) : (
            <a
              href={applyUrl}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className={buttonClass("primary", "md")}
              aria-label={`View posting for ${job.title} (opens in a new tab)`}
            >
              View posting
              <ExternalLinkIcon />
            </a>
          ))
        }
      />

      {closed && (
        <p className={styles.closed}>
          <span className={`t-label ${styles.closedLabel}`}>Closed</span>
          <span>
            {job.closed_at === null ? (
              "This job has closed. It is kept here for reference."
            ) : (
              <>
                This job closed on <LocalTime iso={job.closed_at} />. It is kept here for reference.
              </>
            )}
          </span>
        </p>
      )}

      <ul className={styles.facts}>
        {words !== null && <li>Posting says: {words}</li>}
        <li>{arrangementsText(job.work_arrangements)}</li>
        <li>Employment type: {EMPLOYMENT_TYPE_LABELS[job.employment_type] ?? job.employment_type}</li>
        <li>Pay: {payText(job.pay, viewer.locale)}</li>
        <li>Source: {sourceLabel(job.source.ats)}</li>
        <li>
          Verified open <LocalTime iso={job.last_verified_at} />
        </li>
      </ul>

      <div className={`${styles.body} ${shown ? styles.split : ""}`}>
        <section aria-labelledby="requirements-heading" className={`${styles.panel} ${styles.requirements}`}>
          <h2 id="requirements-heading" className="t-section">
            Requirements
          </h2>
          {!extracted ? (
            <p>Requirements have not been extracted for this version yet.</p>
          ) : job.requirements.length === 0 ? (
            <p>No requirements were found in this posting.</p>
          ) : (
            <RequirementList requirements={job.requirements} resolved={resolved} selected={selected} onSelect={select} />
          )}
        </section>

        <section aria-labelledby="description-heading" className={styles.panel}>
          <h2 id="description-heading" className="t-section">
            Job description
          </h2>
          {job.description.trim() === "" ? (
            <p>{NOT_STATED}</p>
          ) : (
            <div className={styles.posting}>
              <HighlightedText
                text={job.description}
                span={active?.span ?? null}
                callout={active === undefined ? undefined : numbers.get(active.ordinal)}
                announcement="Job sentence highlighted."
              />
            </div>
          )}
        </section>
      </div>
    </article>
  );
}
