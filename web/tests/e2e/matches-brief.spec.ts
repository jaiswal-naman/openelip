import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { BASE_URL, freshAddress, MAILPIT_URL, signInThroughApi } from "./helpers";

/*
 * Phase 4, the matches and the morning brief, as a candidate sees them (the roadmap demo; SC1 to SC4 in the browser).
 *
 * One person, on invented data. A fresh address signs in, the demo seed (`python -m app.cli demo seed-matches`, run inside the
 * worker container) gives that account an invented record, four invented postings and a match run on a recorded model, and
 * composes the brief; the brief email arrives in Mailpit; its first link opens a match; a requirement row is chosen with the
 * keyboard and the exact job sentence and resume line are marked; "Why not" lists the posting the constraints removed; the
 * posting that breaks a constraint is nowhere else; every page passes axe in both themes; and the unsubscribe link stops the
 * email with one click.
 *
 * It needs a stack that holds no other postings, the fake model provider, and a database whose name ends in _test, because the
 * seed refuses anything else. That is the second stack docker-compose.demo.yml describes, so it runs only with DEMO_STACK=1:
 *
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait
 *   cd web && DEMO_STACK=1 BASE_URL=http://localhost:8090 MAILPIT_URL=http://localhost:8026 \
 *     pnpm exec playwright test tests/e2e/matches-brief.spec.ts --project=chromium
 *
 * The standard stack of the other specs (database `app`) skips it, and .github/workflows/e2e.yml runs it on its own stack in a
 * second job. It creates an account and sends mail, so point it at a throwaway stack, as the Playwright config says.
 */

const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");
const DEMO_COMPOSE = ["compose", "-p", "openelip-demo", "-f", "docker-compose.yml", "-f", "docker-compose.demo.yml"];
const SCHEMES = ["light", "dark"] as const;

const EXCLUDED = "Demo posting (invented): Onsite Support Engineer"; // on site in Germany: the demo constraints exclude it
const FIRST_MATCH = "Demo posting (invented): Quillscript Platform Engineer";
const STOPPED = "You will not receive the morning brief by email. Decisions still appear in the app.";

/** Runs the seed in the worker container, the way the manual demo does, and returns what it printed. */
function seedDemo(address: string): string {
  return execFileSync("docker", [...DEMO_COMPOSE, "exec", "-T", "worker", "python", "-m", "app.cli", "demo", "seed-matches", "--email", address], {
    cwd: COMPOSE_DIR,
    encoding: "utf8",
  });
}

/** The text of the newest morning brief sent to `address`, read through Mailpit's HTTP API. */
async function briefEmail(request: APIRequestContext, address: string): Promise<string> {
  let body = "";
  await expect(async () => {
    const query = encodeURIComponent(`to:"${address}" subject:"Your morning brief"`);
    const found = await request.get(`${MAILPIT_URL}/api/v1/search?query=${query}`);
    expect(found.ok()).toBe(true);
    const { messages } = (await found.json()) as { messages: { ID: string }[] };
    expect(messages.length).toBeGreaterThan(0);
    const message = await request.get(`${MAILPIT_URL}/api/v1/message/${messages[0].ID}`);
    body = ((await message.json()) as { Text: string }).Text;
    expect(body).toContain("Your morning brief");
  }).toPass({ timeout: 60_000 });
  return body;
}

/** The page as it is now has no axe violations in either theme. Wait until it shows what you mean to scan before calling this. */
async function expectNoViolationsInBothThemes(page: Page) {
  for (const scheme of SCHEMES) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoAxeViolations(page);
  }
}

