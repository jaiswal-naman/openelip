import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";

/** One row of the flight recorder list (API 3.16 as 07-05 built it). `checks` are the Guardian's verdicts, each with a reason code. */
export type RecorderRow = {
  id: number;
  run_id: string;
  seq: number;
  created_at: string;
  agent_role: string;
  kind: "model_call" | "tool_call" | "state_change" | "outside_action";
  action_type: string | null;
  status: string;
  summary: string;
  outcome_code: string | null;
  cost_usd: string;
  evidence_count: number | null;
  checks: { check: string; passed: boolean; reason: string | null }[];
  approval: string | null;
  decided_at: string | null;
  executed_at: string | null;
};

/** One entry in full. */
export type RecorderEntry = {
  id: number;
  run_id: string;
  seq: number;
  created_at: string;
  agent_role: string;
  campaign_id: string | null;
  kind: RecorderRow["kind"];
  action_type: string | null;
  status: string;
  summary: string;
  inputs: Record<string, unknown> | null;
  evidence: unknown[] | null;
  payload: Record<string, unknown> | null;
  policy_decision: string | null;
  policy_version: string | null;
  undo_until: string | null;
  executed_at: string | null;
  outcome_code: string | null;
  cost_usd: string;
  checks: { id: number; check: string; passed: boolean; reason_code: string | null; details: Record<string, unknown> | null; checked_at: string }[];
  approval: { id: string; status: string; requested_at: string; decided_at: string | null; decided_via: string | null; note: string | null } | null;
  model_calls: { id: number; task: string; prompt_version: string; outcome: string; cost_microusd: number | null }[];
};

export const recorderKeys = { list: (campaignId: string) => ["recorder", "list", campaignId] as const, entry: (id: number) => ["recorder", "entry", id] as const };

const PAGE = 200;

export function recorderQuery(api: Api, campaignId: string) {
  return queryOptions({
    queryKey: recorderKeys.list(campaignId),
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/v1/control/recorder", { params: { query: { campaign_id: campaignId, limit: PAGE } }, signal }))) as unknown as RecorderRow[],
  });
}

export function recorderEntryQuery(api: Api, id: number) {
  return queryOptions({
    queryKey: recorderKeys.entry(id),
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/control/recorder/{entry_id}", { params: { path: { entry_id: id } }, signal }))) as unknown as RecorderEntry,
  });
}

/** The words for why a row did not go ahead: the outcome's code, else the first failed check's. Empty when nothing was refused. */
export function blockedReasonCode(row: Pick<RecorderRow, "status" | "outcome_code" | "checks">): string | null {
  if (row.status !== "blocked") return null;
  return row.outcome_code ?? row.checks.find((check) => !check.passed)?.reason ?? null;
}
