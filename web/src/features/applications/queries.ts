import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

export type ApplicationView = components["schemas"]["ApplicationView"];
export type ApplicationStatus = ApplicationView["status"];

export const applicationKeys = { all: ["applications"] as const };

export function applicationsQuery(api: Api) {
  return queryOptions({
    queryKey: applicationKeys.all,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/applications", { signal })),
  });
}

export function useRecordStatus(applicationId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { status: ApplicationStatus; occurred_at: string }) =>
      unwrap(api.POST("/v1/applications/{application_id}/status", { params: { path: { application_id: applicationId } }, body })),
    onSuccess: () => client.invalidateQueries({ queryKey: applicationKeys.all }),
  });
}
