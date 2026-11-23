import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type BrowserContext, type Locator, type Page } from "@playwright/test";
import { listZipEntries, readZipEntry } from "./fixtures/zipEntry";
import { BASE_URL, freshAddress, MAILPIT_URL, signInThroughApi } from "./helpers";

/*
 * Phase 3, the Career Record, as a candidate sees it (SC1 to SC5).
 *
 * Three groups. The first signs nobody in: a visitor with no session who opens a candidate page is sent to sign in, and
 * the address says where to come back to. The second signs fresh accounts in and scans every candidate page with axe in
 * both themes, and checks that a person who has not agreed to AI processing is told why AI is off and can still keep a
 * record. Both need nothing but the stack Phase 1's browser tests use, so they run on a fresh one.
 *
 * The third is the demo: agree to AI processing, upload an invented resume, see each fact with the words it came from, see
 * the hidden text that was removed, decide facts, set constraints and preferences, edit a constraint and see the change log,
 * export everything and delete the account. It needs the fake model provider to know the answers for the invented resume,
 * which no fresh stack has, so it runs only with DEMO_STACK=1. It copies the recorded answers (fixtures/recorded) into the
 * running worker with `docker compose cp`; the fake provider reads them when a task is called, so no restart is needed.
 *
 *   APP_MODE=server LLM_PROVIDER=fake docker compose up -d --wait   (.env.example plus those two: it is local mode with no provider;
 *                                         Mailpit on, database `app` or a copy)
 *   cd web && DEMO_STACK=1 pnpm exec playwright test tests/e2e/career-record.spec.ts
 *
 * It creates accounts and deletes one, so point it at a throwaway stack, as the Playwright config says.
 */

const FIXTURES = path.resolve(__dirname, "fixtures");
const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");
// Where the worker image keeps the fake provider's recorded answers (backend/Dockerfile puts the backend in /srv/backend).
const RECORDED_IN_WORKER = "/srv/backend/app/platform/llm/recorded";

// WCAG 2.0, 2.1 and 2.2, levels A and AA (FRONTEND_UI 7.2), as a11y.spec.ts scans them.
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const SCHEMES = ["light", "dark"] as const;

type StorageState = Awaited<ReturnType<BrowserContext["storageState"]>>;

/** Agrees to AI processing through the API, as the sheet does: with the wording version the API says is current. False when this stack has no model provider, so there is nothing to agree to. */
async function agreeToAi(context: BrowserContext): Promise<boolean> {
  const me = await context.request.get(`${BASE_URL}/v1/me`);
  expect(me.ok()).toBe(true);
  const { data } = (await me.json()) as { data: { consents: { purpose: string; providers: string[] | null; current_text_version: string | null }[] } };
  const ai = data.consents.find((consent) => consent.purpose === "ai_processing");
  if (!ai?.providers?.length || !ai.current_text_version) return false;
  const agreed = await context.request.patch(`${BASE_URL}/v1/me`, {
    data: { consents: [{ purpose: "ai_processing", granted: true, text_version: ai.current_text_version }] },
    headers: { "Idempotency-Key": randomUUID() },
  });
  expect(agreed.ok()).toBe(true);
  return true;
}

