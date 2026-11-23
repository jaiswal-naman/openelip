import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";

export const localKeys = { status: ["local", "status"] as const };

/**
 * Local mode only: whether the owner exists and which model the stack would call. Asked by the browser, never by a server
 * component: the API answers only a request whose Host header names this computer, and the browser's is the one that does.
 */
export function localStatusQuery(api: Api) {
  return queryOptions({
    queryKey: localKeys.status,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/local/status", { signal })),
  });
}
