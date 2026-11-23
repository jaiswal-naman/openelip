// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { config, proxy } from "./proxy";

const request = (url: string, cookie?: string) => new NextRequest(url, { headers: cookie ? { cookie } : {} });

describe("proxy (the optimistic check in front of /control)", () => {
  it("sends someone with no session cookie to sign in, and brings them back to the same page", () => {
    const response = proxy(request("http://localhost:3000/control/costs?month=2026-11"));
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("http://localhost:3000/sign-in?next=%2Fcontrol%2Fcosts%3Fmonth%3D2026-11");
  });

  it("redirects to an absolute address on the host the request used, because Next refuses a relative one", () => {
    // Behind the reverse proxy this is the public address, as long as the proxy passes the Host header through
    const location = proxy(request("https://careers.example.test/control/costs")).headers.get("location");
    expect(location).toBe("https://careers.example.test/sign-in?next=%2Fcontrol%2Fcosts");
  });

  it("lets a request with a session cookie through, without asking the API", () => {
    const response = proxy(request("http://localhost:3000/control/costs", "app_session=opaque-value"));
    expect(response.headers.get("x-middleware-next")).toBe("1");
    expect(response.headers.has("location")).toBe(false);
  });

  it("treats a cookie with another name as no session", () => {
    expect(proxy(request("http://localhost:3000/control/costs", "theme=dark")).status).toBe(307);
  });

  it("in server mode guards /control and below, and nothing public", () => {
    expect(proxy(request("http://localhost:3000/control")).status).toBe(307);
    for (const path of ["/", "/jobs", "/jobs/12", "/trust", "/app/today", "/controls", "/sign-in"]) {
      expect(proxy(request(`http://localhost:3000${path}`)).headers.get("x-middleware-next"), path).toBe("1");
    }
  });
});

describe("proxy in local mode (APP_MODE=local): nobody signs in", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("sends a request with no session to /local-session and brings it back to the same page", () => {
    vi.stubEnv("APP_MODE", "local");
    for (const [path, next] of [
      ["/", "%2F"],
      ["/jobs?q=backend", "%2Fjobs%3Fq%3Dbackend"],
      ["/control/costs", "%2Fcontrol%2Fcosts"],
    ]) {
      const response = proxy(request(`http://localhost:8080${path}`));
      expect(response.status, path).toBe(307);
      expect(response.headers.get("location"), path).toBe(`http://localhost:8080/local-session?next=${next}`);
    }
  });

  it("lets a request with a session through", () => {
    vi.stubEnv("APP_MODE", "local");
    expect(proxy(request("http://localhost:8080/jobs", "app_session=opaque-value")).headers.get("x-middleware-next")).toBe("1");
  });

  it("any other value of APP_MODE is server mode", () => {
    vi.stubEnv("APP_MODE", "server");
    expect(proxy(request("http://localhost:8080/jobs")).headers.get("x-middleware-next")).toBe("1");
  });

  it("does not run for the pages that open a session, the framework's files or the API's paths", () => {
    const [pattern] = config.matcher;
    const matches = (path: string) => new RegExp(`^${pattern}$`).test(path);
    for (const path of ["/", "/jobs", "/jobs/12", "/setup", "/control/costs", "/app/today"]) expect(matches(path), path).toBe(true);
    for (const path of ["/sign-in", "/local-session", "/_next/static/chunk.js", "/v1/me", "/mcp", "/healthz", "/icon.svg", "/favicon.ico"]) {
      expect(matches(path), path).toBe(false);
    }
  });
});