/** The newest sign-in code sent to `address` that has not been used yet. Mailpit lists the newest message first. */
async function freshCode(request: APIRequestContext, address: string, used: Set<string>): Promise<string> {
  let found: string | undefined;
  await expect(async () => {
    const list = await request.get(`${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:"${address}"`)}`);
    const { messages } = (await list.json()) as { messages: { ID: string }[] };
    for (const { ID } of messages) {
      const message = await request.get(`${MAILPIT_URL}/api/v1/message/${ID}`);
      const { Text } = (await message.json()) as { Text: string };
      const code = /sign-in code is (\d{6})/.exec(Text)?.[1];
      if (code && !used.has(code)) {
        found = code;
        return;
      }
    }
    expect(found).toBeDefined();
  }).toPass({ timeout: 30_000 });
  used.add(found as string);
  return found as string;
}

async function expectNoViolations(page: Page) {
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(violations.map((v) => ({ rule: v.id, impact: v.impact, targets: v.nodes.map((n) => n.target) }))).toEqual([]);
}

/** Presses Tab from where focus is until `target` has it: that control can be reached from the keyboard. */
async function tabTo(page: Page, target: Locator) {
  for (let presses = 0; presses < 120; presses += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  throw new Error("Tab never reached the control");
}

/** How the focused control is outlined: a visible focus ring is a solid line, 2px or more (FRONTEND_UI 7.1). */
async function focusRing(target: Locator) {
  return target.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth) };
  });
}

// ---------------------------------------------------------------------------------------------------------------------
// A visitor with no session is sent to sign in from every page that is for a signed-in person

// Each page redirects on the server when the API says there is no session, and puts the page itself in `next`, so signing in
// brings the person back (the pages' own code: web/src/app/onboarding, app/record and app/settings).
const SIGNED_IN_PAGES = ["/onboarding", "/app/record", "/app/settings?section=privacy"];

test.describe("the candidate pages, signed out", () => {
  for (const path of SIGNED_IN_PAGES) {
    test(`${path} sends a visitor with no session to sign in and says where to come back to`, async ({ browser }) => {
      const context = await browser.newContext({ baseURL: BASE_URL }); // no cookie
      const page = await context.newPage();

      await page.goto(path);

      // exact: a string would be read as a glob, where ? matches any character
      await expect(page).toHaveURL((url) => url.pathname === "/sign-in" && url.searchParams.get("next") === path);
      await expect(page.getByRole("button", { name: "Send code" })).toBeVisible();
      await context.close();
    });
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// Every candidate page, both themes, for a person who has not agreed to AI processing and for one who has

type Case = { path: string; state: "unagreed" | "agreed"; ready: (page: Page) => Locator };

// `ready` says when a page has loaded its data, so axe scans the real screen and not a placeholder. Where this stack has
// no model provider the AI notice stands in for the AI controls, so those pages are ready either way.
const CASES: Case[] = [
  { path: "/onboarding?step=consent", state: "unagreed", ready: (page) => page.getByRole("button", { name: /^(Agree|Continue without AI)$/ }) },
  { path: "/onboarding?step=upload", state: "agreed", ready: (page) => page.getByRole("button", { name: /Choose a PDF or DOCX|Continue without a resume/ }) },
  {
    path: "/onboarding?step=facts",
    state: "agreed",
    ready: (page) => page.getByText(/You have not uploaded a resume yet|Turn on AI processing to use this|AI features are off on this installation/),
  },
  { path: "/onboarding?step=constraints", state: "agreed", ready: (page) => page.getByRole("heading", { level: 3, name: "Location" }) },
  { path: "/onboarding?step=preferences", state: "agreed", ready: (page) => page.getByRole("button", { name: "Add", exact: true }) },
  { path: "/onboarding?step=matches", state: "agreed", ready: (page) => page.getByText(/Your brief arrives by 07:00 in/) },
  { path: "/app/record", state: "agreed", ready: (page) => page.getByRole("heading", { level: 2, name: "Change log" }) },
  { path: "/app/settings?section=privacy", state: "agreed", ready: (page) => page.getByRole("button", { name: "Delete account" }) },
];

test.describe("the candidate pages", () => {
  const states = {} as Record<"unagreed" | "agreed", StorageState>;

  // One sign-in for each kind of account: the sign-in limit is per address, so a sign-in for every page would eat into it.
  test.beforeAll(async ({ browser }) => {
    const unagreed = await browser.newContext({ baseURL: BASE_URL });
    await signInThroughApi(unagreed, freshAddress());
    states.unagreed = await unagreed.storageState();
    await unagreed.close();

    const agreed = await browser.newContext({ baseURL: BASE_URL });
    await signInThroughApi(agreed, freshAddress());
    await agreeToAi(agreed);
    states.agreed = await agreed.storageState();
    await agreed.close();
  });

  for (const scheme of SCHEMES) {
    for (const route of CASES) {
      test(`${route.path} has no axe violations in the ${scheme} theme`, async ({ browser }) => {
        const context = await browser.newContext({ baseURL: BASE_URL, colorScheme: scheme, storageState: states[route.state] });
        const page = await context.newPage();
        await page.goto(route.path);
        await expect(route.ready(page).first()).toBeVisible();
        await expectNoViolations(page);
        await context.close();
      });
    }
  }

  test("someone who has not agreed to AI processing is told why AI is off, and can still keep a record", async ({ browser }) => {
    const context = await browser.newContext({ baseURL: BASE_URL, storageState: states.unagreed });
    const page = await context.newPage();

    await page.goto("/onboarding?step=upload");
    const notice = page.getByText(/Turn on AI processing to use this|AI features are off on this installation/);
    await expect(notice).toBeVisible();
    await expect(page.getByRole("button", { name: /Choose a PDF or DOCX/ })).toHaveCount(0);

    // The record works with no AI and no consent: a fact typed by hand is kept and logged.
    await page.goto("/app/record");
    await expect(page.getByRole("heading", { level: 1, name: "Your record" })).toBeVisible();
    await expect(page.getByText(/Turn on AI processing to use this|AI features are off on this installation/)).toBeVisible();
    await expect(page.getByRole("link", { name: "Upload new" })).toHaveCount(0);
    await page.getByLabel("Kind", { exact: true }).selectOption("skill");
    await page.getByRole("textbox", { name: "The fact, in your words" }).fill("PostgreSQL");
    await page.getByRole("button", { name: "Add fact" }).click();
    await expect(page.getByRole("button", { name: "Edit: PostgreSQL" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Change log, newest first" }).getByRole("row").filter({ hasText: "Added fact" })).toBeVisible();
    await context.close();
  });
});

// ---------------------------------------------------------------------------------------------------------------------
// The phase demo, from sign-in to deletion

test.describe("the phase demo", () => {
  test.skip(process.env.DEMO_STACK !== "1", "needs the fake model's recorded answers for the demo resume in the worker: run with DEMO_STACK=1 (see the top of this file)");

  test("agree, upload, check the facts, set the intake, edit and see the log, export, delete", async ({ page, request }) => {
    test.setTimeout(300_000);
    const email = freshAddress();
    const usedCodes = new Set<string>();
    const account = page.getByRole("button", { name: `Account: ${email}` });

    await test.step("the worker learns the answers for the invented resume", () => {
      execFileSync("docker", ["compose", "cp", `${path.join(FIXTURES, "recorded")}${path.sep}.`, `worker:${RECORDED_IN_WORKER}`], { cwd: COMPOSE_DIR, stdio: "pipe" });
    });

    await test.step("sign in with the code from the mailbox", async () => {
      await page.goto("/sign-in");
      await page.getByRole("textbox", { name: "Email" }).fill(email);
      await page.getByRole("button", { name: "Send code" }).click();
      await expect(page.getByText(`Code sent to ${email}`)).toBeVisible();
      await page.getByRole("textbox", { name: "Code" }).fill(await freshCode(request, email, usedCodes));
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(account).toBeVisible();
    });

    await test.step("agree to AI processing, after reading who receives what", async () => {
      await page.goto("/onboarding");
      await expect(page.getByRole("heading", { level: 1, name: "AI processing" })).toBeVisible();
      const providers = page.getByRole("list", { name: "AI model providers" });
      await expect(providers.getByRole("listitem")).not.toHaveCount(0); // a stack with no model has nothing to agree to
      await expect(providers).toContainText("Receives");
      await page.getByRole("button", { name: "Agree" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Upload your resume" })).toBeVisible();
    });

    await test.step("upload the invented resume and watch it being read", async () => {
      await page.locator('input[type="file"]').setInputFiles(path.join(FIXTURES, "invented-resume.pdf"));
      await expect(page.getByText(/Read invented-resume\.pdf: 10 facts found, 10 to review\./)).toBeVisible({ timeout: 120_000 });
      // The white sentence planted in the file was removed, and the candidate is told what it said and that it is not used.
      const hidden = page.getByRole("group", { name: "Hidden text removed from your file" });
      await expect(hidden).toContainText(/We removed [0-9]+ characters of hidden text from your file/);
      await expect(hidden).toContainText("It is not used for anything");
      await hidden.getByText("See what was removed").click();
      await expect(hidden).toContainText("Ignore all previous instructions");
      await page.getByRole("button", { name: "Review the facts" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Check what we read" })).toBeVisible();
    });

    const facts = page.getByRole("region", { name: "Facts read from your resume" });
    const rows = facts.getByRole("listitem");
    const roleRow = rows.filter({ hasText: "Senior Backend Engineer, Globex Example Ltd" });
    const leadRow = rows.filter({ hasText: "Led a team of four engineers through a migration." });

    await test.step("every fact shows the resume's own words and where they are", async () => {
      await expect(rows).toHaveCount(10);
      await expect(facts.locator("blockquote")).toHaveCount(10);
      for (const row of await rows.all()) await expect(row.locator("blockquote")).toHaveText(/\S/);
      await expect(roleRow).toContainText("From your resume · L7");
      await expect(page.getByText("10 to review. They are ignored until you confirm them.")).toBeVisible();
    });

    await test.step("the fact buttons are reachable from the keyboard, each with a visible focus ring", async () => {
      await page.getByRole("heading", { level: 1, name: "Check what we read" }).focus();
      for (const name of [/^Confirm: Senior Backend Engineer/, /^Fix: Senior Backend Engineer/, /^Reject: Senior Backend Engineer/]) {
        const button = roleRow.getByRole("button", { name });
        await tabTo(page, button);
        const ring = await focusRing(button);
        expect(ring.style).toBe("solid");
        expect(ring.width).toBeGreaterThanOrEqual(2);
      }
    });

    await test.step("the facts step has no axe violations in either theme", async () => {
      for (const scheme of SCHEMES) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoViolations(page);
      }
    });

    await test.step("confirm one fact, reject one, and confirm the rest", async () => {
      await roleRow.getByRole("button", { name: /^Confirm:/ }).click();
      await expect(page.getByText("9 to review. They are ignored until you confirm them.")).toBeVisible();
      await expect(roleRow.getByText("Confirmed", { exact: true })).toBeVisible();
      await leadRow.getByRole("button", { name: /^Reject:/ }).click();
      await expect(page.getByText("8 to review. They are ignored until you confirm them.")).toBeVisible();
      await expect(leadRow).toHaveCount(0); // a rejected fact is not shown as active
      await page.getByRole("button", { name: "Confirm all remaining" }).click();
      await expect(page.getByText("Nothing left to review.")).toBeVisible();
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Your hard constraints" })).toBeVisible();
    });

    await test.step("set a remote policy and a salary floor; skip the rest, which stay not enforced", async () => {
      const skip = page.getByRole("button", { name: "Skip", exact: true });
      await skip.click(); // location
      await page.getByRole("checkbox", { name: "Remote", exact: true }).check();
      await page.getByRole("checkbox", { name: "Hybrid", exact: true }).check();
      await expect(page.getByText("You will only see hybrid or remote jobs.")).toBeVisible();
      await page.getByRole("button", { name: "Confirm", exact: true }).click();
      await expect(page.getByLabel("Countries you are allowed to work in")).toBeVisible();
      await skip.click(); // work authorization
      await page.getByRole("textbox", { name: "Amount" }).fill("3000000");
      await page.getByRole("textbox", { name: "Currency" }).fill("INR");
      await expect(page.getByText("You will never see jobs that pay less than INR 30,00,000 a year.")).toBeVisible();
      await page.getByRole("button", { name: "Confirm", exact: true }).click();
      // Each Skip waits for the card it is about to skip, so a card that is still saving is never skipped by mistake.
      await expect(page.getByRole("textbox", { name: "Amount" })).toHaveCount(0); // the salary card has closed
      await expect(page.getByRole("group", { name: "Types of employment you accept" })).toBeVisible();
      await skip.click();
      await expect(page.getByRole("group", { name: "Levels you accept" })).toBeVisible();
      await skip.click();
      await expect(page.getByRole("textbox", { name: "Companies you never want to see" })).toBeVisible();
      await skip.click();
      await expect(page.getByText("Not set (not enforced)").first()).toBeVisible();
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Your preferences" })).toBeVisible();
    });

    await test.step("a dealbreaker that cannot be checked is saved as a preference, not a guarantee; a protected characteristic is not kept", async () => {
      const box = page.getByRole("textbox", { name: "A preference, or something you will not accept" });
      await page.getByRole("radio", { name: "This is a dealbreaker" }).check();
      await box.fill("No heavy travel");
      await page.getByRole("button", { name: "Add", exact: true }).click();
      const saved = page.getByRole("list", { name: "Your preferences" });
      await expect(saved).toContainText("No heavy travel");
      await expect(saved).toContainText("We cannot check this in job data, so it is a preference, not a guarantee.");

      await box.fill("Religion: Jainism");
      await page.getByRole("button", { name: "Add", exact: true }).click();
      // Next keeps its own empty role=alert on every page, so the alert is found by what it says
      await expect(page.getByRole("alert").filter({ hasText: "We do not keep this in your record." })).toBeVisible();
      await expect(box).toHaveValue("");
      await expect(saved).not.toContainText("Jainism");

      await page.getByRole("button", { name: "Finish" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Time zone and first matches" })).toBeVisible();
    });

    await test.step("the record shows it all on one page, and the account menu leads there", async () => {
      await account.click();
      await page.getByRole("menuitem", { name: "Your record" }).click();
      await expect(page).toHaveURL(/\/app\/record$/);
      await expect(page.getByRole("heading", { level: 1, name: "Your record" })).toBeVisible();
      await expect(page.getByText("You will never see jobs that pay less than INR 30,00,000 a year.").first()).toBeVisible();
      await expect(page.getByRole("list", { name: "Your preferences" })).toContainText("No heavy travel");
      await expect(page.getByRole("list", { name: "Uploaded documents" })).toContainText("invented-resume.pdf");
      await expect(page.getByRole("button", { name: /^Edit: Senior Backend Engineer, Globex Example Ltd/ })).toBeVisible();
      for (const scheme of SCHEMES) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoViolations(page);
      }
    });

    await test.step("edit the salary floor and see a new row in the change log, with before and after, without a reload", async () => {
      await page.getByRole("button", { name: "Edit salary floor" }).click();
      await expect(page.getByText(/From tomorrow.s run, jobs that do not meet this are filtered out/)).toBeVisible();
      await page.getByRole("textbox", { name: "Amount" }).fill("3500000");
      await page.getByRole("button", { name: "Confirm", exact: true }).click();
      await expect(page.getByText("You will never see jobs that pay less than INR 35,00,000 a year.").first()).toBeVisible();
      const row = page.getByRole("table", { name: "Change log, newest first" }).getByRole("row").filter({ hasText: "Changed salary floor" });
      await expect(row).toContainText("INR 30,00,000");
      await expect(row).toContainText("INR 35,00,000");
      await expect(row).toContainText("Edited by you");
      await expect(row.locator("time")).toHaveAttribute("datetime", /^\d{4}-\d{2}-\d{2}T/);
    });

    await test.step("the privacy settings: the AI agreement, and an export that downloads as a ZIP with a manifest", async () => {
      await account.click();
      await page.getByRole("menuitem", { name: "Settings" }).click();
      await expect(page).toHaveURL(/\/app\/settings\?section=privacy$/);
      const ai = page.getByRole("region", { name: "AI processing", exact: true });
      await expect(ai).toContainText("You agreed on");
      await expect(ai.getByRole("button", { name: "Withdraw" })).toBeVisible();
      for (const scheme of SCHEMES) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoViolations(page);
      }

      await page.getByRole("button", { name: "Export all data" }).click();
      const link = page.getByRole("link", { name: "Download your data (ZIP)" });
      await expect(link).toBeVisible({ timeout: 90_000 });
      await expect(page.getByText(/Available until/)).toBeVisible();
      const [download] = await Promise.all([page.waitForEvent("download"), link.click()]);
      const archive = readFileSync(await download.path());
      const manifest = JSON.parse((readZipEntry(archive, "manifest.json") ?? Buffer.from("null")).toString("utf8")) as { format_version: number; files: { path: string }[] } | null;
      expect(manifest).not.toBeNull();
      expect(manifest?.format_version).toBe(1);
      expect(Array.isArray(manifest?.files)).toBe(true);
      // the original upload travels with the data
      expect(listZipEntries(archive).some((name) => /^files\/documents\/.*invented-resume\.pdf$/.test(name))).toBe(true);
    });

    await test.step("delete the account with a fresh emailed code, and land signed out", async () => {
      await page.getByRole("button", { name: "Delete account" }).click();
      const dialog = page.getByRole("dialog", { name: "Delete your account?" });
      await expect(dialog).toContainText("Your hard constraints");
      await expect(dialog).toContainText("cannot be reached by deletion");
      await expect(dialog.getByRole("button", { name: "Delete my account" })).toHaveCount(0); // not before a code is sent
      await dialog.getByRole("button", { name: "Send code" }).click();
      await dialog.getByRole("textbox", { name: "Code from the email" }).fill(await freshCode(request, email, usedCodes));
      await dialog.getByRole("button", { name: "Delete my account" }).click();

      await expect(page).toHaveURL(/\/app\/settings\/deleted$/, { timeout: 30_000 });
      await expect(page.getByRole("heading", { level: 1, name: "Your account is deleted" })).toBeVisible();
      await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
      await page.goto("/app/record");
      await expect(page).toHaveURL(/\/sign-in\?next=%2Fapp%2Frecord/);
    });
  });
});
