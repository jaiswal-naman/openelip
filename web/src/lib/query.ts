import { QueryClient } from "@tanstack/react-query";

/**
 * One cache per server request and one per browser tab. A 60 second `staleTime` stops the client from refetching what the
 * server just rendered (TanStack Query's advanced SSR guide). No automatic retries: a failure shows an error state with
 * a Retry button, so the person knows what happened instead of watching a spinner.
 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { staleTime: 60_000, retry: false } } });
}
