import { useSyncExternalStore } from "react";
import { parseTheme, type ThemeChoice, THEME_KEY } from "./themeScript";

function showTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.dataset.theme = choice;
}

const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

export function setTheme(choice: ThemeChoice): void {
  showTheme(choice);
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, choice);
  } catch {
    // Storage is blocked: the choice still holds for this page, it just will not survive a reload.
  }
  notify();
}

// The attribute on <html> is the one source of truth; it is set by the head script, by setTheme, and by other tabs.
function readTheme(): ThemeChoice {
  return parseTheme(document.documentElement.dataset.theme);
}

function subscribe(listener: () => void): () => void {
  const onOtherTab = (event: StorageEvent) => {
    if (event.key !== THEME_KEY && event.key !== null) return; // null: the storage was cleared
    showTheme(parseTheme(event.newValue));
    notify();
  };
  listeners.add(listener);
  window.addEventListener("storage", onOtherTab);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onOtherTab);
  };
}

/** The theme choice in a client component. Only the system's value is known on the server, so it renders "system" first. */
export function useTheme(): { choice: ThemeChoice; setChoice: (choice: ThemeChoice) => void } {
  const choice = useSyncExternalStore(subscribe, readTheme, () => "system" as const);
  return { choice, setChoice: setTheme };
}
