import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { BASE_URL, freshAddress, signInThroughApi } from "./helpers";

/*
 * Phase 5, chat, tailoring and applications, as a candidate sees them (the roadmap demo).
 *
 * One person, on invented data. A fresh address signs in, `demo seed-matches` and then `demo seed-tailor` (run inside the worker
 * container) give the account an invented record, four invented postings, a match run and a second invented resume. The person
 * opens the chat panel on Today, asks why the excluded job was ruled out and sees the cited answer, says a new skill and confirms the
 * proposed record change, opens the first match, tailors the resume and sees that 2 edits were discarded, approves the 3 that
 * remain, prepares the packet, downloads its PDF, records that they applied and sees it on /app/campaigns. Axe scans the open chat
 * panel, the tailoring page and the Applications page in both themes.
 *
 * The answers come from the fake model provider's default recordings (backend/app/platform/llm/recorded/*.json), which each process
 * of the demo stack consumes in order, once. So this spec needs a FRESH demo stack, and one run only: take the stack down with
 * its volumes and bring it up again before running it a second time. The stack also holds no other postings and its database name
 * ends in _test, because the seeds refuse anything else (docker-compose.demo.yml). Locally:
 *
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml down -v
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait
 *   cd web && DEMO_STACK=1 BASE_URL=http://localhost:8090 MAILPIT_URL=http://localhost:8026 \
 *     pnpm exec playwright test tests/e2e/chat-tailor-apply.spec.ts --project=chromium
 *
 * It skips unless DEMO_STACK=1, and .github/workflows/e2e.yml runs it in the demo job on a fresh stack after matches-brief.spec.ts.
 * It creates an account, so point it at a throwaway stack, as the Playwright config says.
 */

const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");
const DEMO_COMPOSE = ["compose", "-p", "openelip-demo", "-f", "docker-compose.yml", "-f", "docker-compose.demo.yml"];
const SCHEMES = ["light", "dark"] as const;

// The two messages the default recordings answer: the same file the backend demo test uses.
const MESSAGES = JSON.parse(fs.readFileSync(path.join(COMPOSE_DIR, "backend/app/demo_data/phase5/messages.json"), "utf8")) as {
  question: string;
  proposal: string;
};
const FIRST_MATCH = "Demo posting (invented): Quillscript Platform Engineer";

/** Runs one demo command in the worker container, the way the manual demo does, and returns what it printed. */
function demo(command: string, address: string): string {
  return execFileSync("docker", [...DEMO_COMPOSE, "exec", "-T", "worker", "python", "-m", "app.cli", "demo", command, "--email", address], {
    cwd: COMPOSE_DIR,
    encoding: "utf8",
  });
}

/** The page as it is now has no axe violations in either theme. Wait until it shows what you mean to scan before calling this. */
async function expectNoViolationsInBothThemes(page: Page) {
  for (const scheme of SCHEMES) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoAxeViolations(page);
  }
}

