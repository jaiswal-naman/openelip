import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  // A test file can opt into the node environment (proxy.test.ts), where there is no document to clean
  if (typeof window === "undefined") return;
  cleanup();
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
