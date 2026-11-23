// @vitest-environment node
// The browser tests' job seed writes as the database's superuser into whatever stack `docker compose` picks. This file
// sits under src because vitest only reads src, and Playwright would run a *.test.ts under tests/e2e as a browser test.
import { afterEach, describe, expect, it, vi } from "vitest";
import { seedRefusal } from "../../tests/e2e/fixtures/seed-jobs";

describe("seedRefusal (when the browser tests may add invented jobs to a stack)", () => {
  it("refuses the default stack, which holds the real index, and a stack nobody named", () => {
    for (const project of [undefined, "", "openelip"]) {
      expect(seedRefusal(project, "app")).toMatch(/COMPOSE_PROJECT_NAME/);
    }
  });

  it("goes ahead on a throwaway stack: the fresh-clone check's project, or any other name", () => {
    expect(seedRefusal("openelip_fresh", "app")).toBeNull(); // scripts/fresh-clone-check.ps1 -WithBrowserTests
    expect(seedRefusal("openelip_e2e", "app")).toBeNull(); // .github/workflows/e2e.yml
  });

  it("goes ahead on a scratch database whatever the project is, and only on a name that ends in _test", () => {
    expect(seedRefusal(undefined, "app_rehearsal_test")).toBeNull();
    expect(seedRefusal("openelip", "app_p2_test")).toBeNull();
    expect(seedRefusal("openelip", "app_test_backup")).not.toBeNull();
    expect(seedRefusal("openelip", "testing")).not.toBeNull();
  });
});

describe("seedJobs", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.doUnmock("node:child_process");
    vi.resetModules();
  });

  it("stops before it runs anything when the stack is the default one", async () => {
    const execFileSync = vi.fn(() => {
      throw new Error("docker was called");
    });
    vi.doMock("node:child_process", () => ({ execFileSync }));
    vi.stubEnv("COMPOSE_PROJECT_NAME", "openelip");
    vi.stubEnv("E2E_DB_NAME", "app");
    vi.resetModules();
    const { seedJobs } = await import("../../tests/e2e/fixtures/seed-jobs");

    expect(() => seedJobs()).toThrow(/refusing to seed/);
    expect(execFileSync).not.toHaveBeenCalled();
  });
});
