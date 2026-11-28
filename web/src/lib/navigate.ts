/**
 * A full page load that replaces this history entry. For the moment the session changes: every page and query the browser
 * holds was made for the old one, so nothing of the client router is kept.
 */
export function reloadTo(url: string): void {
  window.location.replace(url);
}
