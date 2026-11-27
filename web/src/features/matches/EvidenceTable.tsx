"use client";

import { Cell, Column, Row, type Selection, Table, TableBody, TableHeader } from "react-aria-components";
import numeral from "@/components/ui/numeral.module.css";
import ledger from "@/components/ui/ledger.module.css";
import { IMPORTANCE_LABELS } from "@/lib/labels";
import type { EvidenceItem } from "./queries";
import styles from "./match.module.css";
import { VerdictPill } from "./VerdictPill";

/** The one row selected, or null. A React Aria selection is "all" or a set of keys; a single-selection table has at most one. */
function selectedId(keys: Selection): number | null {
  if (keys === "all") return null;
  const [first] = [...keys];
  return first === undefined ? null : Number(first);
}

/**
 * Every requirement of the job with its verdict and the record evidence for it (MAT-03), as a hairline ledger: requirement,
 * verdict chip, evidence. It is a React Aria Table with single row selection: arrow keys move between rows, Enter or Space
 * selects, pressing the selected row again (or Escape) clears it. Selecting is only a message to the parent, which marks the
 * cited words in the two texts (MAT-04); this table decides nothing about the evidence itself. The row's number is the
 * numeral the marked passage carries in the texts below.
 */
export function EvidenceTable({
  evidence,
  selected,
  onSelect,
}: {
  evidence: EvidenceItem[];
  selected: number | null;
  /** The requirement's id, or null when the selection is cleared. */
  onSelect: (requirementId: number | null) => void;
}) {
  return (
    <Table
      aria-label="Requirements and your evidence"
      className={`${ledger.table} ${styles.table}`}
      selectionMode="single"
      selectedKeys={selected === null ? [] : [String(selected)]}
      onSelectionChange={(keys) => onSelect(selectedId(keys))}
    >
      <TableHeader>
        <Column isRowHeader>Requirement</Column>
        <Column>Verdict</Column>
        <Column>Your evidence</Column>
      </TableHeader>
      <TableBody>
        {evidence.map((item, index) => (
          <Row key={item.requirement.id} id={String(item.requirement.id)} textValue={item.requirement.text} className={styles.row}>
            <Cell>
              <span className={numeral.callout}>
                <span className={`${numeral.numeral} ${styles.itemNo}`} aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className={styles.requirementText}>
                  <span className={`t-label ${styles.importance} ${styles[item.requirement.importance]}`}>{IMPORTANCE_LABELS[item.requirement.importance]}</span>
                  <span className={styles.words}>{item.requirement.text}</span>
                </span>
              </span>
            </Cell>
            <Cell data-label="Verdict">
              <VerdictPill verdict={item.verdict} />
            </Cell>
            <Cell data-label="Your evidence">
              <EvidenceCell item={item} />
            </Cell>
          </Row>
        ))}
      </TableBody>
    </Table>
  );
}

/** What the record says for one requirement: each cited fact's own words and where it came from, or that it is silent. */
function EvidenceCell({ item }: { item: EvidenceItem }) {
  if (item.verdict === "no_evidence") return <span className={styles.noEvidence}>No evidence in your record</span>;
  if (item.record_citations.length === 0) return <span className="muted">No source was recorded</span>;
  return (
    <ul className={styles.citations}>
      {item.record_citations.map((citation) => (
        <li key={citation.fact_id}>
          <span className={styles.words}>{`“${citation.quote}”`}</span>
          <span className={`t-small muted ${styles.source}`}>
            {citation.source === null ? "Added by you" : "From your resume"}
            {!citation.is_current && ", since changed in your record"}
          </span>
        </li>
      ))}
    </ul>
  );
}
