import type { RecorderRow } from "@/features/recorder/queries";

/** What the flight recorder holds about a campaign's packet: when the person approved it, and how the Guardian's checks came out. */
export type PacketRecord = { approvedAt: string | null; checks: { passed: number; total: number } | null };

/**
 * Read from the recorder's own rows, never guessed: the packet's outside action is the one that submits the application. A
 * check kind counts as passed only if every verdict of that kind passed. No such row, or no decision stored, gives null.
 */
export function packetRecord(rows: RecorderRow[] | undefined): PacketRecord | null {
  const row = [...(rows ?? [])].reverse().find((item) => item.kind === "outside_action" && item.action_type === "submit_application");
  if (row === undefined) return null;
  const kinds = new Map<string, boolean>();
  for (const check of row.checks) kinds.set(check.check, (kinds.get(check.check) ?? true) && check.passed);
  const passed = [...kinds.values()].filter(Boolean).length;
  return {
    approvedAt: row.approval === "approved" ? row.decided_at : null,
    checks: kinds.size === 0 ? null : { passed, total: kinds.size },
  };
}
