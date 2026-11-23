import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type TailoredView = Schemas["TailoredView"];
export type EditView = Schemas["EditView"];
export type PacketView = Schemas["PacketView"];
export type EditDecision = Schemas["DecisionIn"]["decision"];

export const POLL_MS = 1500; // how often a page asks again while the server builds something

export const tailorKeys = {
  forJob: (jobId: number) => ["tailor", "job", jobId] as const,
  packet: (jobId: number) => ["tailor", "packet", jobId] as const,
};

/** The newest tailored resume for this job, or null. The list is the person's own, newest first, at most 50. */
export function tailoredForJobQuery(api: Api, jobId: number) {
  return queryOptions({
    queryKey: tailorKeys.forJob(jobId),
    queryFn: async ({ signal }): Promise<TailoredView | null> => {
      const items = await unwrap(api.GET("/v1/tailored-resumes", { signal }));
      return items.find((item) => item.job_id === jobId) ?? null;
    },
    // Edits appear together when the resume is ready, so the page asks again until it is.
    refetchInterval: (query) => (query.state.data?.status === "generating" ? POLL_MS : false),
  });
}

/** The current (not superseded) packet for this job, or null. */
export function packetForJobQuery(api: Api, jobId: number) {
  return queryOptions({
    queryKey: tailorKeys.packet(jobId),
    queryFn: async ({ signal }): Promise<PacketView | null> => {
      const items = await unwrap(api.GET("/v1/applications/packets", { params: { query: { job_id: jobId } }, signal }));
      return items[0] ?? null;
    },
    refetchInterval: (query) => (query.state.data?.status === "draft" ? POLL_MS : false),
  });
}

export function useRequestTailoring(jobId: number) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/tailored-resumes", { body: { job_id: jobId } })),
    onSuccess: () => client.invalidateQueries({ queryKey: tailorKeys.forJob(jobId) }),
  });
}

export function useDecide(jobId: number, tailoredId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (decision: { edit_id: string; decision: EditDecision }) =>
      unwrap(api.POST("/v1/tailored-resumes/{tailored_id}/decisions", { params: { path: { tailored_id: tailoredId } }, body: { decisions: [decision] } })),
    onSuccess: (view) => client.setQueryData(tailorKeys.forJob(jobId), view),
    onError: () => client.invalidateQueries({ queryKey: tailorKeys.forJob(jobId) }),
  });
}

export function useRequestPacket(jobId: number, tailoredId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/applications/packets", { body: { job_id: jobId, tailored_resume_id: tailoredId } })),
    onSuccess: () => client.invalidateQueries({ queryKey: tailorKeys.packet(jobId) }),
  });
}

export function useRecordApplied(jobId: number, packetId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/applications", { body: { job_id: jobId, application_packet_id: packetId } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["applications"] }),
  });
}

export const fileUrl = (tailoredId: string, format: "pdf" | "docx") => `/v1/tailored-resumes/${tailoredId}/file?format=${format}`;
export const packetFileUrl = (packetId: string) => `/v1/applications/packets/${packetId}/file?format=pdf`;
