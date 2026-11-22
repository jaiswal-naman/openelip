"use client";

import { isServer, QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { createApi } from "@/lib/api/client";
import { ApiProvider } from "@/lib/api/context";
import { makeQueryClient } from "@/lib/query";

let browserQueryClient: QueryClient | undefined;

// The server makes a new client for every request; the browser keeps one for the life of the tab.
function getQueryClient(): QueryClient {
  if (isServer) return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}

export function Providers({ children }: { children: ReactNode }) {
  const queryClient = getQueryClient();
  // Same origin: the reverse proxy serves /v1 beside the pages, so the session cookie rides along with no CORS.
  const [api] = useState(() => createApi({ baseUrl: isServer ? "http://localhost" : window.location.origin }));
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider api={api}>{children}</ApiProvider>
    </QueryClientProvider>
  );
}
