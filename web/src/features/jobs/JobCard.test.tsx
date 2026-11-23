import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { JobCard } from "./JobCard";
import { job } from "./testing";
import type { Job } from "./types";

/** A card is a list item, so it is rendered inside a list as the screen does. */
function renderCard(overrides: Partial<Job> = {}) {
  render(
    <ul>
      <JobCard job={job(overrides)} />
    </ul>,
  );
  return screen.getByRole("listitem");
}

describe("JobCard", () => {
  it("links the title to the job's own page", () => {
    renderCard({ id: 42, title: "Data Engineer" });
    expect(screen.getByRole("heading", { level: 2 })).toContainElement(screen.getByRole("link", { name: "Data Engineer" }));
    expect(screen.getByRole("link", { name: "Data Engineer" })).toHaveAttribute("href", "/jobs/42");
  });

  it("says where the posting came from and when it was last verified open (IDX-05)", () => {
    const card = renderCard({ last_verified_at: "2026-11-22T12:21:54Z" });
    expect(within(card).getByText("Source: Greenhouse")).toBeInTheDocument();
    expect(within(card).getByText(/Verified open/)).toHaveTextContent("Verified open 22 Nov 12:21 UTC");
    expect(within(card).getByText("22 Nov 12:21 UTC").closest("time")).toHaveAttribute("datetime", "2026-11-22T12:21:54Z");
  });

  it.each([
    ["greenhouse", "Source: Greenhouse"],
    ["lever", "Source: Lever"],
    ["ashby", "Source: Ashby"],
  ] as const)("names a %s posting's source", (ats, line) => {
    renderCard({ source: { ats, board: "example-co" } });
    expect(screen.getByText(line)).toBeInTheDocument();
  });

  it("shows the company", () => {
    renderCard({ company_name: "Globex" });
    expect(screen.getByText("Globex")).toBeInTheDocument();
  });

  it("does not invent a company name", () => {
    renderCard({ company_name: "  " });
    expect(screen.getByText("Company not stated")).toBeInTheDocument();
  });

  describe("location", () => {
    it("shows the posting's own words when the place was read", () => {
      renderCard({ location_raw: "Pune, India", country: "IN", city: "Pune" });
      expect(screen.getByText("Pune, India")).toBeInTheDocument();
      expect(screen.queryByText("Location: unknown")).not.toBeInTheDocument();
    });

    it("says Location: unknown when no country and no city were read (IDX-06)", () => {
      renderCard({ location_raw: null, country: null, city: null });
      expect(screen.getByText("Location: unknown")).toBeInTheDocument();
      expect(screen.queryByText(/Posting says/)).not.toBeInTheDocument();
    });

    it("keeps what the posting wrote beside an unknown place, and never turns it into a country", () => {
      renderCard({ location_raw: "Middle East", country: null, city: null });
      expect(screen.getByText("Location: unknown")).toBeInTheDocument();
      expect(screen.getByText("Posting says: Middle East")).toBeInTheDocument();
      expect(screen.queryByText(/Saudi|UAE|Emirates/)).not.toBeInTheDocument();
    });
  });

  describe("work arrangement", () => {
    it.each([
      [[], "Remote: not stated"],
      [["remote"], "Remote"],
      [["hybrid"], "Hybrid"],
      [["onsite"], "On-site"],
      [["onsite", "hybrid"], "On-site or Hybrid"],
    ] as const)("shows %j as %s", (arrangements, text) => {
      const card = renderCard({ work_arrangements: [...arrangements] });
      expect(within(card).getByText(text)).toBeInTheDocument();
    });
  });

  describe("the link out", () => {
    it("opens the posting safely, with a name that says where it goes", () => {
      renderCard({ title: "Role One", apply_url: "https://jobs.example.test/1" });
      const link = screen.getByRole("link", { name: "View posting for Role One (opens in a new tab)" });
      expect(link).toHaveAttribute("href", "https://jobs.example.test/1");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
      expect(link).toHaveAttribute("target", "_blank");
    });

    it("is not made from a scheme that could run script (threat T-02-35)", () => {
      renderCard({ apply_url: "javascript:alert(1)" });
      expect(screen.getByText("No link in the posting")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /View posting/ })).not.toBeInTheDocument();
    });
  });

  it("shows posting text as text, never as markup (threat T-02-34)", () => {
    renderCard({ title: "<img src=x onerror=alert(1)> Engineer", company_name: "<b>Bold</b> Co" });
    expect(screen.getByRole("link", { name: "<img src=x onerror=alert(1)> Engineer" })).toBeInTheDocument();
    expect(screen.getByText("<b>Bold</b> Co")).toBeInTheDocument();
    expect(document.querySelector("img, b")).toBeNull();
  });
});
