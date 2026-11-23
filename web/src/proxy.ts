import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/api/types";

/**
 * An optimistic check, not a security boundary (FRONTEND_UI 9.6): no session cookie means there is nothing to render for
 * this person, so send them to sign in and bring them back. The API still decides who may see what.
 *
 * Server mode guards /control only; every other page is public. In local mode (APP_MODE=local, the same setting the API
 * reads) nobody signs in: a request with no session goes to /local-session, which opens the owner's session and comes back.
 *
 * Next requires an absolute address here and builds it from the Host the request arrived with, so the reverse proxy must
 * pass the Host header through unchanged (Caddy's default) for this to point at the public address.
 */
export function proxy(request: NextRequest) {
  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();
  const local = process.env.APP_MODE === "local";
  const { pathname, search } = request.nextUrl;
  if (!local && pathname !== "/control" && !pathname.startsWith("/control/")) return NextResponse.next();
  const target = request.nextUrl.clone();
  target.pathname = local ? "/local-session" : "/sign-in";
  target.search = `?next=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(target);
}

// Every page except the two that open a session, the framework's own files, anything with a file extension, and the paths
// the reverse proxy sends to the API and never to this server (it is still listed for `next dev`, which has no proxy).
export const config = { matcher: ["/((?!_next/|v1/|mcp|healthz|sign-in|local-session|.*\\..*).*)"] };
