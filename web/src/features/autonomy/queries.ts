import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";

/** `GET /v1/autonomy` as 07-05 built it (the route has no response model, so the shape is written here). */
export type Autonomy = {
  version: string;
  actions: Record<string, { level: string; allowed_levels: string[] }>;
  profile_sharing: string;
  undo_window_minutes: number;
  campaign_budget_default_usd: string;
};

export const autonomyKey = ["autonomy"] as const;

export function autonomyQuery(api: Api) {
  return queryOptions({
    queryKey: autonomyKey,
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/autonomy", { signal }))) as unknown as Autonomy,
  });
}

/** Saves a change with `If-Match` set to the version the page showed; a stale version is refused (412) and the page reloads. */
export function useSaveAutonomy() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { version: string; actions?: Record<string, { level: string }>; undo_window_minutes?: number }) =>
      (await unwrap(
        api.PUT("/v1/autonomy", {
          params: { header: { "if-match": `"${input.version}"` } },
          body: { actions: input.actions ?? null, undo_window_minutes: input.undo_window_minutes ?? null },
        }),
      )) as unknown as Autonomy,
    onSuccess: (data) => client.setQueryData(autonomyKey, data),
    onError: () => client.invalidateQueries({ queryKey: autonomyKey }),
  });
}
