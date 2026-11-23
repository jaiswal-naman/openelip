import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { citationResolves, HighlightedText } from "./HighlightedText";

const scrollIntoView = vi.fn();

/** jsdom has neither scrollIntoView nor matchMedia, so each test says what the browser would. */
function stubBrowser(reducedMotion = false) {
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reducedMotion && query === "(prefers-reduced-motion: reduce)",
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

beforeEach(() => stubBrowser());

const ANNOUNCEMENT = "Job sentence highlighted.";
const marks = (container: HTMLElement) => [...container.querySelectorAll("mark")];

describe("HighlightedText", () => {
  it("marks exactly the span, counted in code points, even when an emoji comes before it", () => {
    // "😀" is one code point but two UTF-16 units, so text.slice(2, 8) would give " Pytho" instead of "Python".
    const { container } = render(<HighlightedText text="😀 Python required" span={{ start: 2, end: 8 }} announcement={ANNOUNCEMENT} />);
    expect(marks(container).map((mark) => mark.textContent)).toEqual(["Python"]);
    expect(screen.getByText("Python").tagName).toBe("MARK");
  });

  it("keeps every other character of the text, in order, around the mark", () => {
    const text = "😀😀 Needs Python 🐍 and Go.";
    const start = Array.from("😀😀 Needs ").length;
    const { container } = render(<HighlightedText text={text} span={{ start, end: start + 6 }} announcement={ANNOUNCEMENT} />);
    const pane = container.firstElementChild!;
    const withoutHiddenPrefix = pane.textContent!.replace("Cited: ", "");
    expect(withoutHiddenPrefix).toBe(text);
    expect(marks(container)[0].textContent).toBe("Python");
  });

  it("renders the text with no mark when there is no span", () => {
    const { container } = render(<HighlightedText text="Plain text" span={null} announcement={ANNOUNCEMENT} />);
    expect(marks(container)).toHaveLength(0);
    expect(container).toHaveTextContent("Plain text");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it.each([
    ["starts before the text", { start: -1, end: 3 }],
    ["is empty", { start: 4, end: 4 }],
    ["is backwards", { start: 6, end: 2 }],
    ["runs past the end", { start: 2, end: 99 }],
  ])("marks nothing, never a nearby guess, when the span %s", (_name, span) => {
    const { container } = render(<HighlightedText text="Python required" span={span} announcement={ANNOUNCEMENT} />);
    expect(marks(container)).toHaveLength(0);
    expect(container).toHaveTextContent("Python required");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("gives a screen reader the word Cited: before the mark, outside it, so the mark holds only the quote", () => {
    const { container } = render(<HighlightedText text="Needs Python" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
    const mark = marks(container)[0];
    expect(mark.textContent).toBe("Python");
    expect(mark.previousElementSibling).toHaveTextContent("Cited:");
    expect(mark.previousElementSibling).toHaveClass("sr-only");
  });

  it("shows markup in the text as text (threat T-02-34)", () => {
    const { container } = render(<HighlightedText text="<img src=x onerror=alert(1)> Python" span={{ start: 29, end: 35 }} announcement={ANNOUNCEMENT} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(marks(container)[0].textContent).toBe("Python");
  });

  describe("moving the mark into view", () => {
    it("scrolls the mark to the centre of the view", () => {
      const { container } = render(<HighlightedText text="Needs Python" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
      expect(scrollIntoView.mock.contexts[0]).toBe(marks(container)[0]);
    });

    it("scrolls to the nearest edge, with no smooth scrolling, when the person prefers reduced motion", () => {
      stubBrowser(true);
      render(<HighlightedText text="Needs Python" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest", behavior: "auto" });
    });

    it("scrolls again when the span changes, and not when a re-render leaves it where it was", () => {
      const { rerender } = render(<HighlightedText text="Needs Python and Go" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      rerender(<HighlightedText text="Needs Python and Go" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      expect(scrollIntoView).toHaveBeenCalledTimes(1);

      rerender(<HighlightedText text="Needs Python and Go" span={{ start: 17, end: 19 }} announcement={ANNOUNCEMENT} />);
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
      expect(document.querySelector("mark")).toHaveTextContent("Go");
    });
  });

  describe("telling a screen reader", () => {
    it("announces the highlight politely when a span is marked, and nothing when there is none", () => {
      const { rerender } = render(<HighlightedText text="Needs Python" span={null} announcement={ANNOUNCEMENT} />);
      const region = screen.getByRole("status");
      expect(region).toHaveAttribute("aria-live", "polite");
      expect(region).toBeEmptyDOMElement();

      rerender(<HighlightedText text="Needs Python" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      expect(region).toHaveTextContent(ANNOUNCEMENT);

      rerender(<HighlightedText text="Needs Python" span={null} announcement={ANNOUNCEMENT} />);
      expect(region).toBeEmptyDOMElement();
    });

    it("announces again when the mark moves to another sentence, by putting in a new message instead of repeating the old text", () => {
      const { rerender } = render(<HighlightedText text="Needs Python and Go" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      const first = screen.getByText(ANNOUNCEMENT);
      rerender(<HighlightedText text="Needs Python and Go" span={{ start: 17, end: 19 }} announcement={ANNOUNCEMENT} />);
      expect(screen.getByText(ANNOUNCEMENT)).not.toBe(first);
    });

    it("keeps the announcement off the screen", () => {
      render(<HighlightedText text="Needs Python" span={{ start: 6, end: 12 }} announcement={ANNOUNCEMENT} />);
      expect(screen.getByRole("status")).toHaveClass("sr-only");
    });
  });
});

describe("citationResolves", () => {
  const points = Array.from("😀 Python required");

  it("is true when the text at the span, in code points, is the quote", () => {
    expect(citationResolves(points, { start: 2, end: 8 }, "Python")).toBe(true);
  });

  it("is false when the text changed or the span is wrong, even by one", () => {
    expect(citationResolves(points, { start: 2, end: 8 }, "Python ")).toBe(false);
    expect(citationResolves(points, { start: 3, end: 9 }, "Python")).toBe(false);
    expect(citationResolves(points, { start: 2, end: 8 }, "python")).toBe(false);
  });

  it("is false for a span that is not inside the text", () => {
    expect(citationResolves(points, { start: 9, end: 99 }, "required")).toBe(false); // would match if the end were clipped
    expect(citationResolves(points, { start: -2, end: 3 }, "😀 P")).toBe(false);
    expect(citationResolves(points, { start: 4, end: 4 }, "")).toBe(false);
  });

  it("counts code points, not UTF-16 units: the same indexes PostgreSQL and Python use", () => {
    expect("😀 Python required".slice(2, 8)).not.toBe("Python"); // the mistake this guards against
    expect(citationResolves(points, { start: 2, end: 8 }, "ython ")).toBe(false);
  });
});
