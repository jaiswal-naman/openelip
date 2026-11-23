import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiFailure } from "@/lib/api/errors";
import { LoadMore } from "./LoadMore";

describe("LoadMore", () => {
  it("offers a Load more button that asks for the next page", async () => {
    const onLoad = vi.fn();
    render(<LoadMore isPending={false} error={null} onLoad={onLoad} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Load more" }));
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("ignores presses while a page is on its way, so one click cannot ask twice", async () => {
    const onLoad = vi.fn();
    render(<LoadMore isPending error={null} onLoad={onLoad} />);
    const button = screen.getByRole("button", { name: "Load more" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    await userEvent.setup().click(button);
    expect(onLoad).not.toHaveBeenCalled();
  });

  it("says what failed in the words for the error code, offers Retry, and shows no second button", async () => {
    const onLoad = vi.fn();
    render(<LoadMore isPending={false} error={new ApiFailure({ status: 503, code: "dependency_unavailable" })} onLoad={onLoad} />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load more jobs.");
    expect(alert).toHaveTextContent("A service this needs is unavailable. Try again shortly.");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry" }));
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("gives a cursor the API refused the same plain message as any bad request", () => {
    render(<LoadMore isPending={false} error={new ApiFailure({ status: 400, code: "invalid_request" })} onLoad={() => {}} />);
    expect(screen.getByRole("alert")).toHaveTextContent("The request was not valid.");
  });
});
