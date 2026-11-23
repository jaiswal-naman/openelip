import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiFailure } from "@/lib/api/errors";
import { DISMISS_REASON_LABELS } from "@/lib/labels";
import { DismissDialog } from "./DismissDialog";

async function open(onDismiss = vi.fn().mockResolvedValue(undefined)) {
  const user = userEvent.setup();
  render(<DismissDialog onDismiss={onDismiss} isPending={false} />);
  await user.click(screen.getByRole("button", { name: "Dismiss..." }));
  return { user, onDismiss, dialog: await screen.findByRole("dialog", { name: "Dismiss this job?" }) };
}

describe("DismissDialog", () => {
  it("offers every reason the API accepts, as one group with a visible question", async () => {
    const { dialog } = await open();
    const group = within(dialog).getByRole("radiogroup", { name: "Why are you dismissing it?" });
    expect(within(group).getAllByRole("radio").map((radio) => radio.closest("label")?.textContent)).toEqual(Object.values(DISMISS_REASON_LABELS));
  });

  it("will not dismiss without a reason, and does not call out until one is chosen", async () => {
    const { user, dialog, onDismiss } = await open();
    const button = within(dialog).getByRole("button", { name: "Dismiss job" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onDismiss).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("radio", { name: "Pay is too low" }));
    expect(button).toBeEnabled();
  });

  it("sends the reason and the comment, then closes and forgets them", async () => {
    const { user, dialog, onDismiss } = await open();
    await user.click(within(dialog).getByRole("radio", { name: "The skills do not fit" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Anything to add? (optional)" }), "Needs Rust");
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));
    expect(onDismiss).toHaveBeenCalledWith("skills_mismatch", "Needs Rust");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    const again = await screen.findByRole("dialog");
    expect(within(again).getByRole("textbox", { name: "Anything to add? (optional)" })).toHaveValue("");
    expect(within(again).getByRole("button", { name: "Dismiss job" })).toBeDisabled();
  });

  it("limits the comment to the 500 characters the API takes", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByRole("textbox", { name: "Anything to add? (optional)" })).toHaveAttribute("maxlength", "500");
  });

  it("stays open and says nothing was dismissed when the call fails", async () => {
    const { user, dialog } = await open(vi.fn().mockRejectedValue(new ApiFailure({ status: 429, code: "rate_limited" })));
    await user.click(within(dialog).getByRole("radio", { name: "Other" }));
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Too many requests. Try again in a few minutes. Nothing was dismissed.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes with Cancel and Escape without calling out", async () => {
    const { user, dialog, onDismiss } = await open();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("says in the dialog that a suggested record change waits for confirmation", async () => {
    const { dialog } = await open();
    expect(dialog).toHaveTextContent("a suggestion changes nothing until you confirm it");
  });
});
