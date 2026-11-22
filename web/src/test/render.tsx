import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { createApi } from "@/lib/api/client";
import { ApiProvider } from "@/lib/api/context";
import { makeQueryClient } from "@/lib/query";

/** The API's success envelope (API.md 1.3). */
export function ok(data: unknown, status = 200, meta: Record<string, unknown> = {}): Response {
  return Response.json({ success: true, data, error: null, meta: { request_id: "req_test", ...meta } }, { status });
}

/** The API's error envelope (API.md 1.11). */
export function fail(status: number, code: string, details: Record<string, unknown> = {}, headers?: HeadersInit): Response {
  const error = { code, message: "message for people", details };
  return Response.json({ success: false, data: null, error, meta: { request_id: "req_test" } }, { status, headers });
}

type Handler = (request: Request) => Response | Promise<Response>;

/**
 * A fake `fetch` for the typed client. Routes are keyed "METHOD /path" (query string ignored); a call with no route throws,
 * so a screen that asks for something the test did not plan for fails loudly instead of hanging.
 */
export function fakeFetch(routes: Record<string, Handler | Response>) {
  const calls: Request[] = [];
  const fetch = async (request: Request): Promise<Response> => {
    calls.push(request);
    const key = `${request.method} ${new URL(request.url).pathname}`;
    const route = routes[key];
    if (route === undefined) throw new Error(`No fake route for ${key}`);
    return typeof route === "function" ? route(request) : route.clone();
  };
  return { fetch, calls };
}

export function renderWithApi(ui: ReactElement, routes: Record<string, Handler | Response> = {}) {
  const fake = fakeFetch(routes);
  const queryClient = makeQueryClient();
  const api = createApi({ baseUrl: "http://localhost", fetch: fake.fetch });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider api={api}>{ui}</ApiProvider>
    </QueryClientProvider>,
  );
  return { ...result, calls: fake.calls, queryClient, api };
}
