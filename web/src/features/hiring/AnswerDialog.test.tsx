import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiFailure } from "@/lib/api/errors";
import { ANSWER_PRESETS } from "@/lib/labels";
import { AnswerDialog } from "./AnswerDialog";

async function open(answer: "yes" | "no", onSubmit = vi.fn().mockResolvedValue(undefined), onStale = vi.fn()) {
  const user = userEvent.setup();
  render(<AnswerDialog answer={answer} candidate="Demo Candidate A" onSubmit={onSubmit} onStale={onStale} />);
  await user.click(screen.getByRole("button", { name: answer === "yes" ? /Yes, let's talk/ : /No, with reason/ }));
  return { user, onSubmit, onStale, dialog: await screen.findByRole("dialog") };
}

describe("AnswerDialog", () => {
  it("requires a reason: the submit button stays disabled and nothing is sent", async () => {
    const { user, dialog, onSubmit } = await open("no");
    const send = within(dialog).getByRole("button", { name: "Send no" });
    expect(send).toBeDisabled();
    await user.click(send);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("offers the eight presets in order, each filling a box that stays editable", async () => {
    const { user, dialog } = await open("yes");
    const group = within(dialog).getByRole("group", { name: "Reasons to start from" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(ANSWER_PRESETS.map((p) => p.label));
    expect(ANSWER_PRESETS).toHaveLength(8);
    await user.click(within(group).getByRole("button", { name: "Strong fit" }));
    const box = within(dialog).getByRole("textbox", { name: "Reason (required)" });
    expect(box).toHaveValue("Strong fit for the role.");
    await user.type(box, " Great evidence.");
    expect(box).toHaveValue("Strong fit for the role. Great evidence.");
  });

  it("answers once: the reason is sent trimmed and the dialog closes", async () => {
    const { user, dialog, onSubmit } = await open("yes");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "  Good fit  ");
    await user.click(within(dialog).getByRole("button", { name: "Send yes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("Good fit");
  });

  it("ignores a second press while the first is pending", async () => {
    let finish: () => void = () => {};
    const onSubmit = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    const { user, dialog } = await open("no", onSubmit);
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Skills gap");
    const send = within(dialog).getByRole("button", { name: "Send no" });
    await user.click(send);
    await user.click(send);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    finish();
  });

  it("on already_resolved closes and asks the page to reload the card", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new ApiFailure({ status: 409, code: "already_resolved", details: { resolution: "yes" } }));
    const { user, dialog, onStale } = await open("no", onSubmit);
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Role filled");
    await user.click(within(dialog).getByRole("button", { name: "Send no" }));
    await waitFor(() => expect(onStale).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the dialog and the typed reason on another failure", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new ApiFailure({ status: 500, code: "internal_error" }));
    const { user, dialog } = await open("no", onSubmit);
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "Role filled");
    await user.click(within(dialog).getByRole("button", { name: "Send no" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Reason (required)" })).toHaveValue("Role filled");
  });
});
