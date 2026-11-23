"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { useApi } from "@/lib/api/context";
import { describeFailure } from "@/lib/api/problems";
import { confirmProposal, declineProposal, type Proposal } from "./queries";
import styles from "./chat.module.css";

/** What a stored record row says, in a line: its statement, else its value, else its fields. Shown as text only. */
function summary(value: Record<string, unknown> | null): string {
  if (value === null) return "Nothing";
  if (typeof value.statement === "string") return value.statement;
  if (typeof value.value === "string" || typeof value.value === "number") return String(value.value);
  return Object.entries(value)
    .filter(([, field]) => typeof field === "string" || typeof field === "number" || typeof field === "boolean")
    .map(([name, field]) => `${name}: ${String(field)}`)
    .join(", ");
}

/**
 * A suggested change to the record (CHT-03): the old value, the new value and where it came from. Nothing changes until the
 * person presses Confirm; the buttons call the same routes the Career Record screen uses.
 */
export function ProposalCard({ proposal }: { proposal: Proposal }) {
  const api = useApi();
  const [state, setState] = useState<"open" | "pending" | "confirmed" | "declined">("open");
  const [problem, setProblem] = useState<string | null>(null);

  async function answer(kind: "confirm" | "decline") {
    setState("pending");
    setProblem(null);
    try {
      await (kind === "confirm" ? confirmProposal(api, proposal.event_id) : declineProposal(api, proposal.event_id));
      setState(kind === "confirm" ? "confirmed" : "declined");
    } catch (error) {
      setState("open");
      setProblem(describeFailure(error));
    }
  }

  return (
    <section className={styles.proposal} aria-label="Suggested change to your record">
      <p className="t-card">Suggested change to your record</p>
      <p>{proposal.statement}</p>
      <dl className={styles.values}>
        <dt>Now</dt>
        <dd>{summary(proposal.before)}</dd>
        <dt>Would become</dt>
        <dd>{summary(proposal.after)}</dd>
      </dl>
      <p className="t-small muted">Source: what you said just now</p>
      {state === "confirmed" || state === "declined" ? (
        <p role="status" className="t-card">
          {state === "confirmed" ? "Confirmed" : "Declined"}
        </p>
      ) : (
        <div className={styles.buttons}>
          <Button variant="primary" size="sm" isPending={state === "pending"} onPress={() => void answer("confirm")}>
            Confirm
          </Button>
          <Button size="sm" isPending={state === "pending"} onPress={() => void answer("decline")}>
            Decline
          </Button>
        </div>
      )}
      {problem && (
        <p role="alert" className={styles.problem}>
          {problem} Your record was not changed.
        </p>
      )}
    </section>
  );
}
