import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { expectNoAxeViolations } from "./fixtures/axe";
import { BASE_URL, freshAddress, signInThroughApi } from "./helpers";

/*
 * Phase 6, the hiring desk, as the roadmap demo (plan 06-07): an employer posts a role and watches the anonymous count, a
 * candidate is asked privately and accepts with the exact preview in front of them, and the employer sees the said-yes
 * shortlist with the evidence and the answer clock, then answers yes and receives the email. Every page passes axe in both
 * themes.
 *
 * Two people on invented data. Two fresh addresses sign in; the demo seed (`python -m app.cli demo seed-hiring`, run inside
 * the worker container, which has the migrate, worker and match logins only) makes the first the owner of the invented
 * organization "Halden Demo", and gives the second and six invented candidates the same invented record; a seventh invented
 * candidate blocked "Halden Demo" and is never asked. The model is the fake provider replaying
 * backend/app/demo_data/hiring/recorded (LLM_FAKE_DIR), which hands each recording out once per process: run this spec on a
 * fresh demo stack.
 *
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml down -v
 *   docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait
 *   cd web && DEMO_STACK=1 BASE_URL=http://localhost:8090 MAILPIT_URL=http://localhost:8026 \
 *     pnpm exec playwright test tests/e2e/hiring-desk.spec.ts --project=chromium
 *
 * The standard stack of the other specs (database `app`) skips it; .github/workflows/e2e.yml runs it in the demo job on a
 * stack of its own, after the matches-brief spec.
 */

const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");
const DEMO_COMPOSE = ["compose", "-p", "openelip-demo", "-f", "docker-compose.yml", "-f", "docker-compose.demo.yml"];
const ROLE_TEXT = readFileSync(path.join(COMPOSE_DIR, "backend", "app", "demo_data", "hiring", "role.txt"), "utf8");
const SCHEMES = ["light", "dark"] as const;

const ORGANIZATION = "Halden Demo";
const TITLE = "Backend engineer";
const CANDIDATE_NAME = "Demo Candidate"; // the seed's name for the signed-in candidate, who has set none
const REQUIREMENTS = ["Python and SQL services in production.", "3+ years as a backend engineer.", "Experience leading a small team is a plus."];

function seedHiring(employer: string, candidate: string): string {
  return execFileSync(
    "docker",
    [...DEMO_COMPOSE, "exec", "-T", "worker", "python", "-m", "app.cli", "demo", "seed-hiring", "--employer-email", employer, "--candidate-email", candidate],
    { cwd: COMPOSE_DIR, encoding: "utf8" },
  );
}

/** The page as it is now has no axe violations in either theme. Wait until it shows what you mean to scan before calling this. */
async function expectNoViolationsInBothThemes(page: Page) {
  for (const scheme of SCHEMES) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoAxeViolations(page);
  }
}

