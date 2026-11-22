import { cookies } from "next/headers";
import { type Api, createApi } from "./client";
import { SESSION_COOKIE } from "./types";

const SERVER_TIMEOUT_MS = 5000; // a slow API must not hold up every page's first paint

/**
 * The API client for Server Components: it calls the API over the internal network and forwards only the session
 * cookie, so the API sees the same person the browser would.
 */
export async function serverApi(): Promise<Api> {
  const session = (await cookies()).get(SESSION_COOKIE)?.value;
  return createApi({
    baseUrl: process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000",
    headers: session ? { cookie: `${SESSION_COOKIE}=${session}` } : undefined,
    fetch: (request) => fetch(request, { cache: "no-store", signal: AbortSignal.timeout(SERVER_TIMEOUT_MS) }),
  });
}

export async function hasSessionCookie(): Promise<boolean> {
  return (await cookies()).has(SESSION_COOKIE);
}
