import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

/** Names for the generated shapes the Career Record screens use. */
type Schemas = components["schemas"];
export type RecordView = Schemas["RecordView"];
export type RecordFact = Schemas["RecordFact"];
export type RecordConstraint = Schemas["RecordConstraint"];
export type RecordPreference = Schemas["RecordPreference"];
export type RecordEvent = Schemas["RecordEvent"];
export type RecordDocument = Schemas["RecordDocument"];
export type DocumentView = Schemas["DocumentView"];
export type DocumentLine = Schemas["DocumentLine"];
export type NewConstraint = Schemas["NewConstraint"];
export type NewPreference = Schemas["NewPreference"];
export type FactDecision = Schemas["FactDecision"];
export type FactKind = Schemas["NewFact"]["kind"];

/** Every query key starts with "record", so one invalidation after a write refetches the page, the log and the review list. */
export const recordKeys = {
  all: ["record"] as const,
  view: ["record", "view"] as const,
  unconfirmed: ["record", "facts", "unconfirmed"] as const,
  document: (id: string, withText: boolean) => ["record", "document", id, withText] as const,
  events: ["record", "events", "log"] as const,
  proposals: ["record", "events", "proposed"] as const,
};

/** The record on one page: confirmed facts, active constraints and preferences, counts, the latest uploads. */
export function recordQuery(api: Api) {
  return queryOptions({
    queryKey: recordKeys.view,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/me/record", { signal })),
  });
}

// ponytail: the first 100 facts to review. A decision removes a fact from this list, so the rest appear as these are decided.
export function unconfirmedFactsQuery(api: Api) {
  return queryOptions({
    queryKey: recordKeys.unconfirmed,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/me/facts", { params: { query: { status: "unconfirmed", limit: 100 } }, signal })),
  });
}

/** How a parse is going. With the text it also carries the numbered lines every fact's span points into. */
export function documentQuery(api: Api, id: string, withText = false) {
  return queryOptions({
    queryKey: recordKeys.document(id, withText),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/me/documents/{document_id}", {
          params: { path: { document_id: id }, query: withText ? { include: "text" } : {} },
          signal,
        }),
      ),
  });
}

export function proposalsQuery(api: Api) {
  return queryOptions({
    queryKey: recordKeys.proposals,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/me/events", { params: { query: { status: "proposed", limit: 20 } }, signal })),
  });
}

export type EventsPage = { events: RecordEvent[]; nextCursor: string | null };

/** One page of the change log, newest first. The cursor for the next page is in the envelope's meta, which `unwrap` drops. */
export async function eventsPage(api: Api, cursor: string | undefined, signal?: AbortSignal): Promise<EventsPage> {
  let meta: Record<string, unknown> = {};
  const events = await unwrap(
    api.GET("/v1/me/events", { params: { query: { limit: 20, cursor } }, signal }).then((result) => {
      meta = result.data?.meta ?? {};
      return result;
    }),
  );
  return { events, nextCursor: typeof meta.next_cursor === "string" ? meta.next_cursor : null };
}

// Writes. The client adds the Idempotency-Key; each call is one request.

/** The multipart body is built by hand: the browser sets the Content-Type with its boundary, which is why none is given. */
export function uploadDocument(api: Api, file: File) {
  const form = new FormData();
  form.append("file", file);
  return unwrap(api.POST("/v1/me/documents", { body: { file: "" }, bodySerializer: () => form }));
}

export const reviewFacts = (api: Api, decisions: FactDecision[]) => unwrap(api.POST("/v1/me/facts/review", { body: { decisions } }));

export const addFact = (api: Api, body: { kind: FactKind; statement: string }) => unwrap(api.POST("/v1/me/facts", { body }));

/** One active constraint per kind: saving replaces the one there is, and the change log keeps it. */
export const saveConstraint = (api: Api, body: NewConstraint) => unwrap(api.POST("/v1/me/constraints", { body }));

export const removeConstraint = (api: Api, id: string) =>
  unwrap(api.DELETE("/v1/me/constraints/{constraint_id}", { params: { path: { constraint_id: id } } }));

export const savePreference = (api: Api, body: NewPreference) => unwrap(api.POST("/v1/me/preferences", { body }));

export const changePreference = (api: Api, { id, body }: { id: string; body: Schemas["PreferenceChange"] }) =>
  unwrap(api.PATCH("/v1/me/preferences/{preference_id}", { params: { path: { preference_id: id } }, body }));

export const removePreference = (api: Api, id: string) =>
  unwrap(api.DELETE("/v1/me/preferences/{preference_id}", { params: { path: { preference_id: id } } }));

/** Confirming a proposal applies it; declining answers no. Either way the proposal is answered, once. */
export async function answerProposal(api: Api, { id, answer }: { id: string; answer: "confirm" | "decline" }): Promise<void> {
  const path = { params: { path: { event_id: id } } };
  if (answer === "confirm") await unwrap(api.POST("/v1/me/events/{event_id}/confirm", path));
  else await unwrap(api.POST("/v1/me/events/{event_id}/decline", path));
}

/**
 * A write that refetches the record, the review list and the change log afterwards, so nothing needs a reload. It does so
 * when the write failed too: a conflict means the page was behind, and the latest is what the person should see.
 */
export function useRecordWrite<Input, Output>(write: (api: Api, input: Input) => Promise<Output>) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: Input) => write(api, input),
    onSettled: () => queryClient.invalidateQueries({ queryKey: recordKeys.all }),
  });
}
