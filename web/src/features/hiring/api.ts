import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMe } from "@/features/auth/queries";
import type { Api } from "@/lib/api/client";
import { useApi } from "@/lib/api/context";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type Organization = Schemas["Organization"];
export type Member = Schemas["Member"];
export type OrgRole = Member["role"];
export type RoleView = Schemas["RoleView"];
export type RoleListItem = Schemas["RoleListItem"];
export type RequirementView = Schemas["RequirementView"];
export type RequirementIn = Schemas["RequirementIn"];
export type PayRangeValue = Schemas["app__hiring__role_routes__PayRange"];
export type RolePatch = Schemas["RolePatch"];
export type Preview = Schemas["Preview"];
export type Membership = NonNullable<Schemas["Me"]["organizations"]>[number];
export type Invitation = NonNullable<Schemas["Me"]["invitations"]>[number];

// The introduction, shortlist, answer-clock and signal routes answer with untyped objects (06-04 deviation 1; API.md 3.12 to
// 3.15), so their shapes are written here from what the backend returns. A field the server stops sending fails the component tests.

export type EvidenceRow = {
  requirement: string;
  importance: "required" | "preferred";
  verdict: "met" | "partly_met" | "not_met" | "no_evidence";
  record_quotes: string[];
};
export type SharedDetails = {
  name: string | null;
  headline: string | null;
  evidence: EvidenceRow[];
  facts: { kind: string; statement: string }[];
};
export type ShortlistItem = {
  introduction_id: string;
  status: string;
  responded_at: string | null;
  answer_due_at: string | null;
  is_overdue: boolean;
  employer_answer: "yes" | "no" | null;
  employer_answer_reason: string | null;
  employer_answered_at: string | null;
  shared: SharedDetails | null;
  signals: { from_candidate: boolean; from_you: boolean };
  email: string | null;
};
export type OwedAnswer = {
  introduction_id: string;
  role: { id: string; title: string };
  name: string | null;
  responded_at: string | null;
  answer_due_at: string;
  business_days_left: number;
  is_overdue: boolean;
};
export type AnswerClockData = {
  org_id: string;
  timezone: string;
  as_of: string;
  is_asking_paused: boolean;
  paused_since: string | null;
  pause_reason: string | null;
  owed: OwedAnswer[];
  answered_last_30_days: { total: number; on_time: number };
};
export type Quota = { used: number; limit: number; resets_at: string };
export type SignalsData = {
  sent: { id: string; sender_side: string; role_id: string; introduction_id: string | null; created_at: string }[];
  received: { id: string; sender_side: string; role_id: string; introduction_id: string | null; created_at: string }[];
  quota: Quota;
};
export type AnswerResult = {
  introduction: { id: string; status: string; employer_answer: "yes" | "no"; employer_answer_reason: string; employer_answered_at: string; is_on_time: boolean };
  candidate_sees: string;
  email: string | null;
};

export const hiringKeys = {
  all: ["hiring"] as const,
  org: (id: string) => ["hiring", "org", id] as const,
  roles: (orgId: string) => ["hiring", "roles", orgId] as const,
  role: (id: string) => ["hiring", "role", id] as const,
  preview: (id: string) => ["hiring", "preview", id] as const,
  shortlist: (roleId: string) => ["hiring", "shortlist", roleId] as const,
  clock: (orgId: string) => ["hiring", "clock", orgId] as const,
  signals: (orgId: string) => ["hiring", "signals", orgId] as const,
};

/** The organization the hiring desk shows: the person's first membership. ponytail: no switcher until someone has two. */
export function useMembership(): { membership: Membership | null; invitations: Invitation[]; isLoading: boolean } {
  const me = useMe();
  const organizations = me.data?.organizations ?? [];
  return { membership: organizations[0] ?? null, invitations: me.data?.invitations ?? [], isLoading: me.isPending };
}

export function orgQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: hiringKeys.org(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/orgs/{org_id}", { params: { path: { org_id: id } }, signal })),
  });
}

export function rolesQuery(api: Api, orgId: string) {
  return queryOptions({
    queryKey: hiringKeys.roles(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/roles", { params: { query: { org_id: orgId } }, signal })),
  });
}

export function roleQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: hiringKeys.role(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/roles/{role_id}", { params: { path: { role_id: id } }, signal })),
  });
}

export function previewQuery(api: Api, id: string) {
  return queryOptions({
    queryKey: hiringKeys.preview(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/roles/{role_id}/preview", { params: { path: { role_id: id } }, signal })),
  });
}

export function shortlistQuery(api: Api, roleId: string) {
  return queryOptions({
    queryKey: hiringKeys.shortlist(roleId),
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/v1/roles/{role_id}/shortlist", { params: { path: { role_id: roleId } }, signal }))) as unknown as ShortlistItem[],
  });
}

export function clockQuery(api: Api, orgId: string) {
  return queryOptions({
    queryKey: hiringKeys.clock(orgId),
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/v1/orgs/{org_id}/answer-clock", { params: { path: { org_id: orgId } }, signal }))) as unknown as AnswerClockData,
  });
}

export function signalsQuery(api: Api, orgId: string) {
  return queryOptions({
    queryKey: hiringKeys.signals(orgId),
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/v1/signals", { params: { query: { org_id: orgId } }, signal }))) as unknown as SignalsData,
  });
}

export const useOrg = (id: string) => useQuery(orgQuery(useApi(), id));
export const useRoles = (orgId: string) => useQuery(rolesQuery(useApi(), orgId));
export const useRole = (id: string) => useQuery(roleQuery(useApi(), id));
export const useShortlist = (roleId: string) => useQuery(shortlistQuery(useApi(), roleId));
export const useSignals = (orgId: string) => useQuery(signalsQuery(useApi(), orgId));
/** The clock is re-read every minute: the server owns the business-day arithmetic (FRONTEND_UI 4.7). */
export const useAnswerClock = (orgId: string) => useQuery({ ...clockQuery(useApi(), orgId), refetchInterval: 60_000 });

/**
 * The preview polls while the role is open and no role run has finished yet (`as_of` null). A draft has no counts, so it
 * is not asked at all: no candidate data is requested before publishing.
 */
export function usePreview(role: Pick<RoleView, "id" | "status">) {
  const api = useApi();
  return useQuery({
    ...previewQuery(api, role.id),
    enabled: role.status !== "draft",
    refetchInterval: (query) => (role.status === "open" && query.state.data?.as_of == null ? 15_000 : false),
  });
}

/** Writes refetch what they changed. The Idempotency-Key is added by the client (lib/api/client.ts). */
function useWrite<V, R>(run: (api: Api, input: V) => Promise<R>, invalidate: (result: R, input: V) => readonly (readonly unknown[])[]) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: V) => run(api, input),
    onSuccess: async (result, input) => {
      await Promise.all(invalidate(result, input).map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
  });
}

