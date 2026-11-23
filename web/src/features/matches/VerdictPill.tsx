import { Pill } from "@/components/ui/Pill";
import { VERDICT_LABELS } from "@/lib/labels";
import type { EvidenceItem } from "./queries";

// FRONTEND_UI 6.2: met a check, partly met a half circle, not met a cross, no evidence a question mark.
const TONES = { met: "ok", partly_met: "warn", not_met: "bad", no_evidence: "unk" } as const;

/** A requirement's verdict as words and an icon, never colour alone. */
export function VerdictPill({ verdict }: { verdict: EvidenceItem["verdict"] }) {
  return <Pill tone={TONES[verdict]}>{VERDICT_LABELS[verdict]}</Pill>;
}
