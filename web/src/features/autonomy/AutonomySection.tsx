"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Label, Radio, RadioGroup } from "react-aria-components";
import { ErrorState } from "@/components/ui/States";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { useApi } from "@/lib/api/context";
import { isFailure } from "@/lib/api/errors";
import { describeFailure } from "@/lib/api/problems";
import { AUTONOMY_LEVEL_LABELS, AUTONOMY_ROW_LABELS } from "@/lib/labels";
import { autonomyQuery, useSaveAutonomy } from "./queries";
import styles from "./autonomy.module.css";

const SAVED = "Saved. Applies to new actions from now.";

/**
 * The Autonomy section of Settings (GRD-01, FRONTEND_UI 3.10 and 4.4): one row per kind of outside action. A row offers only
 * the levels the server allows; a row with one level is shown as text with its reason under it. The page never decides what
 * is allowed: the server refuses anything else, and a refusal puts the row back and says why. Profile sharing is a fixed line:
 * no agent shares the profile.
 */
export function AutonomySection() {
  const api = useApi();
  const autonomy = useQuery(autonomyQuery(api));
  const save = useSaveAutonomy();
  const [message, setMessage] = useState<{ row: string; text: string; ok: boolean } | null>(null);
  const [days, setDays] = useState<string | null>(null);

  if (autonomy.isPending) return <p role="status">Loading your autonomy settings...</p>;
  if (autonomy.isError) return <ErrorState what="Could not load your autonomy settings." error={autonomy.error} onRetry={() => void autonomy.refetch()} />;
  const data = autonomy.data;

  function change(type: string, level: string) {
    setMessage(null);
    save.mutate(
      { version: data.version, actions: { [type]: { level } } },
      {
        onSuccess: () => setMessage({ row: type, text: SAVED, ok: true }),
        onError: (error) => {
          if (isFailure(error, "autonomy_not_allowed")) setMessage({ row: type, text: `${describeFailure(error)} Your choice was put back.`, ok: false });
          else if (isFailure(error, "version_conflict") || error instanceof Error) setMessage({ row: type, text: `${describeFailure(error)} Your choice was not saved.`, ok: false });
        },
      },
    );
  }

  function saveWindow() {
    const minutes = Number(days ?? data.undo_window_minutes);
    setMessage(null);
    save.mutate(
      { version: data.version, undo_window_minutes: minutes },
      {
        onSuccess: () => setMessage({ row: "undo", text: SAVED, ok: true }),
        onError: (error) => setMessage({ row: "undo", text: `${describeFailure(error)} Your choice was not saved.`, ok: false }),
      },
    );
  }

  return (
    <section aria-labelledby="autonomy-heading" className={styles.section}>
      <h2 id="autonomy-heading" className="t-section">
        Autonomy
      </h2>
      <p className={`muted ${styles.lead}`}>How much your agents may do before they ask you. Follow-ups and applications are never sent without your approval.</p>
      <ul className={styles.rows}>
        {Object.entries(data.actions).map(([type, row]) => {
          const label = AUTONOMY_ROW_LABELS[type] ?? { name: type, locked: "This cannot be changed." };
          const locked = row.allowed_levels.length <= 1;
          return (
            <li key={type} className={styles.row}>
              {locked ? (
                <>
                  <p className="t-card">{label.name}</p>
                  <p>{AUTONOMY_LEVEL_LABELS[row.level] ?? row.level}</p>
                  <p className="t-small muted">{label.locked}</p>
                </>
              ) : (
                <RadioGroup value={row.level} onChange={(level) => change(type, level)} isDisabled={save.isPending} className={styles.group}>
                  <Label className="t-card">{label.name}</Label>
                  <div className={styles.segmented}>
                    {row.allowed_levels.map((level) => (
                      <Radio key={level} value={level} className={styles.segment}>
                        {AUTONOMY_LEVEL_LABELS[level] ?? level}
                      </Radio>
                    ))}
                  </div>
                  {!row.allowed_levels.includes("tell_me_after") && <p className="t-small muted">{label.locked}</p>}
                </RadioGroup>
              )}
              {message !== null && message.row === type && (
                <p role={message.ok ? "status" : "alert"} className={message.ok ? undefined : styles.problem}>
                  {message.text}
                </p>
              )}
            </li>
          );
        })}
        <li className={styles.row}>
          <p className="t-card">Share my profile</p>
          <p>Only when you accept an introduction</p>
          <p className="t-small muted">No agent shares your profile or your address on its own.</p>
        </li>
      </ul>
      <div className={styles.window}>
        <label className={field.field}>
          <span className={field.label}>Undo window for follow-ups (minutes, 1 to 60)</span>
          <input
            className={`${field.input} ${styles.minutes}`}
            type="number"
            min={1}
            max={60}
            value={days ?? String(data.undo_window_minutes)}
            onChange={(event) => setDays(event.target.value)}
          />
        </label>
        <div className={styles.buttons}>
          <Button isPending={save.isPending} onPress={saveWindow}>
            Save undo window
          </Button>
        </div>
        {message !== null && message.row === "undo" && (
          <p role={message.ok ? "status" : "alert"} className={message.ok ? undefined : styles.problem}>
            {message.text}
          </p>
        )}
        <p>
          Default budget for a new campaign: USD {data.campaign_budget_default_usd}. <span className="t-small muted">Set by the server; each campaign can raise its own.</span>
        </p>
      </div>
    </section>
  );
}
