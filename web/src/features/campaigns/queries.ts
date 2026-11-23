import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type Board = Schemas["Board"];
export type BoardColumn = Schemas["Column"];
export type BoardCard = Schemas["Card"];
export type CampaignDetail = Schemas["CampaignDetail"];
export type FollowUp = Schemas["FollowUp"];
export type Step = Schemas["Step"];
export type PacketView = Schemas["PacketView"];

/** The created or changed campaign (API 3.5): the routes have no response model, so the shape is written here. */
export type CampaignSummary = { id: string; stage: string; paused_at: string | null; pause_reason: string | null; closed_reason: string | null };
export type CancelResult = { id: string; stage: string; closed_reason: string | null; expired_approvals?: number; cancelled_actions?: number };

/** One claim of a dossier: its text is the page's own quote, and every claim has at least one source. Text only. */
export type DossierClaim = {
  text: string;
  topic?: string | null;
  injection_suspected?: boolean;
  sources: { url: string; quote: string; retrieved_at: string }[];
};

/** The Coach's stored prep (07-04 `interview_prep` output). */
export type PrepQuestion = { text: string; requirement_id?: number | null; fact_ids?: string[]; gap?: boolean; framing?: string | null };
export type Prep = { questions: PrepQuestion[]; ask_them: { text: string; claim_indexes?: number[] }[]; removed_by_reason?: Record<string, number> };

export const CREW_REFRESH_MS = 15_000; // INDEX decision 26: no stream until Phase 8, so the page asks again
export const APPROVAL_POLL_MS = 1_000;
export const APPROVAL_POLL_LIMIT_MS = 30_000; // assumption

export const campaignKeys = {
  board: (includeClosed: boolean) => ["campaigns", "board", includeClosed] as const,
  detail: (id: string) => ["campaigns", "detail", id] as const,
  all: ["campaigns"] as const,
};

export function boardQuery(api: Api, includeClosed: boolean) {
  return queryOptions({
    queryKey: campaignKeys.board(includeClosed),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/campaigns", { params: { query: { include_closed: includeClosed } }, signal })),
  });
}

export function campaignQuery(api: Api, id: string, refetchInterval: number | false | (() => number | false) = CREW_REFRESH_MS) {
  return queryOptions({
    queryKey: campaignKeys.detail(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: id } }, signal })),
    refetchInterval,
  });
}

/** The packet a campaign holds (the Phase 5 packet endpoint), with the candidate's question labels and answers. */
export function packetQuery(api: Api, packetId: string) {
  return queryOptions({
    queryKey: ["campaigns", "packet", packetId] as const,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/applications/packets/{packet_id}", { params: { path: { packet_id: packetId } }, signal })),
  });
}

function useRefresh() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: campaignKeys.all });
}

export function useStartCampaign() {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (jobId: number) => (await unwrap(api.POST("/v1/campaigns", { body: { job_id: jobId } }))) as unknown as CampaignSummary,
    onSuccess: refresh,
  });
}

export function usePatchCampaign(id: string) {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (body: Schemas["PatchIn"]) =>
      (await unwrap(api.PATCH("/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: id } }, body }))) as unknown as CampaignSummary,
    onSuccess: refresh,
  });
}

export function useCampaignControl(id: string) {
  const api = useApi();
  const refresh = useRefresh();
  const params = { params: { path: { campaign_id: id } } };
  return {
    pause: useMutation({ mutationFn: () => unwrap(api.POST("/v1/campaigns/{campaign_id}/pause", { ...params, body: null })), onSuccess: refresh }),
    resume: useMutation({ mutationFn: () => unwrap(api.POST("/v1/campaigns/{campaign_id}/resume", { ...params, body: null })), onSuccess: refresh }),
    cancel: useMutation({
      mutationFn: async (reason: "offer_accepted" | "offer_declined" | null) =>
        (await unwrap(api.POST("/v1/campaigns/{campaign_id}/cancel", { ...params, body: reason === null ? null : { reason } }))) as unknown as CancelResult,
      onSuccess: refresh,
    }),
  };
}

export type ApproveResult = { approval: { id: string; status: string }; action: { id: number; type: string; status: string }; release?: string };

export function useApprove(campaignId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { approvalId: string; payloadHash: string; edits?: Record<string, string> }) =>
      (await unwrap(
        api.POST("/v1/approvals/{approval_id}/approve", {
          params: { path: { approval_id: input.approvalId } },
          body: { payload_hash: input.payloadHash, edits: input.edits ?? null },
        }),
      )) as unknown as ApproveResult,
    onSuccess: () => client.invalidateQueries({ queryKey: campaignKeys.detail(campaignId) }),
  });
}

export function useReject(campaignId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { approvalId: string; note?: string }) =>
      unwrap(api.POST("/v1/approvals/{approval_id}/reject", { params: { path: { approval_id: input.approvalId } }, body: input.note ? { note: input.note } : null })),
    onSuccess: () => client.invalidateQueries({ queryKey: campaignKeys.detail(campaignId) }),
  });
}

/** The approval as the candidate sees it before deciding: the payload and the hash the approve call must send back. */
export type ApprovalView = { id: string; status: string; payload: Record<string, unknown>; payload_hash: string; editable_fields: string[]; expires_at: string | null };
export function approvalQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: ["approvals", id] as const,
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/approvals/{approval_id}", { params: { path: { approval_id: id } }, signal }))) as unknown as ApprovalView,
  });
}

export function useUndo() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (actionId: number) =>
      (await unwrap(api.POST("/v1/control/actions/{action_id}/undo", { params: { path: { action_id: actionId } } }))) as unknown as { action: { id: number; status: string } },
    onSettled: () => client.invalidateQueries(),
  });
}
