const FALLBACK = "/jobs";

// Characters a browser strips or rewrites before it reads a URL: "/\t/evil.example" becomes "//evil.example", and "\" becomes "/".
const CONTROL_OR_BACKSLASH = /[\u0000-\u001f\u007f\\]/;

/**
 * Where to go after signing in, from the `next` parameter. Only a path on this site is accepted: it must start with a
 * single "/", and nothing a browser would read as another host or a scheme. Anything else goes to /jobs (threat T-01-21).
 */
export function safeNextPath(raw: string | string[] | null | undefined): string {
  const candidate = Array.isArray(raw) ? raw[0] : raw;
  if (typeof candidate !== "string" || !candidate.startsWith("/") || candidate.startsWith("//")) return FALLBACK;
  if (CONTROL_OR_BACKSLASH.test(candidate)) return FALLBACK;
  if (candidate === "/sign-in" || candidate.startsWith("/sign-in?") || candidate.startsWith("/sign-in/")) return FALLBACK; // no loop
  try {
    // Read the way a browser would: if it resolves to another origin, it was not a path.
    return new URL(candidate, "http://site.invalid").origin === "http://site.invalid" ? candidate : FALLBACK;
  } catch {
    return FALLBACK;
  }
}
