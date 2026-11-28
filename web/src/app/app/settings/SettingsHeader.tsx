"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { useMe } from "@/features/auth/queries";
import { autonomyQuery } from "@/features/autonomy/queries";
import { useAiConsent } from "@/features/consent/useAiConsent";
import { useApi } from "@/lib/api/context";
import { AUTONOMY_LEVEL_LABELS, BRIEF_FREQUENCY_LABELS } from "@/lib/labels";

/**
 * The settings page header, and the jump to the part an address asked for (?section=). Every cell is read from what
 * the sections below already load; a cell whose answer has not arrived yet is left out.
 */
export function SettingsHeader({ jumpTo }: { jumpTo: string | null }) {
  const api = useApi();
  const me = useMe();
  const ai = useAiConsent();
  const autonomy = useQuery(autonomyQuery(api));

  // A link such as "Review and agree" names a part of the page; the settings are one page, so this scrolls to it.
  useEffect(() => {
    if (jumpTo !== null) document.getElementById(jumpTo)?.scrollIntoView();
  }, [jumpTo]);

  const cells: TitleCell[] = [];
  if (ai.gate === "ready") cells.push({ label: "AI processing", value: "On", tone: "ok" });
  else if (ai.gate === "consent_required") cells.push({ label: "AI processing", value: "Off", tone: "muted" });
  else if (ai.gate === "providers_changed") cells.push({ label: "AI processing", value: "Agree again", tone: "redline" });
  else if (ai.gate === "ai_disabled") cells.push({ label: "AI processing", value: "Not set up", tone: "muted" });

  if (me.data) {
    const emailed = me.data.consents.some((consent) => consent.purpose === "brief_email" && consent.granted);
    const frequency = BRIEF_FREQUENCY_LABELS[me.data.brief.frequency];
    cells.push({ label: "Morning brief", value: me.data.brief.frequency === "paused" ? frequency : `${frequency}, ${emailed ? "emailed" : "not emailed"}` });
  }

  if (autonomy.data) {
    const levels = new Set(Object.values(autonomy.data.actions).map((action) => action.level));
    const [only] = levels;
    cells.push({ label: "Autonomy", value: levels.size === 1 ? (AUTONOMY_LEVEL_LABELS[only] ?? only) : "Set per action" });
  }

  return (
    <TitleBlock
      title="Settings"
      lines={<p>Your AI consent, your morning brief, what your agents may do on their own, and your data.</p>}
      cells={cells}
    />
  );
}
