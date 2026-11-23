import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { BASE_URL, freshAddress, signInThroughApi } from "./helpers";

/*
 * Phase 7, campaigns under the Guardian, as a candidate sees them (the roadmap demo; SC1 to SC5 in the browser).
 *
 * One person, on invented data. A fresh address signs in, `python -m app.cli demo seed-campaign` (run inside the worker
 * container) gives that account the matches and tailoring demos and a ready tailored resume, the candidate starts a campaign
 * from the first match, the crew researches the invented company site (served from disk) and prepares a packet, the candidate
 * reads the cited dossier, approves the packet, records Applied, types the follow-up address, and `demo age-campaign` moves
 * the clock eight days on; the drafted follow-up is approved and undone inside its window; the dial is set to Never in
 * settings, the clock moves again, and the next follow-up is blocked with its reason, which the Timeline shows and the flight
 * recorder steps to. The board, the campaign page (Dossier, Packet, Follow-ups), the Autonomy section and the recorder pass axe.
 *
 * It needs a FRESH demo stack (the fake provider hands its answers out once per process, so each demo spec needs a stack of
 * its own), a database whose name ends in _test (the seed refuses anything else) and RESEARCH_PAGES_DIR on the worker, all of
 * which docker-compose.demo.yml sets up:
 *
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml down -v
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait
 *   cd web && DEMO_STACK=1 BASE_URL=http://localhost:8090 MAILPIT_URL=http://localhost:8026 \
 *     pnpm exec playwright test tests/e2e/campaigns.spec.ts --project=chromium
 *
 * The standard stack of the other specs (database `app`) skips it. It creates an account, so point it at a throwaway stack.
 */

const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");
const DEMO_COMPOSE = ["compose", "-p", "openelip-demo", "-f", "docker-compose.yml", "-f", "docker-compose.demo.yml"];
const SCHEMES = ["light", "dark"] as const;

const FIRST_MATCH = "Demo posting (invented): Quillscript Platform Engineer";
const TYPED_ADDRESS = "hiring@apply.initech-demo.invalid";
const PRODUCT_QUOTE = "Initech Demo builds the Quillscript billing platform for small utilities.";
const NEVER_REASON = "You set this kind of action to never, so it was not attempted.";

/** Runs one demo command in the worker container, the way the manual demo does, and returns what it printed. */
function demo(...args: string[]): string {
  return execFileSync("docker", [...DEMO_COMPOSE, "exec", "-T", "worker", "python", "-m", "app.cli", "demo", ...args], { cwd: COMPOSE_DIR, encoding: "utf8" });
}

/** The page as it is now has no axe violations in either theme. Wait until it shows what you mean to scan before calling this. */
async function expectNoViolationsInBothThemes(page: Page) {
  for (const scheme of SCHEMES) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoAxeViolations(page);
  }
}

/** The crew works in the background and the page asks again every 15 seconds: reload until `ready` holds, with no fixed sleep. */
async function reloadUntil(page: Page, ready: () => Promise<void>, timeout = 120_000) {
  await expect(async () => {
    await page.reload();
    await ready();
  }).toPass({ timeout, intervals: [1_000, 2_000, 3_000] });
}