const ME_KEY = ["me"] as const;

export const useCreateOrganization = () =>
  useWrite(
    (api, body: { name: string; timezone: string }) => unwrap(api.POST("/v1/orgs", { body })),
    () => [ME_KEY],
  );

export const useJoinOrganization = () =>
  useWrite(
    (api, orgId: string) => unwrap(api.POST("/v1/orgs/{org_id}/join", { params: { path: { org_id: orgId } } })),
    () => [ME_KEY, hiringKeys.all],
  );

export const useInvite = (orgId: string) =>
  useWrite(
    (api, body: { email: string; role: "member" | "admin" }) => unwrap(api.POST("/v1/orgs/{org_id}/members", { params: { path: { org_id: orgId } }, body })),
    () => [hiringKeys.org(orgId)],
  );

export const useChangeMemberRole = (orgId: string) =>
  useWrite(
    (api, input: { memberId: string; role: OrgRole }) =>
      unwrap(api.PATCH("/v1/orgs/{org_id}/members/{member_id}", { params: { path: { org_id: orgId, member_id: input.memberId } }, body: { role: input.role } })),
    () => [hiringKeys.org(orgId)],
  );

export const useRemoveMember = (orgId: string) =>
  useWrite(
    async (api, memberId: string) => {
      const result = await api.DELETE("/v1/orgs/{org_id}/members/{member_id}", { params: { path: { org_id: orgId, member_id: memberId } } });
      if (result.error !== undefined || !result.response.ok) await unwrap(Promise.resolve(result)); // throws the ApiFailure
    },
    () => [hiringKeys.org(orgId), ME_KEY],
  );

export const useClaimOwnership = (orgId: string) =>
  useWrite<void, unknown>(
    (api) => unwrap(api.POST("/v1/orgs/{org_id}/claim-ownership", { params: { path: { org_id: orgId } } })),
    () => [hiringKeys.org(orgId), ME_KEY],
  );

export type NewRole = { org_id: string; title: string; description: string };
export const useCreateRole = () =>
  useWrite(
    (api, body: NewRole) => unwrap(api.POST("/v1/roles", { body })),
    (_, body) => [hiringKeys.roles(body.org_id)],
  );

export const usePatchRole = (roleId: string) =>
  useWrite(
    (api, body: RolePatch) => unwrap(api.PATCH("/v1/roles/{role_id}", { params: { path: { role_id: roleId } }, body })),
    () => [hiringKeys.role(roleId)],
  );

export const usePutRequirements = (roleId: string) =>
  useWrite(
    (api, requirements: RequirementIn[]) => unwrap(api.PUT("/v1/roles/{role_id}/requirements", { params: { path: { role_id: roleId } }, body: { requirements } })),
    () => [hiringKeys.role(roleId)],
  );

export const usePublishRole = (roleId: string, orgId: string) =>
  useWrite<void, unknown>(
    (api) => unwrap(api.POST("/v1/roles/{role_id}/publish", { params: { path: { role_id: roleId } } })),
    () => [hiringKeys.role(roleId), hiringKeys.preview(roleId), hiringKeys.roles(orgId), hiringKeys.clock(orgId)],
  );

export const useCloseRole = (roleId: string, orgId: string) =>
  useWrite<void, unknown>(
    (api) => unwrap(api.POST("/v1/roles/{role_id}/close", { params: { path: { role_id: roleId } } })),
    () => [hiringKeys.role(roleId), hiringKeys.preview(roleId), hiringKeys.roles(orgId)],
  );

export const useAnswer = (roleId: string | null, orgId: string) =>
  useWrite(
    async (api, input: { introductionId: string; answer: "yes" | "no"; reason: string }) =>
      (await unwrap(
        api.POST("/v1/introductions/{introduction_id}/answer", {
          params: { path: { introduction_id: input.introductionId } },
          body: { answer: input.answer, reason: input.reason },
        }),
      )) as unknown as AnswerResult,
    () => [roleId === null ? hiringKeys.all : hiringKeys.shortlist(roleId), hiringKeys.clock(orgId), hiringKeys.roles(orgId)],
  );

export const useSendSignal = (roleId: string, orgId: string) =>
  useWrite(
    (api, introductionId: string) => unwrap(api.POST("/v1/signals", { body: { introduction_id: introductionId } })),
    () => [hiringKeys.shortlist(roleId), hiringKeys.signals(orgId)],
  );
