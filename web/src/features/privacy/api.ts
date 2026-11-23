import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

export type PrivacyRequest = components["schemas"]["PrivacyRequest"];

export const privacyKeys = { request: (id: string) => ["privacy", "request", id] as const };

/** One export or deletion request, as the API reports it. An export is polled until it has a download or has failed. */
export function requestQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: privacyKeys.request(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/privacy/requests/{request_id}", { params: { path: { request_id: id } }, signal })),
  });
}

// Writes. The client adds the Idempotency-Key to each.

/** Ask for an archive of everything held about the person. Asking while one is being made returns that one. */
export const requestExport = (api: Api) => unwrap(api.POST("/v1/privacy/exports"));

/** Withdraw the AI processing consent. AI features turn off; nothing already saved is touched. */
export const withdrawAiProcessing = (api: Api) =>
  unwrap(api.PATCH("/v1/me", { body: { consents: [{ purpose: "ai_processing", granted: false }] } }));

/** A fresh sign-in code for the account's own address: the proof that whoever is deleting the account can read its mail. */
export const sendDeletionCode = (api: Api, email: string) => unwrap(api.POST("/v1/auth/codes", { body: { email } }));

/** Close the account now and queue the erasure. The API checks the code; a wrong one changes nothing. */
export const deleteAccount = (api: Api, code: string) => unwrap(api.POST("/v1/privacy/deletion", { body: { code } }));
