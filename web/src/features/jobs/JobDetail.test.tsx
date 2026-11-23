import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JobDetail } from "./JobDetail";
import { jobDetail, requirementIn } from "./testing";
import type { JobDetail as JobDetailData } from "./types";

// The real useSearchParams reads the address; so does this one. The page writes the address with history.replaceState,
// which jsdom implements, so the whole path from the address to the highlight and back is exercised.
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));

const scrollIntoView = vi.fn();

beforeEach(() => {
  window.history.replaceState(null, "", "/jobs/7");
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({ matches: false, media: query }));
});

// Invented posting. It starts with an emoji, so every span below begins after a character that is two UTF-16 units wide.
const DESCRIPTION = "😀 Example Co builds tools.\nYou need 5 years of Python.\nExperience with Kubernetes is a plus.\nKnowing Go is a plus.";

function withRequirements(overrides: Partial<JobDetailData> = {}): JobDetailData {
  return jobDetail({
    description: DESCRIPTION,
    content_version: 2,
    requirements_version: 2,
    requirements: [
      requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 1, importance: "required" }),
      requirementIn(DESCRIPTION, "Experience with Kubernetes", { ordinal: 2, importance: "preferred" }),
      requirementIn(DESCRIPTION, "Knowing Go", { ordinal: 3, importance: "preferred" }),
    ],
    ...overrides,
  });
}

const marked = () => [...document.querySelectorAll("mark")].map((mark) => mark.textContent);
const item = (name: RegExp) => screen.getByRole("button", { name });

describe("the job's facts", () => {
  it("shows the title as the page's heading, with the company and where and how it is worked", () => {
    render(<JobDetail job={jobDetail({ title: "Platform Engineer", company_name: "Example Co", location_raw: "Pune, India", work_arrangements: ["hybrid"] })} />);
    expect(screen.getByRole("heading", { level: 1, name: "Platform Engineer" })).toBeInTheDocument();
    expect(screen.getByText("Example Co")).toBeInTheDocument();
    expect(screen.getByText("Pune, India")).toBeInTheDocument();
    expect(screen.getByText("Hybrid")).toBeInTheDocument();
  });

  it("says where the posting came from and when it was last verified open (IDX-05)", () => {
    render(<JobDetail job={jobDetail({ source: { ats: "lever", board: "example-co" }, last_verified_at: "2026-11-22T12:21:54Z" })} />);
    expect(screen.getByText("Source: Lever")).toBeInTheDocument();
    expect(screen.getByText(/Verified open/)).toHaveTextContent("Verified open 22 Nov 12:21 UTC");
    expect(screen.getByText("22 Nov 12:21 UTC").closest("time")).toHaveAttribute("datetime", "2026-11-22T12:21:54Z");
  });

  it("states employment type and pay as the posting gives them", () => {
    render(<JobDetail job={jobDetail({ employment_type: "contract", pay: { min: 120000, max: 150000, currency: "USD" } })} />);
    expect(screen.getByText("Employment type: Contract")).toBeInTheDocument();
    expect(screen.getByText(/^Pay: USD.120,000 to USD.150,000 a year$/)).toBeInTheDocument();
  });

  it("says Not stated in the posting for pay and employment type it does not give, and Location: unknown for a place that was not read", () => {
    render(<JobDetail job={jobDetail({ employment_type: "unknown", pay: null, location_raw: null, country: null, city: null, work_arrangements: [] })} />);
    expect(screen.getByText("Employment type: Not stated in the posting")).toBeInTheDocument();
    expect(screen.getByText("Pay: Not stated in the posting")).toBeInTheDocument();
    expect(screen.getByText("Location: unknown")).toBeInTheDocument();
    expect(screen.getByText("Remote: not stated")).toBeInTheDocument();
  });

  it("keeps what the posting wrote beside an unknown place", () => {
    render(<JobDetail job={jobDetail({ location_raw: "Multiple locations", country: null, city: null })} />);
    expect(screen.getByText("Location: unknown")).toBeInTheDocument();
    expect(screen.getByText("Posting says: Multiple locations")).toBeInTheDocument();
  });

  it("shows the description as text, with its line breaks kept and no markup made from it (threat T-02-34)", () => {
    render(<JobDetail job={jobDetail({ description: "Line one\nLine <b>two</b> <img src=x onerror=alert(1)>" })} />);
    const section = screen.getByRole("region", { name: "Job description" });
    expect(section).toHaveTextContent("Line one Line <b>two</b> <img src=x onerror=alert(1)>");
    expect(section.querySelector("b, img")).toBeNull();
  });

  it("says so when the posting has no text", () => {
    render(<JobDetail job={jobDetail({ description: "" })} />);
    expect(within(screen.getByRole("region", { name: "Job description" })).getByText("Not stated in the posting")).toBeInTheDocument();
  });
});

