import { expect, test } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { seedJobs, type SeededJobs } from "./fixtures/seed-jobs";

/**
 * The public job search (IDX-04, IDX-05, IDX-06): a plain-English query, what every result says about itself, paging by
 * the API's cursor, and opening a result. The invented jobs of fixtures/jobs-index.json are added to the stack first
 * (they are not added to an index that holds real jobs). The first test passes against either kind of index; the others
 * need the invented jobs and are skipped without them.
 */

// In keyword mode every word must be in a posting, so each invented posting says "software engineer".
const QUERY = "software engineer";
const SEARCH_ADDRESS = /\/jobs\?q=software\+engineer$/;

let seeded: SeededJobs | null = null;

test.beforeAll(() => {
  seeded = seedJobs();
});

test("search in plain English, see where each result came from, load more, and open one", async ({ page, request }) => {
  const modeNow = async () => {
    const answer = await request.get(`/v1/jobs?q=${encodeURIComponent(QUERY)}&limit=1`);
    expect(answer.ok()).toBe(true);
    return ((await answer.json()) as { meta: { mode: string } }).meta.mode;
  };
  const before = await modeNow();
  await page.goto("/jobs");
  await page.getByRole("searchbox", { name: "Search open jobs" }).fill(QUERY);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(SEARCH_ADDRESS);

  const results = page.getByRole("region", { name: "Results" });
  const cards = results.getByRole("listitem");
  await expect(cards).toHaveCount(20);
  await expect(results).toHaveAttribute("aria-busy", "false");

  // Every result says which board it came from and when it was last seen open, with the time as a <time> element.
  for (const card of await cards.all()) {
    await expect(card).toContainText(/Source: (Greenhouse|Lever|Ashby)/);
    await expect(card).toContainText(/Verified open/);
    await expect(card.locator("time")).toHaveAttribute("datetime", /^\d{4}-\d{2}-\d{2}T/);
  }

  // The note is there exactly when only keywords were searched. The page's first page is fetched by the server as it
  // renders, between the two answers here, and the embed sweep only ever turns keyword search into hybrid: when both
  // answers agree the page had that mode too. When the sweep reached the stack's jobs in between, either is right.
  const after = await modeNow();
  const note = results.getByText("Keyword results only right now.");
  if (before === after && after === "keyword") await expect(note).toBeVisible();
  else if (before === after) await expect(note).toHaveCount(0);

  // Load more asks for the next page with the API's cursor and the same query, adds the rows, and leaves the cursor out of the address.
  const nextPage = page.waitForRequest((r) => r.url().includes("/v1/jobs?") && r.url().includes("cursor="));
  await results.getByRole("button", { name: "Load more" }).click();
  expect(new URL((await nextPage).url()).searchParams.get("q")).toBe(QUERY);
  await expect.poll(() => cards.count()).toBeGreaterThan(20);
  await expect(page).toHaveURL(SEARCH_ADDRESS);

  // Open the first result: its own page says the same about where it came from, and shows its text.
  const heading = cards.first().getByRole("heading", { level: 2 });
  const title = (await heading.innerText()).trim();
  await heading.getByRole("link").click();
  await expect(page).toHaveURL(/\/jobs\/\d+$/);
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  await expect(page.getByText(/^Source: (Greenhouse|Lever|Ashby)$/)).toBeVisible();
  await expect(page.getByText(/^Verified open/)).toBeVisible();
  await expect(page.getByRole("region", { name: "Job description" })).toBeVisible();
});

test("a job whose place could not be read says Location: unknown, and not a guess", async ({ page }) => {
  test.skip(seeded === null, "needs the invented jobs, which are not added to an index that holds real ones (E2E_SEED_JOBS=always overrides)");
  await page.goto("/jobs?q=Quokka+Platform+Engineer");
  const card = page.getByRole("region", { name: "Results" }).getByRole("listitem").filter({ hasText: "Quokka Platform Engineer" });
  await expect(card).toHaveCount(1);
  await expect(card).toContainText("Location: unknown");
  await expect(card).toContainText("Remote: not stated");
});

for (const colorScheme of ["light", "dark"] as const) {
  test.describe(`${colorScheme} theme`, () => {
    test.use({ colorScheme });

    test("the results have no axe violations, before and after Load more", async ({ page }) => {
      await page.goto("/jobs?q=software+engineer");
      const results = page.getByRole("region", { name: "Results" });
      await expect(results.getByRole("listitem")).toHaveCount(20);
      await expect(results).toHaveAttribute("aria-busy", "false");
      await expectNoAxeViolations(page);

      await results.getByRole("button", { name: "Load more" }).click();
      await expect.poll(() => results.getByRole("listitem").count()).toBeGreaterThan(20);
      await expect(results).toHaveAttribute("aria-busy", "false");
      await expectNoAxeViolations(page);
    });
  });
}