test.describe("the campaign demo", () => {
  test.skip(process.env.DEMO_STACK !== "1", "needs the demo stack (invented company pages, fake model, a _test database): run with DEMO_STACK=1 (see the top of this file)");

  test("start a campaign, read the cited dossier, approve the packet, approve and undo a follow-up, see a blocked one, and step through the recorder", async ({ browser }) => {
    test.setTimeout(600_000);
    const address = freshAddress(); // example.com, not example.test: the relay never delivers to example.test
    const context = await browser.newContext({ baseURL: BASE_URL });
    const page = await context.newPage();
    let campaignPath = "";

    await test.step("sign in, and have the campaign demo seeded for this account", async () => {
      await signInThroughApi(context, address);
      expect(demo("seed-campaign", "--email", address)).toContain("The invented campaign demo was seeded");
    });

    await test.step("start a campaign from the first match", async () => {
      await page.goto("/app/today?view=matches");
      await page.getByRole("link", { name: FIRST_MATCH }).first().click();
      await expect(page.getByRole("heading", { level: 1, name: FIRST_MATCH })).toBeVisible();
      await page.getByRole("button", { name: "Start campaign" }).click();
      await page.waitForURL(/\/app\/campaigns\/[0-9a-f-]{36}/);
      campaignPath = new URL(page.url()).pathname;
      await expect(page.getByRole("heading", { level: 1, name: FIRST_MATCH })).toBeVisible();
    });

    await test.step("Researching becomes Needs you", async () => {
      await reloadUntil(page, async () => {
        await expect(page.getByText("Needs you", { exact: true }).first()).toBeVisible({ timeout: 2_000 });
      });
    });

    await test.step("the Dossier tab shows a claim with a citation chip, its exact quote and the time it was fetched", async () => {
      await page.getByRole("tab", { name: "Dossier" }).click();
      const chip = page.getByRole("link", { name: /Source 1: open the source page/ });
      await expect(chip).toBeVisible();
      await expect(chip).toHaveAttribute("href", /^https:\/\/apply\.initech-demo\.invalid\//);
      await expect(page.getByText(PRODUCT_QUOTE).first()).toBeVisible();
      await expect(page.getByText(/fetched/).first()).toBeVisible();
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("the Packet tab: approve the packet, see the submit kit, and record Applied", async () => {
      await page.getByRole("tab", { name: "Packet" }).click();
      await expect(page.getByText("You answer this").first()).toBeVisible();
      await expectNoViolationsInBothThemes(page);
      await page.getByRole("button", { name: "Approve packet" }).click();
      await expect(page.getByText("The system never submits. Record Applied once you have.")).toBeVisible({ timeout: 60_000 });
      await expect(page.getByRole("link", { name: "Download the resume packet" })).toBeVisible();
      await page.getByRole("button", { name: "Record that I applied" }).click();
      await expect(page.getByText("Recorded as applied.")).toBeVisible();
      await reloadUntil(page, async () => {
        await expect(page.getByText("Applied", { exact: true }).first()).toBeVisible({ timeout: 2_000 });
      });
    });

    await test.step("the board shows the campaign in Applied, and passes axe", async () => {
      await page.goto("/app/campaigns");
      const applied = page.getByRole("region", { name: "Applied" });
      await expect(applied.getByRole("link", { name: FIRST_MATCH })).toBeVisible();
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("type the follow-up address and let eight quiet days pass", async () => {
      await page.goto(`${campaignPath}?tab=follow-ups`);
      await page.getByLabel("Follow-up address").fill(TYPED_ADDRESS);
      await page.getByRole("button", { name: "Save" }).click();
      await expect(page.getByText("Saved", { exact: true })).toBeVisible();
      expect(demo("age-campaign", "--email", address, "--days", "8")).toContain("Moved the clock 8 days on");
    });

    await test.step("the drafted follow-up waits for approval, to the address that was typed", async () => {
      await reloadUntil(page, async () => {
        await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeVisible({ timeout: 2_000 });
      });
      await expect(page.getByText(`${TYPED_ADDRESS} (you typed)`)).toBeVisible();
      await expectNoViolationsInBothThemes(page);
    });

    await test.step("approve it: it is scheduled with a countdown, and Undo shows Undone only after the server says so", async () => {
      await page.getByRole("button", { name: "Approve", exact: true }).click();
      await expect(page.getByText(/Scheduled, sends at/)).toBeVisible({ timeout: 60_000 });
      await expect(page.getByText(/Undo within \d+:\d\d/)).toBeVisible();
      await expect(page.getByText("Closing this tab does not stop the send.")).toBeVisible();
      await page.getByRole("button", { name: "Undo" }).first().click();
      await expect(page.getByText("Undone", { exact: true }).first()).toBeVisible();
    });

    await test.step("Settings, Autonomy: set follow-ups to Never; the locked row shows its reason", async () => {
      await page.goto("/app/settings?section=autonomy");
      await expect(page.getByText(/The system never submits an application for you/)).toBeVisible();
      await page.getByRole("radiogroup", { name: "Send a follow-up" }).getByText("Never", { exact: true }).click();
      await expect(page.getByText("Saved. Applies to new actions from now.")).toBeVisible();
      await expectNoViolationsInBothThemes(page);
      expect(demo("age-campaign", "--email", address, "--days", "8")).toContain("Moved the clock 8 days on");
    });

    await test.step("the Timeline shows the blocked follow-up with its reason, and the recorder steps to it", async () => {
      await page.goto(`${campaignPath}?tab=timeline`);
      const blocked = page.getByRole("listitem").filter({ hasText: "Blocked" }).first();
      await reloadUntil(page, async () => {
        await expect(blocked).toBeVisible({ timeout: 2_000 });
      });
      await expect(blocked).toContainText(NEVER_REASON);
      await blocked.getByRole("link", { name: "Open this step in the recorder" }).click();
      await page.waitForURL(/\/control\/recorder\?campaign_id=.*step=\d+/);
      await expect(page.getByRole("heading", { level: 1, name: "Flight recorder" })).toBeVisible();
      const step = page.getByRole("article", { name: "Selected step" });
      await expect(step).toContainText("Not allowed");
      await expect(step).toContainText(NEVER_REASON);
      await expect(page.getByRole("navigation", { name: "Steps" }).getByText("Blocked", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Previous" }).click();
      await expect(page.getByText(/^Step \d+ of \d+$/)).toBeVisible();
      await page.getByRole("button", { name: "Last" }).click();
      await expectNoViolationsInBothThemes(page);
    });
  });
});
