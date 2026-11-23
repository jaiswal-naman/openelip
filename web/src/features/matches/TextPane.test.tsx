import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TextPane } from "./TextPane";

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
});

describe("TextPane", () => {
  it("is a labelled region the keyboard can scroll, holding the text with the cited span marked", () => {
    const { container } = render(<TextPane title="Job text" text="We need Python and Go." span={{ start: 8, end: 14 }} announcement="Job sentence highlighted." empty="none" />);
    const region = screen.getByRole("region", { name: "Job text" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(container.querySelector("mark")).toHaveTextContent("Python");
    expect(region).toHaveTextContent("We need Cited: Python and Go.");
    expect(screen.getByText("Job sentence highlighted.").closest('[role="status"]')).toHaveAttribute("aria-live", "polite");
  });

  it("shows what stands in for the text, and no region, while there is none", () => {
    render(<TextPane title="Your resume" text={null} span={null} announcement="" empty={<p>Select a requirement to see where your resume states it.</p>} />);
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Your resume" })).toBeInTheDocument();
    expect(screen.getByText("Select a requirement to see where your resume states it.")).toBeInTheDocument();
  });

  it("says why nothing is marked when it is given a note", () => {
    const { container } = render(<TextPane title="Job text" text="A rewritten posting." span={null} announcement="" note="This posting has changed since" empty="none" />);
    expect(screen.getByText("This posting has changed since")).toBeInTheDocument();
    expect(container.querySelector("mark")).toBeNull();
  });

  it("marks nothing for a span that is not inside the text, never a nearby guess", () => {
    const { container } = render(<TextPane title="Job text" text="Short." span={{ start: 3, end: 99 }} announcement="x" empty="none" />);
    expect(container.querySelector("mark")).toBeNull();
    expect(screen.getByRole("region", { name: "Job text" })).toHaveTextContent("Short.");
  });
});
