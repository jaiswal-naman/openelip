import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type Citation = Schemas["CitationView"];
export type Proposal = Schemas["ProposalView"];

export const createSession = (api: Api) => unwrap(api.POST("/v1/chat/sessions", { body: { kind: "open" } }));

export const postMessage = (api: Api, sessionId: string, text: string, contextJobId: number | null) =>
  unwrap(
    api.POST("/v1/chat/sessions/{session_id}/messages", {
      params: { path: { session_id: sessionId } },
      body: contextJobId === null ? { text } : { text, context_job_id: contextJobId },
    }),
  );

export const stopReply = (api: Api, sessionId: string, messageId: string) =>
  unwrap(api.POST("/v1/chat/sessions/{session_id}/messages/{message_id}/stop", { params: { path: { session_id: sessionId, message_id: messageId } } }));

export const confirmProposal = (api: Api, eventId: string) => unwrap(api.POST("/v1/me/events/{event_id}/confirm", { params: { path: { event_id: eventId } } }));

export const declineProposal = (api: Api, eventId: string) => unwrap(api.POST("/v1/me/events/{event_id}/decline", { params: { path: { event_id: eventId } } }));
