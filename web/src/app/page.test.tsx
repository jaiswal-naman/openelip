import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApi } from "@/lib/api/client";
import { fail, fakeFetch, ok } from "@/test/render";
import Home from "./page";

// Where a person in local mode starts. redirect() ends the render by throwing in Next; here it throws the target.
let routes: Record<string, Response> = {};
vi.mock("@/lib/api/server", () => ({
  serverApi: async () => createApi({ baseUrl: "http://localhost", fetch: fakeFetch(routes).fetch }),
}));
vi.mock("next/navigation", () => ({
  redirect: (to: string) => {
    throw new Error(`redirect ${to}`);
  },
}));

const record = (status: string | null) => ({ documents: status === null ? [] : [{ id: "d1", status }] });

describe("the home page in local mode", () => {
  beforeEach(() => vi.stubEnv("APP_MODE", "local"));
  afterEach(() => vi.unstubAllEnvs());

  it("sends a record with no resume read yet to onboarding, and one with a resume to Today", async () => {
    routes = { "GET /v1/me/record": ok(record(null)) };
    await expect(Home()).rejects.toThrow("redirect /onboarding");
    routes = { "GET /v1/me/record": ok(record("succeeded")) };
    await expect(Home()).rejects.toThrow("redirect /app/today");
  });

  it("opens the owner's session first when the cookie is one the API no longer knows, then decides here again", async () => {
    // A reinstall with a new database: the browser still holds the old cookie, so the proxy let the visit through
    routes = { "GET /v1/me/record": fail(401, "unauthenticated") };
    await expect(Home()).rejects.toThrow("redirect /local-session?next=%2F");
  });

  it("goes to Today when the API cannot answer: not knowing is no reason to repeat the setup", async () => {
    routes = { "GET /v1/me/record": fail(503, "dependency_unavailable") };
    await expect(Home()).rejects.toThrow("redirect /app/today");
  });
});
