import AxeBuilder from "@axe-core/playwright";
import { expect, test, type BrowserContext, type Locator, type Page } from "@playwright/test";
import { BASE_URL, freshAddress, signInThroughApi } from "./helpers";

// WCAG 2.0, 2.1 and 2.2, levels A and AA (FRONTEND_UI 7.2). Axe finds only part of what WCAG asks for; the keyboard and
// screen-reader passes in FRONTEND_UI 7 cover the rest.
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const COLOR_SCHEMES = ["light", "dark"] as const;

// `ready` says when a page has finished loading its data, so axe scans the real screen and not a skeleton.
const ROUTES: { path: string; signedIn: boolean; ready: (page: Page) => Locator }[] = [
  { path: "/", signedIn: false, ready: (page) => page.getByRole("main") },
  { path: "/jobs", signedIn: false, ready: (page) => page.getByRole("region", { name: "Results" }) },
  { path: "/sign-in", signedIn: false, ready: (page) => page.getByRole("button", { name: "Send code" }) },
  { path: "/trust", signedIn: false, ready: (page) => page.getByRole("table") },
  { path: "/control/costs", signedIn: true, ready: (page) => page.getByRole("heading", { name: "My model cost" }) },
];

let signedInState: Awaited<ReturnType<BrowserContext["storageState"]>>;

// One sign-in for the whole file: the sign-in limit is per address, so ten sign-ins in a run would eat into it.
test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ baseURL: BASE_URL });
  await signInThroughApi(context, freshAddress());
  signedInState = await context.storageState();
  await context.close();
});

for (const scheme of COLOR_SCHEMES) {
  for (const route of ROUTES) {
    test(`${route.path} has no axe violations in the ${scheme} theme`, async ({ browser }) => {
      const context = await browser.newContext({
        baseURL: BASE_URL,
        colorScheme: scheme,
        storageState: route.signedIn ? signedInState : undefined,
      });
      const page = await context.newPage();
      await page.goto(route.path);
      await expect(route.ready(page).first()).toBeVisible();
      if (route.path === "/jobs") {
        await expect(page.getByRole("region", { name: "Results" })).toHaveAttribute("aria-busy", "false");
      }

      const { violations } = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
      expect(violations.map((v) => ({ rule: v.id, impact: v.impact, targets: v.nodes.map((n) => n.target) }))).toEqual([]);
      await context.close();
    });
  }
}
