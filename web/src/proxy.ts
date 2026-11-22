import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/api/types";

/**
 * An optimistic check, not a security boundary (FRONTEND_UI 9.6): no session cookie means there is nothing to render for
 * this person, so send them to sign in and bring them back. The API still decides who may see what.
 *
 * Next requires an absolute address here and builds it from the Host the request arrived with, so the reverse proxy must
 * pass the Host header through unchanged (Caddy's default) for this to point at the public address.
 */
export function proxy(request: NextRequest) {
  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();
  const signIn = request.nextUrl.clone();
  signIn.pathname = "/sign-in";
  signIn.search = `?next=${encodeURIComponent(request.nextUrl.pathname + request.nextUrl.search)}`;
  return NextResponse.redirect(signIn);
}

export const config = { matcher: ["/control/:path*"] };
