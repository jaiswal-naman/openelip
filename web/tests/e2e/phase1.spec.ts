import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { freshAddress, readCode } from "./helpers";

// `docker compose exec` runs in this folder. COMPOSE_PROJECT_NAME in the environment picks the stack, as it does anywhere.
const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../..");

/** The Phase 1 demo as one journey: sign in with an emailed code, use the app, see the cost of a model call, sign out. */
test("sign in with an emailed code, search, see a model cost, sign out", async ({ page, request }) => {
  const email = freshAddress();
  const account = page.getByRole("button", { name: `Account: ${email}` });

  // Sign in. The code travels outbox, worker, SMTP, Mailpit: the relay path is exercised, not bypassed.
  await page.goto("/sign-in");
  await page.getByRole("textbox", { name: "Email" }).fill(email);
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByText(`Code sent to ${email}`)).toBeVisible();
  await page.getByRole("textbox", { name: "Code" }).fill(await readCode(request, email));
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(account).toBeVisible();

  // The session is a cookie, so a reload keeps it.
  await page.reload();
  await expect(account).toBeVisible();

  // Theme: no explicit choice yet, then dark, and the choice survives a reload.
  const html = page.locator("html");
  await expect(html).not.toHaveAttribute("data-theme", /.+/);
  await page.getByRole("button", { name: "Theme: System" }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  await expect(html).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(html).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "Theme: Dark" })).toBeVisible();

  // Search the job index with a word and a country. The index may be empty on a fresh stack, so what is checked is that
  // the search ran and the address holds it, not how many jobs came back.
  await page.goto("/jobs");
  await page.getByRole("searchbox", { name: "Search open jobs" }).fill("engineer");
  await page.getByLabel("Country").selectOption("IN");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/q=engineer/);
  await expect(page).toHaveURL(/country=IN/);
  const results = page.getByRole("region", { name: "Results" });
  await expect(results).toContainText(/Showing \d+ jobs?/);
  await expect(results).not.toContainText("Could not load jobs.");

  // One costed call through the model gateway, made as this person. The worker holds the database login that may write
  // the ledger. With the fake provider the row costs nothing; the live priced call is a manual check (README).
  const pinged = execFileSync(
    "docker",
    ["compose", "exec", "-T", "worker", "python", "-m", "app.platform.llm", "ping", "--email", email],
    { cwd: COMPOSE_DIR, encoding: "utf8" },
  );
  expect(pinged).toContain("ping ok");
  await page.goto("/control/costs");
  const byTask = page.getByRole("table", { name: "Model cost by task, from the cost ledger" });
  await expect(byTask.getByRole("rowheader", { name: "ping" })).toBeVisible();

  // What this server sends data to, from its configuration.
  await page.goto("/trust");
  const providers = page.getByRole("table", { name: "Providers and sub-processors this server sends data to" });
  await expect(providers.getByRole("rowheader", { name: "Recorded responses" })).toBeVisible();

  // Sign out, and a page that needs an account sends the person back to sign in.
  await account.click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  await page.goto("/control/costs");
  await expect(page).toHaveURL(/\/sign-in\?next=%2Fcontrol%2Fcosts/);
});
