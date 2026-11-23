import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

/** Names for the generated shapes the match screens read: when the API changes a field, the type check fails here. */
type Schemas = components["schemas"];
export type TodayMatches = Schemas["TodayMatches"];
export type MatchView = Schemas["MatchView"];
export type MatchData = Schemas["MatchDetail"];
export type EvidenceItem = Schemas["EvidenceItem"];
export type RecordCitation = Schemas["RecordCitation"];
export type ConstraintCheck = Schemas["ConstraintCheck"];
export type DecisionView = Schemas["DecisionView"];
export type DecisionData = Schemas["DecisionDetail"];
export type BriefData = Schemas["BriefDetail"];
export type BriefDecision = Schemas["BriefDecision"];
export type FeedbackBody = Schemas["FeedbackBody"];
export type FeedbackRecorded = Schemas["FeedbackRecorded"];
export type DismissReason = NonNullable<FeedbackBody["reason"]>;

export const matchKeys = {
  today: (date: string | null) => ["matches", "today", date] as const,
  whyNot: (date: string) => ["matches", "why-not", date] as const,
  latestBrief: ["matches", "latest-brief"] as const,
  document: (id: string) => ["matches", "document", id] as const,
};

/** The matches shown on a date (the person's own today when none is named): at most five, and zero is a valid day. */
export function todayQuery(api: Api, date: string | null = null) {
  return queryOptions({
    queryKey: matchKeys.today(date),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/matches", { params: { query: date === null ? {} : { date } }, signal })),
  });
}

export const WHY_NOT_PAGE_SIZE = 100;
// ponytail: stop after 1,000 rows. A day's runs store up to about 70 held-back rows each, so a real day is far below it; the bound only
// keeps a ledger that never stops handing out a cursor from making the page ask for ever.
export const WHY_NOT_MAX_PAGES = 10;

/**
 * Everything held back on one local date: withheld and filtered decisions, newest first (MAT-06). A day with several runs can hold
 * more than one page, so this follows the cursor in the envelope's meta (which `unwrap` drops) until the ledger has no more.
 */
export function whyNotQuery(api: Api, date: string) {
  return queryOptions({
    queryKey: matchKeys.whyNot(date),
    queryFn: async ({ signal }) => {
      const rows: DecisionView[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < WHY_NOT_MAX_PAGES; page += 1) {
        const pending = api.GET("/v1/decisions", {
          params: { query: { outcome: "withheld,filtered", from: date, to: date, limit: WHY_NOT_PAGE_SIZE, cursor } },
          signal,
        });
        rows.push(...(await unwrap(pending)));
        const next = (await pending).data?.meta.next_cursor;
        if (typeof next !== "string" || next === "") break;
        cursor = next;
      }
      return rows;
    },
  });
}

/** The newest in-app brief with its decisions, or null before the first one has been made (the list is newest first). */
export function latestBriefQuery(api: Api) {
  return queryOptions({
    queryKey: matchKeys.latestBrief,
    queryFn: async ({ signal }): Promise<BriefData | null> => {
      const [latest] = await unwrap(api.GET("/v1/briefs", { params: { query: { limit: 1 } }, signal }));
      if (latest === undefined) return null;
      return unwrap(api.GET("/v1/briefs/{brief_id}", { params: { path: { brief_id: latest.id } }, signal }));
    },
  });
}

/** The resume the evidence cites, with the cleaned text its spans point into. Loaded only when a row is selected. */
export function resumeTextQuery(api: Api, documentId: string) {
  return queryOptions({
    queryKey: matchKeys.document(documentId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/me/documents/{document_id}", { params: { path: { document_id: documentId }, query: { include: "text" } }, signal })),
  });
}

/** Save, unsave or dismiss one match. A dismissal may propose a record change; it changes nothing until confirmed. */
export const giveFeedback = (api: Api, decisionId: string, body: FeedbackBody) =>
  unwrap(api.POST("/v1/matches/{decision_id}/feedback", { params: { path: { decision_id: decisionId } }, body }));