describe("the link to the posting", () => {
  it("opens the posting safely from an open job (threat T-02-35)", () => {
    render(<JobDetail job={jobDetail({ title: "Role One", apply_url: "https://jobs.example.test/1" })} />);
    const link = screen.getByRole("link", { name: "View posting for Role One (opens in a new tab)" });
    expect(link).toHaveAttribute("href", "https://jobs.example.test/1");
    expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it.each(["javascript:alert(1)", "data:text/html,hi", "ftp://files.example.test/x", "/relative"])("makes no link from %s", (url) => {
    render(<JobDetail job={jobDetail({ apply_url: url })} />);
    expect(screen.queryByRole("link", { name: /View posting/ })).not.toBeInTheDocument();
    expect(screen.getByText("No link in the posting")).toBeInTheDocument();
  });
});

describe("a closed job", () => {
  const closed = jobDetail({ status: "closed", closed_at: "2026-11-21T06:12:00Z" });

  it("says Closed with the date it closed", () => {
    render(<JobDetail job={closed} />);
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.getByText(/This job closed on/)).toHaveTextContent("This job closed on 21 Nov 06:12 UTC.");
    expect(screen.getByText("21 Nov 06:12 UTC").closest("time")).toHaveAttribute("datetime", "2026-11-21T06:12:00Z");
  });

  it("hides the link to the posting, which would send someone to a job that is gone", () => {
    render(<JobDetail job={closed} />);
    expect(screen.queryByRole("link", { name: /View posting/ })).not.toBeInTheDocument();
    expect(screen.queryByText("No link in the posting")).not.toBeInTheDocument();
  });

  it("still says it is closed when the time of closing was not recorded", () => {
    render(<JobDetail job={jobDetail({ status: "closed", closed_at: null })} />);
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.getByText(/This job has closed/)).toBeInTheDocument();
  });

  it("shows no Closed banner for an open job", () => {
    render(<JobDetail job={jobDetail()} />);
    expect(screen.queryByText("Closed")).not.toBeInTheDocument();
  });
});

