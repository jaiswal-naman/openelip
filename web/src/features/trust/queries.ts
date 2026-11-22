import { queryOptions } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";

export const trustKeys = { subprocessors: ["trust", "subprocessors"] as const };

/** Every model provider and sub-processor this server sends data to, from its running configuration. Public. */
export function subprocessorsQuery(api: Api) {
  return queryOptions({
    queryKey: trustKeys.subprocessors,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/privacy/subprocessors", { signal })),
  });
}
