import { describe, expect, it, vi } from "vitest";
import { createApi } from "@/lib/api/client";
import { ApiFailure } from "@/lib/api/errors";
import { fail, fakeFetch, ok } from "@/test/render";
import { loadJob } from "./loadJob";
import { jobDetail } from "./testing";

// notFound() ends rendering by throwing; here it throws something the test can recognise.
vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NOT_FOUND_PAGE");
  },
}));

function apiWith(routes: Record<string, Response>) {
  const fake = fakeFetch(routes);
  return { api: createApi({ baseUrl: "http://localhost", fetch: fake.fetch }), calls: fake.calls };
}

describe("loadJob", () => {
  it("returns the job the API sends for the id", async () => {
    const { api, calls } = apiWith({ "GET /v1/jobs/7": ok(jobDetail({ id: 7, title: "Platform Engineer" })) });
    await expect(loadJob(api, "7")).resolves.toMatchObject({ id: 7, title: "Platform Engineer" });
    expect(calls).toHaveLength(1);
  });

  it("shows the not-found page for a job the API does not know, or does not list (threat T-02-38)", async () => {
    const { api } = apiWith({ "GET /v1/jobs/9": fail(404, "not_found") });
    await expect(loadJob(api, "9")).rejects.toThrow("NOT_FOUND_PAGE");
  });

  it("shows the not-found page for an id the API refuses with a 422", async () => {
    const { api } = apiWith({ "GET /v1/jobs/9": fail(422, "validation_failed") });
    await expect(loadJob(api, "9")).rejects.toThrow("NOT_FOUND_PAGE");
  });

  it("does not hide a failure that is not about the id: a service that is down is an error, not a missing job", async () => {
    const { api } = apiWith({ "GET /v1/jobs/9": fail(503, "dependency_unavailable") });
    await expect(loadJob(api, "9")).rejects.toMatchObject({ code: "dependency_unavailable" });
    await expect(loadJob(api, "9")).rejects.toBeInstanceOf(ApiFailure);
  });

  it("does not hide a lost connection either", async () => {
    const api = createApi({ baseUrl: "http://localhost", fetch: () => Promise.reject(new TypeError("Failed to fetch")) });
    await expect(loadJob(api, "9")).rejects.toMatchObject({ code: "network_error" });
  });

  it.each(["abc", "0", "007", "-5", "1e3", "12abc", "1.5", "", " 7", "7 ", "9".repeat(16), "0x10"])(
    "shows the not-found page for the address %j without asking the API (threat T-02-37)",
    async (id) => {
      const { api, calls } = apiWith({});
      await expect(loadJob(api, id)).rejects.toThrow("NOT_FOUND_PAGE");
      expect(calls).toHaveLength(0);
    },
  );

  it("asks for a whole-number id exactly as written, up to the largest one a number holds exactly", async () => {
    const { api, calls } = apiWith({
      "GET /v1/jobs/1": ok(jobDetail({ id: 1 })),
      "GET /v1/jobs/999999999999999": ok(jobDetail({ id: 999999999999999 })),
    });
    await loadJob(api, "1");
    await loadJob(api, "999999999999999");
    expect(calls.map((call) => new URL(call.url).pathname)).toEqual(["/v1/jobs/1", "/v1/jobs/999999999999999"]);
  });
});
