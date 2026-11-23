import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnswerClock, clockState } from "./AnswerClock";

const NOW = new Date("2026-11-23T09:00:00Z").getTime();
const base = { dueAt: "2026-11-27T18:00:00Z", businessDaysLeft: 3, isOverdue: false, timeZone: "UTC", now: NOW };

describe("clockState (FRONTEND_UI 4.7)", () => {
  it("is neutral for more than 2 business days", () => {
    expect(clockState(base)).toEqual({ tone: "neutral", text: "3 days" });
  });
  it("is amber for 2 or 1 business days", () => {
    expect(clockState({ ...base, businessDaysLeft: 2 })).toEqual({ tone: "amber", text: "2 days" });
    expect(clockState({ ...base, businessDaysLeft: 1 })).toEqual({ tone: "amber", text: "1 day" });
  });
  it("is red with the due time under 1 business day", () => {
    expect(clockState({ ...base, businessDaysLeft: 0, dueAt: "2026-11-23T18:00:00Z" })).toEqual({ tone: "red", text: "Due today 18:00" });
  });
  it("is filled red 'Overdue' past due, from the server flag or from the passing of the due time", () => {
    expect(clockState({ ...base, isOverdue: true })).toEqual({ tone: "overdue", text: "Overdue" });
    expect(clockState({ ...base, dueAt: "2026-11-23T08:59:00Z", businessDaysLeft: 0 })).toEqual({ tone: "overdue", text: "Overdue" });
  });
});

describe("AnswerClock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("renders the server's days and tone", () => {
    render(<AnswerClock dueAt="2026-11-27T18:00:00Z" businessDaysLeft={3} isOverdue={false} timeZone="UTC" />);
    expect(screen.getByText("3 days")).toHaveAttribute("data-tone", "neutral");
  });

  it("re-renders once a minute and announces only when the tone changes", () => {
    render(<AnswerClock dueAt="2026-11-23T09:03:30Z" businessDaysLeft={0} isOverdue={false} timeZone="UTC" />);
    const pill = screen.getByText("Due today 09:03");
    expect(pill).toHaveAttribute("data-tone", "red");
    const live = screen.getByRole("status");
    expect(live).toHaveTextContent("");

    act(() => void vi.advanceTimersByTime(60_000)); // 09:01, same tone
    act(() => void vi.advanceTimersByTime(60_000)); // 09:02
    expect(live).toHaveTextContent("");

    act(() => void vi.advanceTimersByTime(2 * 60_000)); // 09:04: past due, tone red to overdue
    expect(screen.getByText("Overdue")).toHaveAttribute("data-tone", "overdue");
    expect(live).toHaveTextContent("Answer clock: Overdue");
  });
});
