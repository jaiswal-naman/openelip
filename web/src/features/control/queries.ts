import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";

export type GroupBy = "month" | "task"; // "campaign" arrives with campaigns, in a later phase

export const controlKeys = { costs: (groupBy: GroupBy) => ["control", "costs", groupBy] as const };

/** The signed-in person's own model cost, summed from the ledger. The API reads it under their id. */
export function costsQuery(api: Api, groupBy: GroupBy) {
  return queryOptions({
    queryKey: controlKeys.costs(groupBy),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/control/costs", { params: { query: { group_by: groupBy } }, signal })),
  });
}
