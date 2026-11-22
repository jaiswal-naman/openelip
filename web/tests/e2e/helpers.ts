import { randomBytes } from "node:crypto";
import { expect, type APIRequestContext, type BrowserContext } from "@playwright/test";

export const BASE_URL = process.env.BASE_URL ?? "http://localhost:8080";
// Mailpit is the stack's mail catcher: the relay delivers every sign-in email to it, so a test reads its code from there.
export const MAILPIT_URL = process.env.MAILPIT_URL ?? "http://localhost:8025";

/** A new address for every run. Not under example.test: the relay never delivers there, so no code would arrive. */
export function freshAddress(): string {
  return `e2e+${randomBytes(5).toString("hex")}@example.com`;
}

/** The six-digit code from the newest email sent to `address`, read through Mailpit's HTTP API. */
export async function readCode(request: APIRequestContext, address: string): Promise<string> {
  const query = encodeURIComponent(`to:"${address}"`);
  let code: string | undefined;
  // The worker relays outgoing mail about once a second, so the email may take a moment to arrive.
  await expect(async () => {
    const found = await request.get(`${MAILPIT_URL}/api/v1/search?query=${query}`);
    expect(found.ok()).toBe(true);
    const { messages } = (await found.json()) as { messages: { ID: string }[] };
    expect(messages.length).toBeGreaterThan(0);
    const message = await request.get(`${MAILPIT_URL}/api/v1/message/${messages[0].ID}`);
    const { Text } = (await message.json()) as { Text: string };
    code = /sign-in code is (\d{6})/.exec(Text)?.[1];
    expect(code).toBeDefined();
  }).toPass({ timeout: 30_000 });
  return code as string;
}

/** Signs a browser context in through the API, so a test that is not about the sign-in form does not click through it. */
export async function signInThroughApi(context: BrowserContext, address: string): Promise<void> {
  const asked = await context.request.post(`${BASE_URL}/v1/auth/codes`, { data: { email: address } });
  expect(asked.status()).toBe(202);
  const code = await readCode(context.request, address);
  const session = await context.request.post(`${BASE_URL}/v1/auth/sessions`, { data: { email: address, code } });
  expect(session.ok()).toBe(true);
}
