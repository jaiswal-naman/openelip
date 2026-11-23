import { notFound } from "next/navigation";
import type { Api } from "@/lib/api/client";
import { isFailure, unwrap } from "@/lib/api/errors";
import type { DecisionData, MatchData } from "./queries";

const DECISION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** What `/app/matches/[id]` shows: the match, or the decision behind a link to a job that was never one. */
export type LoadedMatch = { kind: "match"; match: MatchData } | { kind: "held"; decision: DecisionData };

const isAbsent = (error: unknown) => isFailure(error) && (error.status === 404 || error.status === 422);

/**
 * The page's data, for a Server Component. Only a shown decision of the signed-in person is a match. A link to a job the
 * constraints filtered or the run withheld falls back to the decision, so the page can say why there is no match (MAT-05,
 * MAT-06). Anything else is the not-found page, the same page for all of it so nothing is revealed (FRONTEND_UI 9.9):
 *   - an address that is not a decision id: not sent to the API at all;
 *   - an id the API does not know, or that is another person's: 404 (threat T-04-35).
 * A failure that is not about the id, such as the API being down, is thrown so the error page says so and offers Retry.
 */
export async function loadMatch(api: Api, id: string): Promise<LoadedMatch> {
  if (!DECISION_ID.test(id)) notFound();
  try {
    return { kind: "match", match: await unwrap(api.GET("/v1/matches/{decision_id}", { params: { path: { decision_id: id } } })) };
  } catch (error) {
    if (!isAbsent(error)) throw error;
  }
  try {
    return { kind: "held", decision: await unwrap(api.GET("/v1/decisions/{decision_id}", { params: { path: { decision_id: id } } })) };
  } catch (error) {
    if (isAbsent(error)) notFound();
    throw error;
  }
}
