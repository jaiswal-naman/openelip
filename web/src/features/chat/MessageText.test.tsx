import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageText } from "./MessageText";
import type { Citation } from "./queries";

const decision: Citation = { alias: "d1", kind: "decision", decision_id: "6f1d2c3e-aaaa-4bbb-8ccc-0123456789ab", job_id: null, fact_id: null, constraint_id: null };

describe("MessageText", () => {
  it("turns a marker the API vouched for into a chip that links to an internal page", () => {
    render(<MessageText text="It was ruled out [[d1]]." citations={{ d1: decision }} />);
    const chip = screen.getByRole("link", { name: "Decision 1" });
    expect(chip).toHaveAttribute("href", `/app/matches/${decision.decision_id}`);
  });

  it("leaves a marker with no citation as plain text", () => {
    render(<MessageText text="See [[d9]] for why." citations={{}} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("See [[d9]] for why.")).toBeInTheDocument();
  });

  it("links a job and a fact to pages that exist, and shows a kind with no page as a chip without a link", () => {
    render(
      <MessageText
        text="[[j1]] [[f1]] [[x1]]"
        citations={{
          j1: { alias: "j1", kind: "job", decision_id: null, job_id: 42, fact_id: null, constraint_id: null },
          f1: { alias: "f1", kind: "fact", decision_id: null, job_id: null, fact_id: "7", constraint_id: null },
        }}
      />,
    );
    expect(screen.getByRole("link", { name: "Job 1" })).toHaveAttribute("href", "/jobs/42");
    expect(screen.getByRole("link", { name: "Record 1" })).toHaveAttribute("href", "/app/record");
  });

  it("renders a model-written link, a bare URL and an image as plain text", () => {
    const { container } = render(
      <MessageText text={'Click [here](https://evil.example) or https://evil.example now <img src=x onerror=alert(1)> <a href="https://evil.example">x</a>'} citations={{}} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("[here](https://evil.example)");
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("does not let a citation link to an outside address even if the event carries odd ids", () => {
    const odd: Citation = { alias: "d1", kind: "decision", decision_id: "javascript:alert(1)", job_id: null, fact_id: null, constraint_id: null };
    render(<MessageText text="[[d1]]" citations={{ d1: odd }} />);
    const href = screen.getByRole("link").getAttribute("href") ?? "";
    expect(href.startsWith("/app/matches/")).toBe(true);
    expect(href).not.toContain(":");
  });
});
