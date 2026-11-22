import type { components } from "./schema";

/** Names for the generated response shapes the screens use. */
export type Me = components["schemas"]["Me"];
export type Costs = components["schemas"]["Costs"];
export type CostRow = components["schemas"]["CostRow"];
export type Subprocessors = components["schemas"]["Subprocessors"];
export type Subprocessor = components["schemas"]["Subprocessor"];

/** The name of the session cookie the API sets (API.md 1.2). The browser never reads it; the proxy only checks it exists. */
export const SESSION_COOKIE = "app_session";