test.describe("the matches and morning brief demo", () => {
  test.skip(process.env.DEMO_STACK !== "1", "needs the demo stack (invented postings, fake model, a _test database): run with DEMO_STACK=1 (see the top of this file)");

  test("seed, read the brief email, open a match, check the evidence, see why not, and unsubscribe", async ({ browser }) => {
    test.setTimeout(300_000);
    const address = freshAddress(); // example.com, not example.test: the relay never delivers to example.test
    const context = await browser.newContext({ baseURL: BASE_URL });
    const page = await context.newPage();
    let email = "";
    let matchIds: string[] = [];

    await test.step("sign in, and have the demo seeded for this account", async () => {
      await signInThroughApi(context, address);
      const report = seedDemo(address);
      expect(report).toContain("Seeded the invented demo");
      expect(report).toMatch(/3 shown today, 1 filtered by the constraints/);
    });

    await test.step("the morning brief email arrives in Mailpit with its three matches, and not the excluded posting", async () => {
      email = await briefEmail(context.request, address);
      matchIds = [...email.matchAll(/\/app\/matches\/([0-9a-f-]{36})/g)].map((found) => found[1]);
      expect(matchIds).toHaveLength(3);
      expect(email).toContain("Stop this email: ");
      expect(email).not.toContain(EXCLUDED);
    });

    await test.step("the first link opens the match: its job, its requirement table and both texts", async () => {
      await page.goto(`/app/matches/${matchIds[0]}`);
      await expect(page.getByRole("heading", { level: 1, name: FIRST_MATCH })).toBeVisible();
      const table = page.getByRole("grid", { name: "Requirements and your evidence" });
      await expect(table.getByRole("row")).toHaveCount(5); // the header and the four requirements
      await expect(table.getByRole("row", { name: /shipped services in Quillscript and PostgreSQL/ })).toContainText("Met");
      await expect(table.getByRole("row", { name: /Kubernetes in production/ })).toContainText("No evidence");
      await expect(page.getByRole("table", { name: "Your hard constraints and whether this job meets them" })).toContainText("Satisfied");
      await expect(page.locator("mark")).toHaveCount(0); // nothing is marked until a row is chosen
    });

    await test.step("a row chosen with the keyboard marks the exact job sentence and the cited resume line, and says so", async () => {
      const row = page.getByRole("row", { name: /shipped services in Quillscript and PostgreSQL/ });
      await row.focus();
      await page.keyboard.press("Enter");
      await expect(row).toHaveAttribute("aria-selected", "true");

      const marks = page.locator("mark");
      await expect(marks).toHaveCount(2);
      await expect(marks.nth(0)).toHaveText("shipped services in Quillscript and PostgreSQL");
      await expect(marks.nth(1)).toHaveText("Skills: Quillscript, PostgreSQL.");
      // exact: the section around both panes is named "Job text and your resume", which contains both of these names
      await expect(page.getByRole("region", { name: "Job text", exact: true }).locator("mark")).toHaveCount(1);
      await expect(page.getByRole("region", { name: "Your resume", exact: true }).locator("mark")).toHaveCount(1);
      await expect(page.getByRole("status").filter({ hasText: "Job sentence and resume line 3 highlighted." })).toHaveCount(1);
      await expect(page).toHaveURL(/[?&]req=\d+/); // a reload or a shared link lands on the same highlight
      await expectNoViolationsInBothThemes(page);

      await page.keyboard.press("Enter"); // pressing the chosen row again clears it
      await expect(page.locator("mark")).toHaveCount(0);
    });

    await test.step("Today opens on the decisions, and every tab passes axe in both themes", async () => {
      await page.goto("/app/today");
      await expect(page.getByRole("heading", { level: 1, name: /^Today, / })).toBeVisible();
      await expect(page.getByRole("tab", { name: /^Decisions \(3\)$/ })).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("article")).toHaveCount(3);
      await expect(page.getByRole("article").first().getByRole("link", { name: "Open" })).toBeVisible();
      await expectNoViolationsInBothThemes(page);

      await page.getByRole("tab", { name: /^Matches \(3\)$/ }).click();
      await expect(page).toHaveURL(/view=matches/);
      await expect(page.getByRole("article")).toHaveCount(3);
      await expect(page.getByRole("article").first()).toContainText("Meets");
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("the posting the constraints removed is in Why not, labelled, and on no other page", async () => {
      await page.getByRole("tab", { name: "Why not" }).click();
      await expect(page).toHaveURL(/view=why-not/);
      const filtered = page.getByRole("region", { name: "Filtered by your constraints (1)" });
      await expect(filtered).toContainText(EXCLUDED);
      await expect(filtered).toContainText("Only jobs in the United States.");
      await expectNoViolationsInBothThemes(page);

      // invariant 1 in the browser: it is on no match card, no brief decision and no match page, and not in what the API says
      await page.goto("/app/today?view=matches");
      await expect(page.getByRole("article")).toHaveCount(3);
      await expect(page.getByText(EXCLUDED)).toHaveCount(0);
      await page.goto("/app/today?view=decisions");
      await expect(page.getByRole("article")).toHaveCount(3);
      await expect(page.getByText(EXCLUDED)).toHaveCount(0);
      for (const id of matchIds) {
        await page.goto(`/app/matches/${id}`);
        await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(EXCLUDED);
        await expect(page.getByText(EXCLUDED)).toHaveCount(0);
      }
      const today = await context.request.get(`${BASE_URL}/v1/matches`);
      expect(today.ok()).toBe(true);
      expect(JSON.stringify(await today.json())).not.toContain("Onsite Support Engineer");
    });

    await test.step("the brief settings pass axe in both themes", async () => {
      await page.goto("/app/settings?section=brief");
      await expect(page.getByRole("heading", { level: 2, name: "Morning brief" })).toBeVisible();
      await expect(page.getByRole("switch", { name: "Email me the brief" })).toBeChecked(); // the seed agreed to the email
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("the unsubscribe link does nothing until its button is pressed, then stops the email with one click", async () => {
      const link = /\/unsubscribe\?t=\S+/.exec(email)?.[0];
      expect(link).toBeDefined();
      await page.goto(link as string);
      await expect(page.getByRole("heading", { level: 1, name: "Stop the morning brief email" })).toBeVisible();
      await expect(page.getByText(/Nothing has changed yet/)).toBeVisible();
      const before = await context.request.get(`${BASE_URL}/v1/me`);
      const consents = ((await before.json()) as { data: { consents: { purpose: string; granted: boolean }[] } }).data.consents;
      expect(consents.find((consent) => consent.purpose === "brief_email")?.granted).toBe(true); // loading the page changed nothing
      await expectNoViolationsInBothThemes(page);

      await page.getByRole("button", { name: "Stop the morning brief email" }).click();
      await expect(page.getByText(STOPPED)).toBeVisible();
      await expect(page.getByRole("button", { name: "Undo" })).toBeVisible(); // signed in here, so Undo acts at once
      await expect(page.getByRole("button", { name: "Choose weekly instead" })).toBeVisible();
      await expectNoViolationsInBothThemes(page);

      const after = await context.request.get(`${BASE_URL}/v1/me`);
      const stopped = ((await after.json()) as { data: { consents: { purpose: string; granted: boolean }[] } }).data.consents;
      expect(stopped.find((consent) => consent.purpose === "brief_email")?.granted).toBe(false);
    });

    await context.close();
  });
});
