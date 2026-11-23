import { describe, expect, it, vi } from "vitest";
import { createApi } from "@/lib/api/client";
import { ApiFailure } from "@/lib/api/errors";
import { fail, fakeFetch, ok } from "@/test/render";
import { loadMatch } from "./loadMatch";
import { DECISION_ID, decisionData, matchData, OTHER_DECISION_ID } from "./testing";

// notFound() ends rendering by throwing; here it throws something the test can recognise.
vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NOT_FOUND_PAGE");
  },
}));

function apiWith(routes: Record<string, Response>) {
  const fake = fakeFetch(routes);
  return { api: createApi({ baseUrl: "http://localhost", fetch: fake.fetch }), calls: fake.calls };
}

const paths = (calls: Request[]) => calls.map((call) => new URL(call.url).pathname);

describe("loadMatch", () => {
  it("returns the match the API sends for a shown decision, and asks for nothing else", async () => {
    const { api, calls } = apiWith({ [`GET /v1/matches/${DECISION_ID}`]: ok(matchData()) });
    await expect(loadMatch(api, DECISION_ID)).resolves.toMatchObject({ kind: "match", match: { decision_id: DECISION_ID } });
    expect(paths(calls)).toEqual([`/v1/matches/${DECISION_ID}`]);
  });

  it("falls back to the decision for a link to a job that was filtered or withheld (MAT-05, MAT-06)", async () => {
    const { api, calls } = apiWith({
      [`GET /v1/matches/${OTHER_DECISION_ID}`]: fail(404, "not_found"),
      [`GET /v1/decisions/${OTHER_DECISION_ID}`]: ok(decisionData()),
    });
    await expect(loadMatch(api, OTHER_DECISION_ID)).resolves.toMatchObject({ kind: "held", decision: { outcome: "filtered" } });
    expect(paths(calls)).toEqual([`/v1/matches/${OTHER_DECISION_ID}`, `/v1/decisions/${OTHER_DECISION_ID}`]);
  });

  it("shows the not-found page when neither knows the id, which is also another person's decision (threat T-04-35)", async () => {
    const { api } = apiWith({
      [`GET /v1/matches/${DECISION_ID}`]: fail(404, "not_found"),
      [`GET /v1/decisions/${DECISION_ID}`]: fail(404, "not_found"),
    });
    await expect(loadMatch(api, DECISION_ID)).rejects.toThrow("NOT_FOUND_PAGE");
  });

  it.each(["abc", "7", "7a1c2d3e-4f50-4a6b-8c7d", "../v1/me", "", ` ${DECISION_ID}`, `${DECISION_ID}/x`])(
    "shows the not-found page for the address %j without asking the API",
    async (id) => {
      const { api, calls } = apiWith({});
      await expect(loadMatch(api, id)).rejects.toThrow("NOT_FOUND_PAGE");
      expect(calls).toHaveLength(0);
    },
  );

  it("does not hide a failure that is not about the id: a service that is down is an error, not a missing match", async () => {
    const { api } = apiWith({ [`GET /v1/matches/${DECISION_ID}`]: fail(503, "dependency_unavailable") });
    await expect(loadMatch(api, DECISION_ID)).rejects.toBeInstanceOf(ApiFailure);
    await expect(loadMatch(api, DECISION_ID)).rejects.toMatchObject({ code: "dependency_unavailable" });
  });

  it("does not hide a failure of the fallback either", async () => {
    const { api } = apiWith({
      [`GET /v1/matches/${DECISION_ID}`]: fail(404, "not_found"),
      [`GET /v1/decisions/${DECISION_ID}`]: fail(500, "internal_error"),
    });
    await expect(loadMatch(api, DECISION_ID)).rejects.toMatchObject({ code: "internal_error" });
  });
});
