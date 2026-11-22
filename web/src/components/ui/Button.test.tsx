import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button } from "./Button";

describe("Button", () => {
  it("presses when it is not pending", async () => {
    const press = vi.fn();
    render(<Button onPress={press}>Save</Button>);
    await userEvent.setup().click(screen.getByRole("button", { name: "Save" }));
    expect(press).toHaveBeenCalledOnce();
  });

  it("keeps its label, shows a spinner and ignores presses while pending", async () => {
    const press = vi.fn();
    const { container } = render(
      <Button onPress={press} isPending>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    await userEvent.setup().click(button);
    expect(press).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(container.querySelector("button span[aria-hidden='true']")).not.toBeNull(); // the spinner
  });

  it("stays focusable while pending, so a keyboard user keeps their place", () => {
    render(<Button isPending>Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    button.focus();
    expect(button).toHaveFocus();
    expect(button).toBeEnabled();
  });

  it("stops being a submit button while pending, so Enter in a field cannot send the form twice", () => {
    const { rerender } = render(<Button type="submit">Send</Button>);
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("type", "submit");
    rerender(
      <Button type="submit" isPending>
        Send
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("type", "button");
  });

  it("leaves no hidden image nodes behind, which axe reports as missing alternative text", async () => {
    render(<Button isPending>Save</Button>);
    await userEvent.setup().click(screen.getByRole("button", { name: "Save" }));
    expect(document.querySelectorAll('[role="img"]')).toHaveLength(0);
  });
});
