import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { CostsView } from "./CostsView";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: nav.replace, push: vi.fn(), refresh: vi.fn() }) }));

// An invented account and invented ledger rows (example_task is not a real task)
const me = ok({ id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "reader@example.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" });
const LABEL = "Measured: sums of recorded model calls.";

const byMonth = (rows: unknown[], total = "1.120000", calls = 252) =>
  ok({ group_by: "month", currency: "USD", total_usd: total, model_calls: calls, rows, label: LABEL });
const byTask = (rows: unknown[], total = "1.120000", calls = 252) =>
  ok({ group_by: "task", currency: "USD", total_usd: total, model_calls: calls, rows, label: LABEL });

const MONTHS = [
  { period: "2026-11", task: null, calls: 212, cost_usd: "0.480000", input_tokens: 120000, output_tokens: 30000 },
  { period: "2026-10", task: null, calls: 40, cost_usd: "0.640000", input_tokens: 9000, output_tokens: 1200 },
];
const TASKS = [
  { period: null, task: "example_task", calls: 200, cost_usd: "1.000000", input_tokens: 100000, output_tokens: 25000 },
  { period: null, task: "ping", calls: 52, cost_usd: "0.004200", input_tokens: 5200, output_tokens: 1040 },
];

const routes = (month: Response, task: Response) => ({
  "GET /v1/me": me,
  "GET /v1/control/costs": (request: Request) => (new URL(request.url).searchParams.get("group_by") === "task" ? task : month),
});

beforeEach(() => nav.replace.mockReset());

describe("the costs page", () => {
  it("labels every figure as measured operational data from the ledger, and shows the total", async () => {
    renderWithApi(<CostsView />, routes(byMonth(MONTHS), byTask(TASKS)));
    expect(await screen.findByRole("heading", { level: 1, name: "My model cost" })).toBeInTheDocument();
    expect(screen.getByText("Measured operational data from the cost ledger.")).toBeInTheDocument();
    const total = await screen.findByText("Total, all recorded calls");
    expect(total.parentElement).toHaveTextContent("USD 1.12");
    expect(total.parentElement).toHaveTextContent("252 model calls");
  });

  it("has a table by month with calls, tokens in and out, cost and share of the total", async () => {
    renderWithApi(<CostsView />, routes(byMonth(MONTHS), byTask(TASKS)));
    const table = await screen.findByRole("table", { name: /Model cost by month/ });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Month",
      "Calls",
      "Tokens in / out",
      "Cost",
      "Share of total",
    ]);
    const november = within(table).getByRole("row", { name: /Nov 2026/ });
    expect(within(november).getByRole("rowheader")).toHaveTextContent("Nov 2026");
    expect(november).toHaveTextContent("212");
    // 120,000 is above 9,999, so it is shown compact with the exact value kept for screen readers and the hover title
    expect(november).toHaveTextContent("120K");
    expect(november).toHaveTextContent("(120,000)");
    expect(november).toHaveTextContent("30K");
    expect(within(november).getByTitle("120,000")).toBeInTheDocument();
    expect(november).toHaveTextContent("USD 0.48");
    expect(november).toHaveTextContent("43%"); // 0.48 of 1.12, printed beside the bar
  });

  it("has a table by task, and shows a sub-cent cost to four decimals, never as zero", async () => {
    renderWithApi(<CostsView />, routes(byMonth(MONTHS), byTask(TASKS)));
    const table = await screen.findByRole("table", { name: /Model cost by task/ });
    const ping = within(table).getByRole("row", { name: /ping/ });
    expect(ping).toHaveTextContent("USD 0.0042");
    expect(ping).toHaveTextContent("<1%"); // a real share is not shown as 0%
    expect(within(table).getByRole("row", { name: /example_task/ })).toHaveTextContent("89%");
  });

  it("draws each share as an SVG bar that screen readers skip, because the percentage is printed beside it", async () => {
    const { container } = renderWithApi(<CostsView />, routes(byMonth(MONTHS), byTask(TASKS)));
    await screen.findByRole("table", { name: /by task/ });
    const bars = container.querySelectorAll("svg");
    expect(bars).toHaveLength(MONTHS.length + TASKS.length);
    bars.forEach((bar) => expect(bar).toHaveAttribute("aria-hidden", "true"));
  });

  it("does not invent a share when nothing has a cost: a free fake provider shows no bar and no percentage", async () => {
    const free = [{ period: null, task: "ping", calls: 3, cost_usd: "0.000000", input_tokens: 300, output_tokens: 60 }];
    const freeMonth = [{ period: "2026-11", task: null, calls: 3, cost_usd: "0.000000", input_tokens: 300, output_tokens: 60 }];
    const { container } = renderWithApi(<CostsView />, routes(byMonth(freeMonth, "0.000000", 3), byTask(free, "0.000000", 3)));
    const table = await screen.findByRole("table", { name: /by task/ });
    expect(within(table).getByRole("row", { name: /ping/ })).toHaveTextContent("USD 0.00");
    expect(within(table).getAllByText("No cost recorded")).toHaveLength(1);
    expect(container.querySelectorAll("svg")).toHaveLength(0);
    expect(container.textContent).not.toMatch(/%/);
  });

  it("explains an empty ledger, with the one action that changes it, naming the signed-in person", async () => {
    renderWithApi(<CostsView />, routes(byMonth([], "0.000000", 0), byTask([], "0.000000", 0)));
    expect(await screen.findByText(/No model calls have been recorded for your account yet/)).toBeInTheDocument();
    expect(screen.getByText(/python -m app.platform.llm ping --email reader@example.test/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows what failed with a Retry that works, and keeps the title and label on screen", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    renderWithApi(<CostsView />, {
      "GET /v1/me": me,
      "GET /v1/control/costs": (request) => {
        if (++attempt <= 2) return fail(503, "dependency_unavailable"); // both group-by calls fail the first time
        return new URL(request.url).searchParams.get("group_by") === "task" ? byTask(TASKS) : byMonth(MONTHS);
      },
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load your model costs.");
    expect(alert).toHaveTextContent("A service this needs is unavailable. Try again shortly.");
    expect(screen.getByRole("heading", { name: "My model cost" })).toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table", { name: /by month/ })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sends someone whose session ended to sign in, and back here afterwards", async () => {
    renderWithApi(<CostsView />, routes(fail(401, "unauthenticated"), fail(401, "unauthenticated")));
    expect(await screen.findByText(/Your session has ended/)).toBeInTheDocument();
    expect(nav.replace).toHaveBeenCalledWith("/sign-in?next=%2Fcontrol%2Fcosts");
  });
});
