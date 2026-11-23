import { describe, expect, it } from "vitest";
import { highlightAnnouncement, lineNumberAt } from "./lines";

describe("lineNumberAt", () => {
  const text = "Ada Example\nBackend engineer\nBuilt Quillscript services";

  it("is one for a span on the first line, and counts a line for every line break before it", () => {
    expect(lineNumberAt(text, 0)).toBe(1);
    expect(lineNumberAt(text, text.indexOf("Backend"))).toBe(2);
    expect(lineNumberAt(text, text.indexOf("Built"))).toBe(3);
  });

  it("counts blank lines, as the numbered lines of the resume do", () => {
    const withBlank = "Name\n\nSkills: Python";
    expect(lineNumberAt(withBlank, withBlank.indexOf("Skills"))).toBe(3);
  });

  it("counts code points, so an emoji before the span does not move it to another line", () => {
    // Code points: 😀 \n 😀 \n x. The x is the fifth code point (index 4) but sits at UTF-16 index 6.
    const emoji = "😀\n😀\nx";
    expect(lineNumberAt(emoji, 4)).toBe(3);
    expect(lineNumberAt(emoji, 2)).toBe(2);
  });
});

describe("highlightAnnouncement", () => {
  it("names the job sentence and the resume line when both are marked", () => {
    expect(highlightAnnouncement(true, 12)).toBe("Job sentence and resume line 12 highlighted.");
  });

  it("claims only the job sentence when no resume line is marked", () => {
    expect(highlightAnnouncement(true, null)).toBe("Job sentence highlighted.");
  });

  it("claims only the resume line when the posting no longer holds the quote", () => {
    expect(highlightAnnouncement(false, 4)).toBe("Resume line 4 highlighted.");
  });

  it("says nothing when nothing is marked", () => {
    expect(highlightAnnouncement(false, null)).toBe("");
  });
});
