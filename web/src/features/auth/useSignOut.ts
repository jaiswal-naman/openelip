"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useApi } from "@/lib/api/context";
import { isFailure, unwrapEmpty } from "@/lib/api/errors";
import { authKeys } from "./queries";

/**
 * Sign out: the API revokes the session and clears the cookie (DELETE needs an Idempotency-Key, which the client adds).
 * Afterwards nothing the signed-in person read stays in the cache, and the page goes back to /.
 */
export function useSignOut() {
  const api = useApi();
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: async () => {
      try {
        await unwrapEmpty(api.DELETE("/v1/auth/sessions/current"));
      } catch (error) {
        if (!isFailure(error, "unauthenticated")) throw error; // already signed out is the outcome we wanted
      }
    },
    onSuccess: () => {
      queryClient.removeQueries();
      queryClient.setQueryData(authKeys.me, null);
      router.push("/");
      router.refresh();
    },
  });
}
