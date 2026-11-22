import createClient, { type Client, type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

export type Api = Client<paths>;

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Every write is JSON and carries a fresh Idempotency-Key (API.md 1.2 and 1.6). A cross-site form cannot send either,
 * which is the CSRF defence together with SameSite=Lax. The two public sign-in routes ignore the key; sending it is harmless.
 */
const writeHeaders: Middleware = {
  onRequest({ request }) {
    if (!WRITE_METHODS.has(request.method)) return;
    if (!request.headers.has("Content-Type")) request.headers.set("Content-Type", "application/json");
    if (!request.headers.has("Idempotency-Key")) request.headers.set("Idempotency-Key", crypto.randomUUID());
  },
};

export type ApiOptions = {
  /** An absolute origin. In the browser this is the page's own origin, because the proxy serves /v1 beside the page. */
  baseUrl: string;
  /** Replaced by a fake in tests, so no mocking library is needed. */
  fetch?: (request: Request) => Promise<Response>;
  headers?: Record<string, string>;
};

export function createApi({ baseUrl, fetch, headers }: ApiOptions): Api {
  const api = createClient<paths>({ baseUrl, fetch, headers });
  api.use(writeHeaders);
  return api;
}
