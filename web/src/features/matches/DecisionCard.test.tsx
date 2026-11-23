import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { DecisionCard } from "./DecisionCard";
import { briefDecision, DECISION_ID } from "./testing";

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const FEEDBACK = `POST /v1/matches/${DECISION_ID}/feedback`;

describe("DecisionCard", () => {
  it("is an article named by its title, with the reason it is here and the note about its apply link", () => {
    renderWithApi(<DecisionCard decision={briefDecision({ note: "Apply link not re-checked this morning" })} isOpen={false} />);
    const card = screen.getByRole("article", { name: "Demo posting (invented): Quillscript Platform Engineer at Initech Demo" });
    expect(within(card).getByText("Match")).toBeInTheDocument();
    expect(within(card).getByText("Apply link not re-checked this morning")).toBeInTheDocument();
  });

  it("saves, and shows Saved with an Unsave button", async () => {
    const user = userEvent.setup();
    renderWithApi(<DecisionCard decision={briefDecision()} isOpen={false} />, { [FEEDBACK]: ok({ is_saved: true, is_dismissed: false, proposal_event_id: null }, 201) });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("button", { name: "Unsave" })).toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("starts from the saved state the day's matches already know", () => {
    renderWithApi(<DecisionCard decision={briefDecision()} feedback={{ is_saved: true, is_dismissed: false }} isOpen={false} />);
    expect(screen.getByRole("button", { name: "Unsave" })).toBeInTheDocument();
  });

  it("collapses to a one-line receipt after a dismissal, so the list does not jump, and says a suggestion waits", async () => {
    const user = userEvent.setup();
    renderWithApi(<DecisionCard decision={briefDecision()} isOpen={false} />, { [FEEDBACK]: ok({ is_saved: false, is_dismissed: true, proposal_event_id: "e1" }, 201) });
    await user.click(screen.getByRole("button", { name: "Dismiss..." }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: "Not this company" }));
    await user.click(within(dialog).getByRole("button", { name: "Dismiss job" }));

    expect(await screen.findByText("Dismissed. Open it and save it to take that back.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss..." })).not.toBeInTheDocument();
    expect(screen.getByText(/It changes nothing until you confirm it/)).toBeInTheDocument();
  });

  it("goes back and says the choice was not saved when the server refuses", async () => {
    const user = userEvent.setup();
    renderWithApi(<DecisionCard decision={briefDecision()} isOpen={false} />, { [FEEDBACK]: fail(500, "internal_error") });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your choice was not saved.");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("shows what changed in a posting shown again", () => {
    renderWithApi(<DecisionCard decision={briefDecision({ change_note: "Title changed." })} isOpen={false} />);
    expect(screen.getByText("Changed since you saw it")).toBeInTheDocument();
    expect(screen.getByText("Title changed.")).toBeInTheDocument();
  });
});
