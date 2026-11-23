import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authKeys } from "@/features/auth/queries";
import type { EvidenceRow, SharedDetails } from "@/features/hiring/api";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { Me } from "@/lib/api/types";

// The introduction and signal routes answer with untyped objects (06-04), so the candidate's shapes are written here from
// what the backend returns (GET /v1/introductions, GET /v1/introductions/{id}, API.md 3.12). A field the server stops
// sending fails the component tests.

export type { EvidenceRow, SharedDetails };

export type IntroductionStatus = "asked" | "accepted" | "declined" | "expired" | "withdrawn";

export type IntroductionItem = {
  id: string;
  status: IntroductionStatus;
  asked_at: string;
  expires_at: string;
  responded_at: string | null;
  employer_answer: "yes" | "no" | null;
  employer_answered_at: string | null;
  organization_name: string | null;
  role_title: string | null;
};

export type PayRange = { min: number | null; max: number | null; currency: string | null };

export type IntroductionDetail = {
  view: "candidate";
  id: string;
  status: IntroductionStatus;
  asked_at: string;
  expires_at: string;
  responded_at: string | null;
  employer_answer: "yes" | "no" | null;
  employer_answer_reason: string | null;
  employer_answered_at: string | null;
  organization: { id: string; name: string; is_synthetic: boolean };
  role: { id: string; title: string; job_id: number | null; country: string | null; city: string | null; work_arrangements: string[]; pay_range: PayRange | null };
  has_employer_signal: boolean;
  terms: string;
  share_preview: { on_accept: SharedDetails; on_employer_yes: { email: string } };
  share_preview_hash: string;
  consent_text_version: string;
};

export type CandidateSignals = {
  sent: { id: string; sender_side: string; role_id: string; introduction_id: string | null; created_at: string }[];
  received: { id: string; sender_side: string; role_id: string; introduction_id: string | null; created_at: string }[];
  quota: { used: number; limit: number; resets_at: string };
};

/**
 * The wording the person saw beside the "Open to introductions from employers" switch when they agreed. The API stores it
 * with the consent; change this string when that sentence changes. No earlier screen sent a version for this purpose.
 */
export const OPEN_TO_INTRODUCTIONS_TEXT_VERSION = "open-to-introductions-2026-11";

export const introKeys = {
  all: ["introductions"] as const,
  list: ["introductions", "list"] as const,
  detail: (id: string) => ["introductions", "detail", id] as const,
  signals: ["introductions", "signals"] as const,
};

export function introductionsQuery(api: Api) {
  return queryOptions({
    queryKey: introKeys.list,
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/introductions", { signal }))) as unknown as IntroductionItem[],
  });
}

export function introductionQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: introKeys.detail(id),
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/v1/introductions/{introduction_id}", { params: { path: { introduction_id: id } }, signal }))) as unknown as IntroductionDetail,
  });
}

export function candidateSignalsQuery(api: Api) {
  return queryOptions({
    queryKey: introKeys.signals,
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/signals", { signal }))) as unknown as CandidateSignals,
  });
}

export const useIntroductions = () => useQuery(introductionsQuery(useApi()));
export const useIntroduction = (id: string, enabled = true) => useQuery({ ...introductionQuery(useApi(), id), enabled });
export const useCandidateSignals = (enabled = true) => useQuery({ ...candidateSignalsQuery(useApi()), enabled });

/** An ask the person can still answer: asked and not past its expiry (the server also refuses a lapsed one). */
export const isOpenAsk = (item: IntroductionItem, now: number = Date.now()) => item.status === "asked" && new Date(item.expires_at).getTime() > now;

/** Accept shares exactly what the sheet showed: the hash and the wording version both come from the GET the sheet read. */
export function useAcceptIntroduction(id: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { share_preview_hash: string; consent_text_version: string }) =>
      unwrap(api.POST("/v1/introductions/{introduction_id}/accept", { params: { path: { introduction_id: id } }, body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: introKeys.all }),
  });
}

export function useDeclineIntroduction(id: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/introductions/{introduction_id}/decline", { params: { path: { introduction_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: introKeys.all }),
  });
}

export function useSendInterest() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (introductionId: string) => unwrap(api.POST("/v1/signals", { body: { introduction_id: introductionId } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: introKeys.all }),
  });
}

const grant = { purpose: "open_to_introductions", granted: true, text_version: OPEN_TO_INTRODUCTIONS_TEXT_VERSION } as const;
const withdraw = { purpose: "open_to_introductions", granted: false } as const;

/** The settings switch. A grant carries the wording version (without it the API answers 422); a withdrawal sends `granted: false` only. */
export const saveOpenToIntroductions = (api: Api, on: boolean): Promise<Me> => unwrap(api.PATCH("/v1/me", { body: { consents: [on ? grant : withdraw] } }));

export function useOpenToIntroductions() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (on: boolean) => saveOpenToIntroductions(api, on),
    onSuccess: (me) => queryClient.setQueryData(authKeys.me, me),
  });
}
