/**
 * Light, dark, or whatever the system says. The system choice stores nothing and sets no attribute, so the CSS
 * `prefers-color-scheme` rules decide with no JavaScript (FRONTEND_UI 5.2). An explicit choice sets `data-theme` on <html>.
 *
 * This file is plain data so the server layout can import it; the React hook is in theme.ts.
 */
export type ThemeChoice = "system" | "light" | "dark";

export const THEME_KEY = "theme";

/** Runs in <head> before first paint, so a stored choice never flashes the other theme. Kept as one constant for a CSP hash. */
export const THEME_SCRIPT = `try{var t=localStorage.getItem("${THEME_KEY}");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export function parseTheme(value: unknown): ThemeChoice {
  return value === "light" || value === "dark" ? value : "system";
}
