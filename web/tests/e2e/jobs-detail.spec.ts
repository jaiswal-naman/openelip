import { expect, test, type Locator, type Page } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { seedJobs, type SeededJobs } from "./fixtures/seed-jobs";

/**
 * The public job page (IDX-05, IDX-06, IDX-08): a job's facts, its requirements, and the exact words a requirement quotes
 * marked in the posting. Every test needs the invented jobs of fixtures/jobs-index.json, which are added to the stack
 * first and are not added to an index that holds real jobs (then these tests are skipped).
 */

let seeded: SeededJobs | null = null;

test.beforeAll(() => {
  seeded = seedJobs();
});

test.beforeEach(() => {
  test.skip(seeded === null, "needs the invented jobs, which are not added to an index that holds real ones (E2E_SEED_JOBS=always overrides)");
});

/** The address of an invented job, by its external id in jobs-index.json. */
const jobAddress = (externalId: string) => `/jobs/${seeded![externalId]}`;

/** Presses Tab from the top of the page until the target has the focus: the keyboard-only way to reach it. */
async function tabTo(page: Page, target: Locator) {
  for (let presses = 0; presses < 60; presses++) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  throw new Error("the keyboard never reached the target");
}

test("shows the job's facts, where it came from, when it was verified open, and its requirements as Required and Preferred", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-requirements"));
  await expect(page.getByRole("heading", { level: 1, name: "Quokka Backend Software Engineer" })).toBeVisible();
  await expect(page.getByText("Quillfeather Labs", { exact: true })).toBeVisible();
  await expect(page.getByText("Pune, India", { exact: true })).toBeVisible();
  await expect(page.getByText("Employment type: Full time")).toBeVisible();
  await expect(page.getByText(/^Pay: USD.120,000 to USD.150,000 a year$/)).toBeVisible();
  await expect(page.getByText("Source: Greenhouse")).toBeVisible();
  await expect(page.getByText(/^Verified open/)).toBeVisible();
  await expect(page.locator("time").first()).toHaveAttribute("datetime", /^2026-11-22T12:21:54/);

  await expect(page.getByRole("heading", { level: 2, name: "Requirements" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 3, name: "Required (2)" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 3, name: "Preferred (2)" })).toBeVisible();
  await expect(page.locator("mark")).toHaveCount(0); // nothing is highlighted until a requirement is chosen

  const link = page.getByRole("link", { name: /View posting for Quokka Backend Software Engineer/ });
  await expect(link).toHaveAttribute("href", /^https:\/\/jobs\.example\.test\//);
  await expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
  await expect(link).toHaveAttribute("target", "_blank");
});

test("clicking a requirement marks its exact words, keeps the focus on the button, and survives a reload", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-requirements"));
  const button = page.getByRole("button", { name: /5 years of Python experience/ });
  await button.click();

  await expect(page.locator("mark")).toHaveCount(1);
  await expect(page.locator("mark")).toHaveText("5 years of Python experience");
  await expect(page.locator("mark")).toBeInViewport();
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(button).toBeFocused();
  await expect(page.getByRole("region", { name: "Job description" }).getByRole("status")).toContainText("Job sentence highlighted.");
  await expect(page).toHaveURL(/\/jobs\/\d+\?req=1$/);

  await page.reload();
  await expect(page.locator("mark")).toHaveText("5 years of Python experience");
  await expect(button).toHaveAttribute("aria-pressed", "true");

  // Another requirement moves the mark; pressing the selected one again clears it and the address.
  const other = page.getByRole("button", { name: /Knowing Go/ });
  await other.click();
  await expect(page.locator("mark")).toHaveText("Knowing Go");
  await expect(page).toHaveURL(/\?req=4$/);
  await other.click();
  await expect(page.locator("mark")).toHaveCount(0);
  await expect(page).toHaveURL(/\/jobs\/\d+$/);
});

