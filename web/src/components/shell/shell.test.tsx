import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Home from "@/app/page";
import { INDEPENDENCE_STATEMENT, PRODUCT_NAME } from "@/lib/product";
import { SiteFooter } from "./SiteFooter";
import { SiteHeader } from "./SiteHeader";
import { ThemeToggle } from "./ThemeToggle";

vi.mock("next/navigation", () => ({ usePathname: () => "/jobs" }));

describe("ThemeToggle", () => {
  it("offers system, light and dark, and starts on the system", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);
    await user.click(screen.getByRole("button", { name: "Theme: System" }));
    // React Aria names a menu after the button that opened it, so a screen reader hears "Theme: System, menu"
    const options = within(await screen.findByRole("menu", { name: "Theme: System" })).getAllByRole("menuitemradio");
    expect(options.map((option) => option.textContent)).toEqual(["System", "Light", "Dark"]);
    expect(options[0]).toBeChecked();
  });

  it("switches the theme from the menu and remembers it", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);
    await user.click(screen.getByRole("button", { name: "Theme: System" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Dark" }));

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("theme")).toBe("dark");
    expect(screen.getByRole("button", { name: "Theme: Dark" })).toBeInTheDocument();
  });

  it("can be used from the keyboard alone", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);
    screen.getByRole("button", { name: "Theme: System" }).focus();
    await user.keyboard("{Enter}");
    await user.keyboard("{ArrowDown}{Enter}"); // System, then Light
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("SiteHeader", () => {
  it("has the landmarks, the product name, the two links and a way to sign in", () => {
    render(<SiteHeader />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: `${PRODUCT_NAME} home` })).toHaveAttribute("href", "/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/jobs");
    expect(within(nav).getByRole("link", { name: "Trust" })).toHaveAttribute("href", "/trust");
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });

  it("marks the page you are on, for screen readers as well as the eye", () => {
    render(<SiteHeader />);
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Trust" })).not.toHaveAttribute("aria-current");
  });

  it("starts with a skip link to the main content", () => {
    render(<SiteHeader />);
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main");
  });
});

describe("SiteFooter", () => {
  it("says the project is independent and not affiliated", () => {
    render(<SiteFooter />);
    expect(screen.getByRole("contentinfo")).toHaveTextContent(INDEPENDENCE_STATEMENT);
  });
});

describe("home page", () => {
  it("links to the job search and the trust page", () => {
    render(<Home />);
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Search open jobs" })).toHaveAttribute("href", "/jobs");
    expect(screen.getByRole("link", { name: "See where your data goes" })).toHaveAttribute("href", "/trust");
  });

  it("lists only what works today, each with a text status", () => {
    render(<Home />);
    const today = screen.getByRole("region", { name: "What works today" });
    expect(within(today).getAllByText("Working")).toHaveLength(4);
    expect(today).toHaveTextContent("planned and not built yet");
  });

  it("shows no figure at all: a number on this page would have to come from a recorded measurement", () => {
    const { container } = render(<Home />);
    expect(container.textContent).not.toMatch(/\d/);
  });
});
