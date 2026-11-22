import { afterEach, describe, expect, it, vi } from "vitest";
import { fail, fakeFetch, ok } from "@/test/render";
import { createApi } from "./client";
import { ApiFailure, unwrap, unwrapEmpty } from "./errors";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const me = { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "a@b.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("writes", () => {
  it("send a fresh Idempotency-Key and JSON on every write (API.md 1.2 and 1.6)", async () => {
    const { fetch, calls } = fakeFetch({
      "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }, 202),
      "DELETE /v1/auth/sessions/current": new Response(null, { status: 204 }),
    });
    const api = createApi({ baseUrl: "http://localhost", fetch });

    await api.POST("/v1/auth/codes", { body: { email: "a@b.test" } });
    await api.POST("/v1/auth/codes", { body: { email: "a@b.test" } });
    await api.DELETE("/v1/auth/sessions/current");

    const keys = calls.map((request) => request.headers.get("Idempotency-Key"));
    expect(keys.every((key) => key !== null && UUID.test(key))).toBe(true);
    expect(new Set(keys).size).toBe(3);
    expect(calls.every((request) => request.headers.get("Content-Type") === "application/json")).toBe(true);
  });

  it("keep a key the caller chose, so a retry can repeat it", async () => {
    const { fetch, calls } = fakeFetch({ "DELETE /v1/auth/sessions/current": new Response(null, { status: 204 }) });
    const api = createApi({ baseUrl: "http://localhost", fetch });
    await api.DELETE("/v1/auth/sessions/current", { params: { header: { "idempotency-key": "my-own-key-123" } } });
    expect(calls[0].headers.get("Idempotency-Key")).toBe("my-own-key-123");
  });

  it("are the only requests that carry the key: a read does not", async () => {
    const { fetch, calls } = fakeFetch({ "GET /v1/me": ok(me) });
    await createApi({ baseUrl: "http://localhost", fetch }).GET("/v1/me");
    expect(calls[0].headers.has("Idempotency-Key")).toBe(false);
  });
});

describe("unwrap", () => {
  const getMe = (routes: Parameters<typeof fakeFetch>[0]) => {
    const { fetch } = fakeFetch(routes);
    return unwrap(createApi({ baseUrl: "http://localhost", fetch }).GET("/v1/me"));
  };

  it("returns the envelope's data", async () => {
    await expect(getMe({ "GET /v1/me": ok(me) })).resolves.toEqual(me);
  });

  it("throws the API's code, the request id and the details", async () => {
    const error = await getMe({ "GET /v1/me": fail(401, "unauthenticated") }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiFailure);
    expect(error).toMatchObject({ status: 401, code: "unauthenticated", requestId: "req_test" });
  });

  it("reads how long to wait from Retry-After", async () => {
    const error = await getMe({ "GET /v1/me": fail(429, "rate_limited", {}, { "Retry-After": "840" }) }).catch((e: unknown) => e);
    expect(error).toMatchObject({ code: "rate_limited", retryAfterSeconds: 840 });
  });

  it("falls back to details.retry_after_seconds when there is no header", async () => {
    const error = await getMe({ "GET /v1/me": fail(429, "rate_limited", { retry_after_seconds: 90 }) }).catch((e: unknown) => e);
    expect(error).toMatchObject({ retryAfterSeconds: 90 });
  });

  it("keeps the attempts that are left on a wrong code", async () => {
    const error = await getMe({ "GET /v1/me": fail(401, "code_invalid", { attempts_remaining: 3 }) }).catch((e: unknown) => e);
    expect(error).toMatchObject({ code: "code_invalid", details: { attempts_remaining: 3 } });
  });

  it("names a proxy's bad gateway page dependency_unavailable instead of crashing on it", async () => {
    const page = new Response("<html>Bad gateway</html>", { status: 502, headers: { "Content-Type": "text/html" } });
    await expect(getMe({ "GET /v1/me": page })).rejects.toMatchObject({ code: "dependency_unavailable", status: 502 });
  });

  it("calls an answer that is not in the envelope unreadable", async () => {
    await expect(getMe({ "GET /v1/me": Response.json({ unexpected: true }) })).rejects.toMatchObject({ code: "unreadable_response" });
  });

  it("turns a network failure into network_error without the browser's own message", async () => {
    const api = createApi({
      baseUrl: "http://localhost",
      fetch: () => Promise.reject(new TypeError("Failed to fetch https://secret.internal/v1/me")),
    });
    const error = await unwrap(api.GET("/v1/me")).catch((e: unknown) => e);
    expect(error).toMatchObject({ code: "network_error", status: 0 });
    expect(String((error as Error).message)).not.toContain("secret");
  });

  it("unwrapEmpty accepts a 204 and refuses an error", async () => {
    const ok204 = fakeFetch({ "DELETE /v1/auth/sessions/current": new Response(null, { status: 204 }) });
    await expect(unwrapEmpty(createApi({ baseUrl: "http://localhost", fetch: ok204.fetch }).DELETE("/v1/auth/sessions/current"))).resolves.toBeUndefined();
    const bad = fakeFetch({ "DELETE /v1/auth/sessions/current": fail(401, "unauthenticated") });
    await expect(unwrapEmpty(createApi({ baseUrl: "http://localhost", fetch: bad.fetch }).DELETE("/v1/auth/sessions/current"))).rejects.toMatchObject({ code: "unauthenticated" });
  });
});

describe("serverApi", () => {
  async function serverCall(cookieStore: Record<string, string>) {
    const seen: Request[] = [];
    vi.doMock("next/headers", () => ({
      cookies: async () => ({
        get: (name: string) => (name in cookieStore ? { name, value: cookieStore[name] } : undefined),
        has: (name: string) => name in cookieStore,
      }),
    }));
    vi.stubGlobal("fetch", async (request: Request) => {
      seen.push(request);
      return ok(me);
    });
    vi.stubEnv("API_INTERNAL_URL", "http://api:8000");
    const { serverApi } = await import("./server");
    await (await serverApi()).GET("/v1/me");
    return seen[0];
  }

  it("calls the internal URL and forwards only the session cookie", async () => {
    const request = await serverCall({ app_session: "abc123", theme_hint: "dark", other: "x" });
    expect(request.url).toBe("http://api:8000/v1/me");
    expect(request.headers.get("cookie")).toBe("app_session=abc123");
  });

  it("sends no cookie when there is no session", async () => {
    const request = await serverCall({ other: "x" });
    expect(request.headers.has("cookie")).toBe(false);
  });
});