test.describe("the hiring desk demo", () => {
  test.skip(process.env.DEMO_STACK !== "1", "needs the demo stack (fake model with LLM_FAKE_DIR, a _test database): run with DEMO_STACK=1 (see the top of this file)");

  test("post a role, watch the anonymous count, accept as the candidate, answer yes on the said-yes shortlist", async ({ browser }) => {
    test.setTimeout(300_000);
    const employerAddress = freshAddress(); // example.com, not example.test: the relay never delivers to example.test
    const candidateAddress = freshAddress();
    const employerContext = await browser.newContext({ baseURL: BASE_URL });
    const candidateContext = await browser.newContext({ baseURL: BASE_URL });
    const employer = await employerContext.newPage();
    const candidate = await candidateContext.newPage();
    let roleId = "";

    await test.step("both people sign in, and the demo is seeded for them", async () => {
      await signInThroughApi(employerContext, employerAddress);
      await signInThroughApi(candidateContext, candidateAddress);
      const report = seedHiring(employerAddress, candidateAddress);
      expect(report).toContain("Seeded the invented hiring demo.");
      expect(report).toContain(ORGANIZATION);
    });

    await test.step("the employer opens the roles of Halden Demo, marked as invented", async () => {
      await employer.goto("/hire/roles");
      await expect(employer.getByText(ORGANIZATION).first()).toBeVisible();
      await expect(employer.getByText("Demo (invented)").first()).toBeVisible();
      await expectNoViolationsInBothThemes(employer);
    });

    // Saving the draft reads the requirements (POST /v1/roles extracts); "Extract" on the role page reads again after an edit, so
    // it is only checked to be there: pressing it now would race the confirmations below.
    await test.step("New role: the demo description is saved and read into requirements", async () => {
      await employer.getByRole("link", { name: "New role" }).click();
      await employer.getByLabel("Role title").fill(TITLE);
      await employer.getByLabel("Describe the role").fill(ROLE_TEXT);
      await employer.getByRole("button", { name: "Save draft" }).click();
      await expect(employer).toHaveURL(/\/hire\/roles\/[0-9a-f-]{36}$/);
      roleId = /\/hire\/roles\/([0-9a-f-]{36})$/.exec(employer.url())?.[1] ?? "";
      await expect(employer.getByRole("heading", { level: 1, name: TITLE })).toBeVisible();
      await expect(employer.getByRole("button", { name: "Extract", exact: true })).toBeVisible();
      for (const requirement of REQUIREMENTS) {
        await expect(employer.getByRole("button", { name: `Confirm: ${requirement}` })).toBeVisible();
      }
      await expect(employer.getByRole("region", { name: "Anonymous preview (counts only, no identities)" })).toContainText("Counts appear after publishing.");
    });

    await test.step("confirm every requirement, set the pay range, and publish", async () => {
      for (const requirement of REQUIREMENTS) {
        const confirm = employer.getByRole("button", { name: `Confirm: ${requirement}` });
        await confirm.click();
        await expect(confirm).toHaveCount(0);
      }
      await employer.getByLabel("Currency").selectOption("EUR");
      await employer.getByLabel("Minimum a year").fill("70000");
      await employer.getByLabel("Maximum a year").fill("95000");
      await employer.getByRole("button", { name: "Save pay and location" }).click();
      await expect(employer.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
      await expect(employer.getByText("Publishing needs:")).toBeVisible();
      await expectNoViolationsInBothThemes(employer);
      const publish = employer.getByRole("button", { name: "Publish", exact: true });
      await expect(publish).toBeEnabled();
      await publish.click();
      await expect(employer.getByRole("link", { name: "Shortlist", exact: true })).toBeVisible();
    });

    await test.step("the anonymous preview fills in with counts only: seven eligible, seven asked, no name or address", async () => {
      const preview = employer.getByRole("region", { name: "Anonymous preview (counts only, no identities)" });
      // the worker runs the ask job: a role run on the recorded model, then the asks; no fixed wait, the page is read again
      await expect(async () => {
        await employer.reload();
        await expect(preview.getByText(/^As of /)).toBeVisible({ timeout: 2_000 });
      }).toPass({ timeout: 120_000 });
      const counts = preview.locator("dd");
      await expect(counts.nth(0)).toHaveText("7"); // eligible: the blocked candidate is filtered out
      await expect(counts.nth(1)).toHaveText("7"); // selected to ask
      await expect(preview).not.toContainText(CANDIDATE_NAME);
      await expect(preview).not.toContainText(candidateAddress);
      await expectNoViolationsInBothThemes(employer);
    });

    await test.step("the candidate sees the ask on Today, opens the consent sheet and reads exactly what would be shared", async () => {
      await candidate.goto("/app/today");
      const card = candidate.getByRole("article", { name: `${ORGANIZATION} asks about ${TITLE}` });
      await expect(card).toBeVisible();
      await expect(card.getByText("Demo (invented)")).toBeVisible();
      await expectNoViolationsInBothThemes(candidate);
      await card.getByRole("button", { name: "Review and accept" }).click();
      const sheet = candidate.getByRole("dialog");
      const title = sheet.getByRole("heading", { name: `Share with ${ORGANIZATION} for ${TITLE}?` });
      await expect(title).toBeFocused(); // focus starts on the title, not on Accept
      const table = sheet.getByRole("grid", { name: "Requirements and your evidence that would be shared" });
      await expect(table.getByRole("row")).toHaveCount(4); // the header and the three requirements
      await expect(table.getByRole("row", { name: /Python and SQL services in production/ })).toContainText("Met");
      await expect(table).toContainText("Skills: Python, SQL, Quillscript.");
      await expect(sheet.getByText(/Your email is shared only if they answer yes/)).toBeVisible();
      await expect(sheet.getByRole("checkbox", { name: /Send serious interest/ })).not.toBeChecked();
      await expectNoViolationsInBothThemes(candidate);
      await sheet.getByRole("button", { name: "Accept and share" }).click();
      await expect(candidate.getByRole("dialog")).toHaveCount(0);
      const accepted = candidate.getByRole("region", { name: "Introductions you accepted" });
      await expect(accepted).toContainText(`${ORGANIZATION}: ${TITLE}`);
      await expect(accepted).toContainText("Waiting for their answer");
    });

    await test.step("the employer's shortlist holds only the person who said yes, with the shared evidence and the clock", async () => {
      await employer.goto(`/hire/shortlist/${roleId}`);
      await expect(employer.getByRole("heading", { level: 1, name: `Shortlist, ${TITLE}` })).toBeVisible();
      const cards = employer.getByRole("article");
      await expect(cards).toHaveCount(1);
      const card = employer.getByRole("article", { name: `Candidate ${CANDIDATE_NAME}` });
      await expect(card.locator("[data-tone]")).toBeVisible(); // the answer clock
      await expect(card).not.toContainText(candidateAddress); // no email before a yes
      await card.getByRole("button", { name: "Shared details" }).click();
      await expect(card.getByRole("grid", { name: `Shared evidence of ${CANDIDATE_NAME}` })).toBeVisible();
      await expectNoViolationsInBothThemes(employer);
    });

    await test.step("the employer answers yes with a preset reason and then sees the candidate's email", async () => {
      const card = employer.getByRole("article", { name: `Candidate ${CANDIDATE_NAME}` });
      await card.getByRole("button", { name: `Yes, let's talk (${CANDIDATE_NAME})` }).click();
      const dialog = employer.getByRole("dialog");
      await expect(dialog.getByRole("heading", { name: `Say yes: ${CANDIDATE_NAME}` })).toBeVisible();
      await dialog.getByRole("group", { name: "Reasons to start from" }).getByRole("button", { name: "Strong fit" }).click();
      await expect(dialog.getByRole("textbox", { name: "Reason (required)" })).toHaveValue("Strong fit for the role.");
      await dialog.getByRole("button", { name: "Send yes" }).click();
      await expect(employer.getByText(`Email: ${candidateAddress}`)).toBeVisible();
    });

    await test.step("the answer clock page passes axe", async () => {
      await employer.goto("/hire/answers");
      await expect(employer.getByText(/You owe (no answers\.|\d+ answers?)/)).toBeVisible();
      await expectNoViolationsInBothThemes(employer);
    });

    await test.step("the candidate sees the employer's yes and its reason", async () => {
      await candidate.goto("/app/today?view=decisions"); // the answer email's link
      const accepted = candidate.getByRole("region", { name: "Introductions you accepted" });
      await expect(accepted).toContainText("They answered yes.");
      await expect(accepted).toContainText("Strong fit for the role.");
      await expectNoViolationsInBothThemes(candidate);
    });

    await employerContext.close();
    await candidateContext.close();
  });
});
