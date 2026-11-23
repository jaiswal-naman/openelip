import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ok, renderWithApi } from "@/test/render";
import { TodayIntroductions } from "./TodayIntroductions";
import { ASK, detail, item, OTHER, PAST, signals } from "./testing";

function routes(over: Record<string, Response | ((request: Request) => Response)> = {}) {
  return {
    "GET /v1/introductions": ok([item()]),
    [`GET /v1/introductions/${ASK}`]: ok(detail()),
    "GET /v1/signals": ok(signals()),
    ...over,
  };
}

describe("introductions on Today", () => {
  it("shows each open ask as an INTRODUCTION card with the demo pill, the pay and the expiry, and nothing expired or answered", async () => {
    renderWithApi(
      <TodayIntroductions openId={null} />,
      routes({
        "GET /v1/introductions": ok([item(), item({ id: OTHER, expires_at: PAST, role_title: "Lapsed role" }), item({ id: "x", status: "declined", role_title: "Declined role" })]),
      }),
    );
    const card = await screen.findByRole("article", { name: "Halden Demo asks about Backend engineer" });
    expect(within(card).getByText("Introduction")).toBeInTheDocument();
    expect(await within(card).findByText("Demo (invented)")).toBeInTheDocument();
    expect(within(card).getByText("EUR 70,000 to 95,000 a year")).toBeInTheDocument();
    expect(within(card).getByText(/Answer by 30 Nov/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Review and accept" })).toBeInTheDocument();
    expect(screen.queryByText(/Lapsed role/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Declined role/)).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Review and accept opens the consent sheet", async () => {
    const user = userEvent.setup();
    renderWithApi(<TodayIntroductions openId={null} />, routes());
    await user.click(await screen.findByRole("button", { name: "Review and accept" }));
    expect(await screen.findByRole("heading", { name: "Share with Halden Demo for Backend engineer?" })).toBeInTheDocument();
  });

  it("?decision=<id> opens that ask's sheet on load and marks its card", async () => {
    renderWithApi(<TodayIntroductions openId={ASK} />, routes());
    expect(await screen.findByRole("heading", { name: "Share with Halden Demo for Backend engineer?" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Halden Demo asks about Backend engineer", hidden: true })).toHaveAttribute("aria-current", "true");
  });

  it("?decision= naming an ask that is no longer open opens nothing", async () => {
    renderWithApi(<TodayIntroductions openId={ASK} />, routes({ "GET /v1/introductions": ok([item({ status: "accepted", responded_at: "2026-11-23T09:00:00Z" })]) }));
    expect(await screen.findByRole("region", { name: "Introductions you accepted" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("after accepting, the card moves to the accepted list, which waits for their answer", async () => {
    const user = userEvent.setup();
    let accepted = false;
    renderWithApi(
      <TodayIntroductions openId={null} />,
      routes({
        "GET /v1/introductions": () => ok([accepted ? item({ status: "accepted", responded_at: "2026-11-23T09:00:00Z" }) : item()]),
        [`GET /v1/introductions/${ASK}`]: () => ok(detail(accepted ? { status: "accepted" } : {})),
        [`POST /v1/introductions/${ASK}/accept`]: () => {
          accepted = true;
          return ok({ introduction: { id: ASK, status: "accepted" }, shared: "x" });
        },
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Review and accept" }));
    await user.click(await screen.findByRole("button", { name: "Accept and share" }));
    const section = await screen.findByRole("region", { name: "Introductions you accepted" });
    expect(within(section).getByText("Halden Demo: Backend engineer")).toBeInTheDocument();
    expect(within(section).getByText("Accepted 23 Nov")).toBeInTheDocument();
    expect(within(section).getByText("Waiting for their answer")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByRole("article", { name: "Halden Demo asks about Backend engineer" })).not.toBeInTheDocument();
  });

  it("an accepted introduction shows the employer's answer with its reason and their serious interest", async () => {
    renderWithApi(
      <TodayIntroductions openId={null} />,
      routes({
        "GET /v1/introductions": ok([item({ status: "accepted", responded_at: "2026-11-23T09:00:00Z", employer_answer: "yes" })]),
        [`GET /v1/introductions/${ASK}`]: ok(detail({ status: "accepted", employer_answer: "yes", employer_answer_reason: "Strong fit for the role.", has_employer_signal: true })),
      }),
    );
    const section = await screen.findByRole("region", { name: "Introductions you accepted" });
    expect(within(section).getByText("They answered yes.")).toBeInTheDocument();
    expect(await within(section).findByText(/Their reason: Strong fit for the role\./)).toBeInTheDocument();
    expect(within(section).getByText("Serious interest from the employer")).toBeInTheDocument();
  });

  it("the person can send one serious-interest signal per accepted introduction, within the weekly quota", async () => {
    const user = userEvent.setup();
    let sent = false;
    const { calls } = renderWithApi(
      <TodayIntroductions openId={null} />,
      routes({
        "GET /v1/introductions": ok([item({ status: "accepted", responded_at: "2026-11-23T09:00:00Z" })]),
        [`GET /v1/introductions/${ASK}`]: ok(detail({ status: "accepted" })),
        "GET /v1/signals": () => ok(sent ? signals({ sent: [{ id: "s", sender_side: "candidate", role_id: "role-1", introduction_id: ASK, created_at: "2026-11-23T10:00:00Z" }], quota: { used: 2, limit: 3, resets_at: "2026-11-30T00:00:00Z" } }) : signals()),
        "POST /v1/signals": () => {
          sent = true;
          return ok({ id: "s" }, 201);
        },
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Send serious interest (2 of 3 left this week)" }));
    expect(await screen.findByText("You sent serious interest")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Send serious interest/ })).not.toBeInTheDocument();
    const posts = calls.filter((request) => request.method === "POST");
    expect(posts).toHaveLength(1);
    expect(await posts[0]!.clone().json()).toEqual({ introduction_id: ASK });
  });

  it("no button when the week's signals are used up", async () => {
    renderWithApi(
      <TodayIntroductions openId={null} />,
      routes({
        "GET /v1/introductions": ok([item({ status: "accepted", responded_at: "2026-11-23T09:00:00Z" })]),
        [`GET /v1/introductions/${ASK}`]: ok(detail({ status: "accepted" })),
        "GET /v1/signals": ok(signals({ quota: { used: 3, limit: 3, resets_at: "2026-11-30T00:00:00Z" } })),
      }),
    );
    await screen.findByText("Waiting for their answer");
    await waitFor(() => expect(screen.queryByRole("button", { name: /Send serious interest/ })).not.toBeInTheDocument());
  });
});
