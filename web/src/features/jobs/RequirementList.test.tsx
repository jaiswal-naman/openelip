import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { RequirementList } from "./RequirementList";
import { requirementIn } from "./testing";

// Invented posting text
const DESCRIPTION = "We need 5 years of Python. Experience with Kubernetes is a plus. Knowing Go is also a plus.";
const python = requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 1, importance: "required" });
const kubernetes = requirementIn(DESCRIPTION, "Experience with Kubernetes", { ordinal: 2, importance: "preferred" });
const go = requirementIn(DESCRIPTION, "Knowing Go", { ordinal: 3, importance: "preferred" });

function renderList(props: Partial<ComponentProps<typeof RequirementList>> = {}) {
  const onSelect = vi.fn();
  render(<RequirementList requirements={[python, kubernetes, go]} resolved={new Set([1, 2, 3])} selected={null} onSelect={onSelect} {...props} />);
  return onSelect;
}

describe("RequirementList", () => {
  it("groups the items under Required and Preferred headings with their counts", () => {
    renderList();
    const required = screen.getByRole("list", { name: "Required (1)" });
    const preferred = screen.getByRole("list", { name: "Preferred (2)" });
    expect(within(required).getByRole("button", { name: /5 years of Python/ })).toBeInTheDocument();
    expect(within(preferred).getByRole("button", { name: /Experience with Kubernetes/ })).toBeInTheDocument();
    expect(within(preferred).getByRole("button", { name: /Knowing Go/ })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual(["Required (1)", "Preferred (2)"]);
  });

  it("leaves out a group that has no items instead of showing a zero", () => {
    renderList({ requirements: [python] });
    expect(screen.getByRole("heading", { name: "Required (1)" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Preferred/ })).not.toBeInTheDocument();
  });

  it("shows each item's text with its importance as a word, not only a colour", () => {
    renderList();
    const item = screen.getByRole("button", { name: /5 years of Python/ });
    expect(within(item).getByText("Required")).toBeInTheDocument();
    expect(within(item).getByText("5 years of Python")).toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: /Knowing Go/ })).getByText("Preferred")).toBeInTheDocument();
  });

  it("marks only the selected item as pressed", () => {
    renderList({ selected: 2 });
    expect(screen.getByRole("button", { name: /Experience with Kubernetes/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /5 years of Python/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: /Knowing Go/ })).toHaveAttribute("aria-pressed", "false");
  });

  it("selects an item by its ordinal when it is pressed, and clears the selection when the selected one is pressed again", async () => {
    const user = userEvent.setup();
    const onSelect = renderList({ selected: 1 });
    await user.click(screen.getByRole("button", { name: /Knowing Go/ }));
    expect(onSelect).toHaveBeenLastCalledWith(3);
    await user.click(screen.getByRole("button", { name: /5 years of Python/ }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("can be used with the keyboard alone", async () => {
    const user = userEvent.setup();
    const onSelect = renderList();
    await user.tab();
    expect(screen.getByRole("button", { name: /5 years of Python/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenLastCalledWith(1);
    await user.tab();
    await user.keyboard(" ");
    expect(onSelect).toHaveBeenLastCalledWith(2);
  });

  it("withholds an item whose citation did not resolve: it says so, offers nothing to press, and is still readable", () => {
    renderList({ resolved: new Set([1, 3]) });
    const row = screen.getByText("Experience with Kubernetes").closest("li")!;
    expect(within(row).getByText("Withheld: citation did not resolve")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    expect(within(row).getByText("Preferred")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /5 years of Python/ })).toBeInTheDocument(); // the others are unaffected
  });

  it("shows requirement text as text, never as markup (threat T-02-34)", () => {
    const hostile = { ...python, text: "<img src=x onerror=alert(1)> Python" };
    renderList({ requirements: [hostile], resolved: new Set([1]) });
    expect(screen.getByText("<img src=x onerror=alert(1)> Python")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});