test("marks the exact words after emoji, which are one character to the database and two to JavaScript", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-emoji"));
  await page.getByRole("button", { name: /3 years of Python/ }).click();
  await expect(page.locator("mark")).toHaveText("3 years of Python");
  await page.getByRole("button", { name: /Experience with Kubernetes/ }).click();
  await expect(page.locator("mark")).toHaveText("Experience with Kubernetes");
  await expect(page.locator("mark")).toHaveCount(1);
});

test("opening the page with ?req= highlights that requirement, and ignores a number that is none", async ({ page }) => {
  await page.goto(`${jobAddress("inv-quokka-requirements")}?req=3`);
  await expect(page.locator("mark")).toHaveText("Familiarity with Kubernetes");
  await expect(page.getByRole("button", { name: /Familiarity with Kubernetes/ })).toHaveAttribute("aria-pressed", "true");

  await page.goto(`${jobAddress("inv-quokka-requirements")}?req=99`);
  await expect(page.getByRole("heading", { level: 3, name: "Required (2)" })).toBeVisible();
  await expect(page.locator("mark")).toHaveCount(0);
  await expect(page.locator('button[aria-pressed="true"]')).toHaveCount(0);
});

test("a requirement can be chosen with the keyboard alone", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-requirements"));
  const button = page.getByRole("button", { name: /Familiarity with Kubernetes/ });
  await tabTo(page, button);

  await page.keyboard.press("Enter");
  await expect(page.locator("mark")).toHaveText("Familiarity with Kubernetes");
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(button).toBeFocused();

  await page.keyboard.press("Space");
  await expect(page.locator("mark")).toHaveCount(0);
  await expect(button).toBeFocused();
});

test("a closed job shows Closed with the date and offers no link to the posting", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-closed"));
  await expect(page.getByRole("heading", { level: 1, name: "Quokka Retired Software Engineer" })).toBeVisible();
  await expect(page.getByText("Closed", { exact: true })).toBeVisible();
  await expect(page.getByText(/This job closed on/)).toBeVisible();
  await expect(page.locator("time[datetime^='2026-11-21T06:12:00']")).toBeVisible();
  await expect(page.getByRole("link", { name: /View posting/ })).toHaveCount(0);
});

test("a job nobody has read for requirements says so, and what it does not state stays unstated", async ({ page }) => {
  await page.goto(jobAddress("inv-quokka-unknown"));
  await expect(page.getByText("Requirements have not been extracted for this version yet.")).toBeVisible();
  await expect(page.getByText("Location: unknown")).toBeVisible();
  await expect(page.getByText("Remote: not stated")).toBeVisible();
  await expect(page.getByText("Employment type: Not stated in the posting")).toBeVisible();
  await expect(page.getByText("Pay: Not stated in the posting")).toBeVisible();
});

test.describe("an address that is not a job this page may show", () => {
  async function expectNotFound(page: Page, address: string) {
    await page.goto(address);
    await expect(page.getByRole("heading", { level: 1, name: "Not found" })).toBeVisible();
    await expect(page.getByText("You do not have access to this, or it does not exist.")).toBeVisible();
  }

  test("an id that is not in the index", async ({ page }) => {
    await expectNotFound(page, "/jobs/999999999");
  });

  test("an address that is not an id", async ({ page }) => {
    await expectNotFound(page, "/jobs/not-a-number");
    await expectNotFound(page, "/jobs/0");
  });

  test("a job that is not listed", async ({ page }) => {
    await expectNotFound(page, jobAddress("inv-quokka-unlisted"));
  });
});

for (const colorScheme of ["light", "dark"] as const) {
  test.describe(`${colorScheme} theme`, () => {
    test.use({ colorScheme });

    test("the job page has no axe violations, with a requirement highlighted", async ({ page }) => {
      await page.goto(`${jobAddress("inv-quokka-requirements")}?req=1`);
      await expect(page.locator("mark")).toHaveText("5 years of Python experience");
      await expectNoAxeViolations(page);
    });

    test("a closed job's page has no axe violations", async ({ page }) => {
      await page.goto(jobAddress("inv-quokka-closed"));
      await expect(page.getByText("Closed", { exact: true })).toBeVisible();
      await expectNoAxeViolations(page);
    });
  });
}
