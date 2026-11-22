import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useTheme } from "./theme";
import { parseTheme, THEME_KEY, THEME_SCRIPT } from "./themeScript";

const html = () => document.documentElement;
const runHeadScript = () => new Function(THEME_SCRIPT)();

afterEach(() => vi.restoreAllMocks());

describe("useTheme", () => {
  it("follows the system by default: no attribute, nothing stored", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.choice).toBe("system");
    expect(html().hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });

  it("sets data-theme and persists the choice when toggled", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setChoice("dark"));
    expect(result.current.choice).toBe("dark");
    expect(html().dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("goes back to the system by removing the attribute and the stored value", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setChoice("light"));
    act(() => result.current.setChoice("system"));
    expect(html().hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });

  it("still applies the choice for this page when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setChoice("dark"));
    expect(html().dataset.theme).toBe("dark");
  });

  it("follows a change made in another tab", () => {
    const { result } = renderHook(() => useTheme());
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: THEME_KEY, newValue: "dark" }));
    });
    expect(result.current.choice).toBe("dark");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: THEME_KEY, newValue: null }));
    });
    expect(result.current.choice).toBe("system");
  });
});

describe("the head script that prevents a flash", () => {
  it("applies a stored choice before React runs", () => {
    window.localStorage.setItem(THEME_KEY, "dark");
    runHeadScript();
    expect(html().dataset.theme).toBe("dark");
  });

  it("ignores anything that is not light or dark", () => {
    window.localStorage.setItem(THEME_KEY, "purple");
    runHeadScript();
    expect(html().hasAttribute("data-theme")).toBe(false);
  });

  it("does nothing when nothing is stored, leaving the system to decide", () => {
    runHeadScript();
    expect(html().hasAttribute("data-theme")).toBe(false);
  });

  it("does not throw when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(runHeadScript).not.toThrow();
  });
});

describe("parseTheme", () => {
  it("maps unknown values to the system", () => {
    expect(parseTheme(null)).toBe("system");
    expect(parseTheme("light")).toBe("light");
    expect(parseTheme("sepia")).toBe("system");
  });
});
