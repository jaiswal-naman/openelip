import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

// The same rules as a11y.spec.ts: WCAG 2.0, 2.1 and 2.2, levels A and AA (FRONTEND_UI 7.2). Axe finds only part of what
// WCAG asks for; the keyboard steps in the specs that use this cover some of the rest.
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/** Scans the page as it is now: wait until it shows what you mean to scan before calling this. */
export async function expectNoAxeViolations(page: Page): Promise<void> {
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(violations.map((v) => ({ rule: v.id, impact: v.impact, targets: v.nodes.map((n) => n.target) }))).toEqual([]);
}