test.describe("the chat, tailoring and applications demo", () => {
  test.skip(process.env.DEMO_STACK !== "1", "needs a fresh demo stack (invented postings, fake model, a _test database): run with DEMO_STACK=1 (see the top of this file)");

  test("ask about a decision, confirm a proposal, tailor, approve, download the packet and record applying", async ({ browser }) => {
    test.setTimeout(300_000);
    const address = freshAddress();
    const context = await browser.newContext({ baseURL: BASE_URL, acceptDownloads: true });
    const page = await context.newPage();

    await test.step("sign in, and have both demo seeds run for this account", async () => {
      await signInThroughApi(context, address);
      expect(demo("seed-matches", address)).toContain("Seeded the invented demo");
      expect(demo("seed-tailor", address)).toContain("Seeded the invented tailoring demo");
    });

    await test.step("the chat panel opens from Today and answers the scripted question with a citation chip", async () => {
      await page.goto("/app/today");
      await expect(page.getByRole("heading", { level: 1, name: /^Today, / })).toBeVisible();
      await page.getByRole("button", { name: "Ask the agent" }).click();
      const dialog = page.getByRole("dialog", { name: "Ask the agent" });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("textbox", { name: "Your message" }).fill(MESSAGES.question);
      await dialog.getByRole("button", { name: "Send" }).click();

      await expect(dialog.getByRole("log")).toContainText("United States", { timeout: 60_000 });
      const chip = dialog.getByRole("link", { name: "Decision 1" });
      await expect(chip).toBeVisible();
      await expect(chip).toHaveAttribute("href", /^\/app\/matches\/[0-9a-f-]{36}$/);
      await expect(dialog.getByRole("log").locator("a")).toHaveCount(1); // the chip is the only link a reply holds
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("a second message proposes a record change, which changes nothing until Confirm", async () => {
      const dialog = page.getByRole("dialog", { name: "Ask the agent" });
      await expect(async () => {
        await dialog.getByRole("textbox", { name: "Your message" }).fill(MESSAGES.proposal);
        await expect(dialog.getByRole("button", { name: "Send" })).toBeEnabled();
      }).toPass({ timeout: 60_000 });
      await dialog.getByRole("button", { name: "Send" }).click();

      const card = dialog.getByRole("region", { name: "Suggested change to your record" });
      await expect(card).toBeVisible({ timeout: 60_000 });
      await expect(card).toContainText("Rust");
      await expect(card).toContainText("Source: what you said just now");
      await card.getByRole("button", { name: "Confirm" }).click();
      await expect(card.getByText("Confirmed")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
    });

    await test.step("the first match offers Tailor resume", async () => {
      await page.goto("/app/today");
      await page.getByRole("article").filter({ hasText: FIRST_MATCH }).getByRole("link", { name: "Open" }).click();
      await expect(page.getByRole("heading", { level: 1, name: FIRST_MATCH })).toBeVisible();
      await page.getByRole("link", { name: "Tailor resume" }).click();
      await expect(page).toHaveURL(/\/app\/matches\/[0-9a-f-]{36}\/tailor$/);
    });

    await test.step("tailoring shows 3 edits and says 2 were discarded before the person saw them", async () => {
      await page.getByRole("button", { name: "Tailor my resume" }).click();
      await expect(page.getByRole("heading", { level: 2, name: "3 edits proposed. 2 discarded before you saw them" })).toBeVisible({ timeout: 90_000 });
      await expect(page.getByRole("radiogroup", { name: "Your decision on this edit" })).toHaveCount(3);
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("every edit is approved, and the export counts them", async () => {
      for (let done = 1; done <= 3; done += 1) {
        await page.getByRole("radiogroup", { name: "Your decision on this edit" }).nth(done - 1).getByRole("radio", { name: "Approve" }).check();
        await expect(page.getByText("Approved", { exact: true })).toHaveCount(done);
      }
      await expect(page.getByText(/3 approved edits are used\. None are left out\./)).toBeVisible();
    });

    await test.step("the packet is prepared, then its PDF downloads and applying is recorded", async () => {
      await page.getByRole("button", { name: "Prepare packet" }).click();
      await expect(page.getByRole("heading", { level: 3, name: "Short note" })).toBeVisible({ timeout: 90_000 });
      await expect(page.getByText("You answer this").first()).toBeVisible();
      await expectNoViolationsInBothThemes(page);

      const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "Download packet PDF" }).click()]);
      expect(download.suggestedFilename()).toMatch(/\.pdf$/);
      expect(fs.statSync(await download.path()).size).toBeGreaterThan(0);

      await page.getByRole("button", { name: "Record that I applied" }).click();
      await expect(page.getByRole("link", { name: "your applications" })).toBeVisible();
    });

    await test.step("Applications lists it as Applied with a timestamp", async () => {
      await page.goto("/app/campaigns");
      await expect(page.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();
      const item = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: FIRST_MATCH }) });
      await expect(item).toContainText("Applied");
      await expect(item.locator("time")).toHaveCount(1);
      await expectNoViolationsInBothThemes(page);
    });

    await context.close();
  });
});
