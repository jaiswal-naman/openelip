// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
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

  it("covers /control and below, and nothing public", () => {
    expect(config.matcher).toEqual(["/control/:path*"]);
  });
});
