import type { Api } from "@/lib/api/client";
import { unwrap } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import type { Me } from "@/lib/api/types";

export type BriefFrequency = components["schemas"]["BriefSettings"]["frequency"];

/**
 * The wording the person saw beside the "Email me the brief" switch when they agreed. The API stores it with the consent
 * (the AI consent works the same way, with a version the server sends); change this string when that sentence changes.
 */
export const BRIEF_EMAIL_TEXT_VERSION = "brief-email-2026-11-a";

/** Monday: the day a weekly brief falls on when someone chooses weekly in one click and has not picked a day (assumption). */
export const DEFAULT_WEEKDAY = 1;

const grantEmail = { purpose: "brief_email", granted: true, text_version: BRIEF_EMAIL_TEXT_VERSION } as const;
const withdrawEmail = { purpose: "brief_email", granted: false } as const;

export type BriefChoices = { timezone: string; frequency: BriefFrequency; weekday: number; emailOn: boolean };

/**
 * Save how often the brief comes, its time zone and, when the switch changed, whether it is also emailed (BRF-03): one
 * request, so it is all or nothing. A weekly brief names its day; the others name none (the API refuses a stray one).
 */
export function saveBriefSettings(api: Api, choices: BriefChoices, emailChanged: boolean): Promise<Me> {
  return unwrap(
    api.PATCH("/v1/me", {
      body: {
        timezone: choices.timezone,
        brief: { frequency: choices.frequency, weekday: choices.frequency === "weekly" ? choices.weekday : null },
        ...(emailChanged ? { consents: [choices.emailOn ? grantEmail : withdrawEmail] } : {}),
      },
    }),
  );
}

/** Turn the brief email back on after an unsubscribe. */
export const turnBriefEmailOn = (api: Api): Promise<Me> => unwrap(api.PATCH("/v1/me", { body: { consents: [grantEmail] } }));

/** "Choose weekly instead": the email back on, once a week. */
export const chooseWeeklyBrief = (api: Api): Promise<Me> =>
  unwrap(api.PATCH("/v1/me", { body: { brief: { frequency: "weekly", weekday: DEFAULT_WEEKDAY }, consents: [grantEmail] } }));

/** Save the time zone the brief is read in. */
export const saveTimeZone = (api: Api, timezone: string): Promise<Me> => unwrap(api.PATCH("/v1/me", { body: { timezone } }));

/**
 * The one-click unsubscribe of RFC 8058, as the email's own header does it: the token in the address, the form value
 * `List-Unsubscribe=One-Click` in the body, no session and no cookie (`credentials: "omit"`), so the token alone names the
 * person (threat T-04-33). The server answers again with the same result when it is asked twice.
 */
export async function unsubscribeWithToken(api: Api, token: string): Promise<void> {
  await unwrap(
    api.POST("/v1/briefs/unsubscribe", {
      params: { query: { t: token } },
      body: { "List-Unsubscribe": "One-Click" },
      bodySerializer: (body) => new URLSearchParams(body),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      credentials: "omit",
    }),
  );
}
