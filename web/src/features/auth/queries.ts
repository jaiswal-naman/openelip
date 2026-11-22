import { queryOptions, useQuery } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { isFailure, unwrap } from "@/lib/api/errors";
import type { Me } from "@/lib/api/types";

export const authKeys = { me: ["me"] as const };

/** Who is signed in: the account, or `null` when nobody is. A 401 here is an answer, not an error. */
export function meQuery(api: Api) {
  return queryOptions({
    queryKey: authKeys.me,
    queryFn: async ({ signal }): Promise<Me | null> => {
      try {
        return await unwrap(api.GET("/v1/me", { signal }));
      } catch (error) {
        if (isFailure(error, "unauthenticated")) return null;
        throw error;
      }
    },
  });
}

export function useMe() {
  return useQuery(meQuery(useApi()));
}
