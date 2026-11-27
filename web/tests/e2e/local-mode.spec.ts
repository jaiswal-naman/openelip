import { expect, test } from "@playwright/test";

// Local mode (APP_MODE=local, the .env.example default): the stack the README starts, with no model provider and no key.
// It needs a stack of its own, started from .env.example as it is, because every other spec signs in with an emailed code in
// server mode. Run it with LOCAL_STACK=1 (the `local` job of e2e.yml); everywhere else it skips itself.
test.skip(process.env.LOCAL_STACK !== "1", "needs a stack started from .env.example in local mode: run with LOCAL_STACK=1");

// Invented. It is stored (sealed) and never sent anywhere: no model call is made in this test.
const KEY = "sk-ant-api03-an-invented-key-for-the-browser-test";

test("opening the app signs the owner in with no sign-in page, asks for the model key once, and opens the hiring desk too", async ({
  page,
  browser,
}) => {
  const pathsVisited: string[] = [];
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) pathsVisited.push(new URL(frame.url()).pathname);
  });

  // A first visit with no session: the browser is sent to open the owner's session and lands on the setup page.
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup\?next=%2F$/);
  await expect(page.getByRole("heading", { name: "Set up your model" })).toBeVisible();
  expect(pathsVisited).not.toContain("/sign-in");
  // Signed in as the one owner, who is a candidate and an employer at once: the hiring desk is in the register from the start
  const roles = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Roles" });
  await expect(roles).toBeVisible();
  await expect(page.getByLabel("API key")).toHaveAttribute("type", "password");

  // The stack has no key yet, and says so (never the key itself).
  const before = await page.request.get("/v1/local/status");
  expect(before.ok()).toBe(true);
  expect((await before.json()).data).toMatchObject({ mode: "local", has_owner: true, model: { key_set: false } });

  // Paste a key: it is saved, the page goes on to where the visit was going (/), which sends a record with no resume read yet
  // to onboarding (key first, then the resume, then Today), and only the last four characters come back.
  await page.getByLabel("API key").fill(KEY);
  await page.getByRole("button", { name: "Save key" }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const after = await page.request.get("/v1/local/status");
  const saved = await after.text();
  expect(JSON.parse(saved).data.model).toEqual({ provider: "anthropic", key_set: true, key_last4: "test", source: "app" });
  expect(saved).not.toContain(KEY);

  // The owner opens the hiring desk from the register and creates their organization there.
  await roles.click();
  await expect(page).toHaveURL(/\/hire\/roles$/);
  await page.waitForLoadState("networkidle"); // the page is hydrated: a name typed before that is wiped when React takes over
  await page.getByLabel("Organization name").fill("Halden Studio");
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page).toHaveURL(/\/hire\/team$/);

  // Another browser with no session never sees a sign-in page either, and is not asked for the key again.
  const other = await browser.newContext();
  const visit = await other.newPage();
  const otherPaths: string[] = [];
  visit.on("framenavigated", (frame) => {
    if (frame === visit.mainFrame()) otherPaths.push(new URL(frame.url()).pathname);
  });
  await visit.goto("/sign-in");
  await expect(visit).toHaveURL(/\/jobs$/);
  expect(otherPaths).not.toContain("/setup");
  await expect(visit.getByRole("heading", { name: "Sign in" })).toHaveCount(0);
  await other.close();
});