describe("requirements", () => {
  it("lists them under Required and Preferred with counts", () => {
    render(<JobDetail job={withRequirements()} />);
    expect(screen.getByRole("heading", { level: 2, name: "Requirements" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Required (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Preferred (2)" })).toBeInTheDocument();
    expect(item(/5 years of Python/)).toBeInTheDocument();
  });

  it("says they have not been extracted when the requirements are for another version of the posting", () => {
    render(<JobDetail job={jobDetail({ content_version: 2, requirements_version: 1, requirements: [] })} />);
    expect(screen.getByText("Requirements have not been extracted for this version yet.")).toBeInTheDocument();
    expect(screen.queryByText("No requirements were found in this posting.")).not.toBeInTheDocument();
  });

  it("says they have not been extracted when the posting was never read for requirements", () => {
    render(<JobDetail job={jobDetail({ content_version: 1, requirements_version: null, requirements: [] })} />);
    expect(screen.getByText("Requirements have not been extracted for this version yet.")).toBeInTheDocument();
  });

  it("says none were found when the current version was read and held none", () => {
    render(<JobDetail job={jobDetail({ content_version: 3, requirements_version: 3, requirements: [] })} />);
    expect(screen.getByText("No requirements were found in this posting.")).toBeInTheDocument();
    expect(screen.queryByText("Requirements have not been extracted for this version yet.")).not.toBeInTheDocument();
  });
});

describe("selecting a requirement", () => {
  it("marks exactly its words in the posting, even after an emoji, and nothing else", async () => {
    render(<JobDetail job={withRequirements()} />);
    expect(marked()).toEqual([]);

    await userEvent.setup().click(item(/5 years of Python/));
    expect(marked()).toEqual(["5 years of Python"]);
    expect(item(/5 years of Python/)).toHaveAttribute("aria-pressed", "true");
    expect(item(/Knowing Go/)).toHaveAttribute("aria-pressed", "false");
  });

  it("scrolls the mark into view, announces it, and keeps the focus on the button", async () => {
    render(<JobDetail job={withRequirements()} />);
    const button = item(/Experience with Kubernetes/);
    await userEvent.setup().click(button);

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(document.querySelector("mark"));
    expect(screen.getByRole("status")).toHaveTextContent("Job sentence highlighted.");
    expect(button).toHaveFocus();
  });

  it("writes ?req= with the ordinal into the address, without moving the page", async () => {
    const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    render(<JobDetail job={withRequirements()} />);
    await userEvent.setup().click(item(/Knowing Go/));
    expect(window.location.pathname).toBe("/jobs/7");
    expect(window.location.search).toBe("?req=3");
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("moves the mark when another requirement is selected", async () => {
    const user = userEvent.setup();
    render(<JobDetail job={withRequirements()} />);
    await user.click(item(/5 years of Python/));
    await user.click(item(/Knowing Go/));
    expect(marked()).toEqual(["Knowing Go"]);
    expect(item(/5 years of Python/)).toHaveAttribute("aria-pressed", "false");
    expect(window.location.search).toBe("?req=3");
  });

  it("clears the mark and the address when the selected requirement is pressed again", async () => {
    const user = userEvent.setup();
    render(<JobDetail job={withRequirements()} />);
    await user.click(item(/Knowing Go/));
    await user.click(item(/Knowing Go/));
    expect(marked()).toEqual([]);
    expect(item(/Knowing Go/)).toHaveAttribute("aria-pressed", "false");
    expect(window.location.search).toBe("");
  });

  it("keeps the other parts of the address when it adds ?req=", async () => {
    window.history.replaceState(null, "", "/jobs/7?utm=newsletter#top");
    render(<JobDetail job={withRequirements()} />);
    await userEvent.setup().click(item(/Knowing Go/));
    expect(window.location.search).toBe("?utm=newsletter&req=3");
    expect(window.location.hash).toBe("#top");
  });

  it("works from the keyboard alone", async () => {
    const user = userEvent.setup();
    render(<JobDetail job={withRequirements()} />);
    item(/Experience with Kubernetes/).focus();
    await user.keyboard("{Enter}");
    expect(marked()).toEqual(["Experience with Kubernetes"]);
    await user.keyboard(" ");
    expect(marked()).toEqual([]);
    expect(item(/Experience with Kubernetes/)).toHaveFocus();
  });
});

describe("opening the page with ?req=", () => {
  it("highlights that requirement at once", () => {
    window.history.replaceState(null, "", "/jobs/7?req=3");
    render(<JobDetail job={withRequirements()} />);
    expect(marked()).toEqual(["Knowing Go"]);
    expect(item(/Knowing Go/)).toHaveAttribute("aria-pressed", "true");
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
  });

  it.each(["?req=9", "?req=abc", "?req=", "?req=-1", "?req=1.5", "?req=1e0", "?req=%3Cimg%3E", "?req=99999999999999999999"])(
    "ignores %s, which matches no requirement (threat T-02-37)",
    (query) => {
      window.history.replaceState(null, "", `/jobs/7${query}`);
      render(<JobDetail job={withRequirements()} />);
      expect(marked()).toEqual([]);
      expect(screen.getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true")).toHaveLength(0);
      expect(scrollIntoView).not.toHaveBeenCalled();
    },
  );

  it("does not read a missing ?req= as requirement number 0", () => {
    const first = requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 0, importance: "required" });
    render(<JobDetail job={withRequirements({ requirements: [first] })} />);
    expect(marked()).toEqual([]);
  });

  it("can select the requirement numbered 0 when the address asks for it", () => {
    window.history.replaceState(null, "", "/jobs/7?req=0");
    const first = requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 0, importance: "required" });
    render(<JobDetail job={withRequirements({ requirements: [first] })} />);
    expect(marked()).toEqual(["5 years of Python"]);
  });
});

describe("a citation that does not resolve", () => {
  // The span points at "Knowing Go" but the requirement's text says something else: the posting changed, or the span is wrong.
  const stale = (): JobDetailData => {
    const ok = requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 1, importance: "required" });
    const wrong = { ...requirementIn(DESCRIPTION, "Knowing Go", { ordinal: 2, importance: "preferred" }), text: "Knowing Rust" };
    return withRequirements({ requirements: [ok, wrong] });
  };

  it("is withheld, with its text still readable, and has nothing to press", () => {
    render(<JobDetail job={stale()} />);
    const row = screen.getByText("Knowing Rust").closest("li")!;
    expect(within(row).getByText("Withheld: citation did not resolve")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    expect(item(/5 years of Python/)).toBeInTheDocument();
  });

  it("is never highlighted, not even as a nearby sentence, when the address asks for it", () => {
    window.history.replaceState(null, "", "/jobs/7?req=2");
    render(<JobDetail job={stale()} />);
    expect(marked()).toEqual([]);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("is withheld when the span is off by one character: what counting UTF-16 units after the emoji would give", () => {
    const python = requirementIn(DESCRIPTION, "5 years of Python", { ordinal: 1 });
    const shifted = { ...python, span: { start: python.span.start + 1, end: python.span.end + 1 } };
    render(<JobDetail job={withRequirements({ requirements: [shifted] })} />);
    expect(screen.getByText("Withheld: citation did not resolve")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /5 years of Python/ })).not.toBeInTheDocument();
    expect(marked()).toEqual([]);
  });
});
