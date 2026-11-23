import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { bodyOf, recordView } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { ConstraintCards } from "./ConstraintCards";

/** A saved hard constraint as the API shows it: every typed field is there, null where the kind does not use it. */
function constraint(kind: string, statement: string, fields: Record<string, unknown> = {}) {
  return {
    id: `7c2d9e41-5a6b-4c3d-8e9f-0a1b2c3d4e${kind.length}`,
    kind,
    statement,
    source: "candidate_edit",
    created_at: "2026-11-23T05:00:00Z",
    country_codes: null,
    cities: null,
    arrangements: null,
    employment_types: null,
    seniorities: null,
    salary_floor: null,
    company_names: null,
    ...fields,
  };
}

/** A record that remembers the constraints saved to it, like the real one. */
function statefulRecord(initial: unknown[] = []) {
  let constraints = initial;
  const posted: unknown[] = [];
  return {
    posted,
    routes: {
      "GET /v1/me/record": () => ok(recordView({ constraints })),
      "POST /v1/me/constraints": async (request: Request) => {
        const body = (await bodyOf(request)) as { kind: string };
        posted.push(body);
        const saved = constraint(body.kind, (body as unknown as { statement: string }).statement, body);
        constraints = [...constraints.filter((item) => (item as { kind: string }).kind !== body.kind), saved];
        return ok({ constraint: saved, record_version: 4 }, 201);
      },
    },
  };
}

const headings = () => screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent);

/** Skip the cards in front of the one a test is about. Each Skip opens the next card, so the next one is found after it. */
async function skipCards(user: ReturnType<typeof userEvent.setup>, count: number) {
  for (let skipped = 0; skipped < count; skipped += 1) await user.click(await screen.findByRole("button", { name: "Skip" }));
}

describe("the hard-constraint cards in the intake", () => {
  it("shows one card per kind, in the order of the intake, each not set until answered", async () => {
    const { routes } = statefulRecord();
    renderWithApi(<ConstraintCards mode="intake" />, routes);
    await screen.findByRole("heading", { level: 3, name: "Location" });
    expect(headings()).toEqual(["Location", "Remote policy", "Work authorization", "Salary floor", "Employment type", "Seniority", "Excluded companies"]);
    expect(screen.getAllByText("Not set (not enforced)")).toHaveLength(7); // nothing is set until a card is confirmed
    expect(screen.getByLabelText("Countries you want jobs in")).toBeInTheDocument(); // the first card is open
    expect(screen.queryByRole("group", { name: "Ways of working you accept" })).not.toBeInTheDocument(); // the rest wait
  });

  it("opens the first card, and moves to the next when a card is skipped, with nothing sent", async () => {
    const user = userEvent.setup();
    const { routes } = statefulRecord();
    const { calls } = renderWithApi(<ConstraintCards mode="intake" />, routes);
    expect(await screen.findByLabelText("Countries you want jobs in")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip" }));
    expect(await screen.findByRole("group", { name: "Ways of working you accept" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Countries you want jobs in")).not.toBeInTheDocument();
    expect(calls.filter((call) => call.method !== "GET")).toHaveLength(0);
    // the skipped card says what that means
    const location = screen.getByRole("heading", { level: 3, name: "Location" }).closest("li")!;
    expect(within(location).getByText("Not set (not enforced)")).toBeInTheDocument();
  });

  it("reads a salary floor back in a sentence, saves that sentence with the typed value, and opens the next card", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    const { calls } = renderWithApi(<ConstraintCards mode="intake" />, routes);
    await skipCards(user, 3); // location, remote policy, work authorization
    await user.type(await screen.findByRole("textbox", { name: "Amount" }), "3000000");
    await user.type(screen.getByRole("textbox", { name: "Currency" }), "inr");
    expect(screen.getByText("You will never see jobs that pay less than INR 30,00,000 a year.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      kind: "salary_floor",
      statement: "You will never see jobs that pay less than INR 30,00,000 a year.",
      salary_floor: { amount: 3000000, currency: "INR" },
    });
    expect(calls.find((call) => call.method === "POST")!.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByRole("group", { name: "Types of employment you accept" })).toBeInTheDocument();
    const floor = screen.getByRole("heading", { level: 3, name: "Salary floor" }).closest("li")!;
    expect(within(floor).getByText("You will never see jobs that pay less than INR 30,00,000 a year.")).toBeInTheDocument();
  });

  it("saves a monthly figure as the yearly one it comes to, and says the yearly one", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    renderWithApi(<ConstraintCards mode="intake" />, routes);
    await skipCards(user, 3); // location, remote policy, work authorization
    await user.type(await screen.findByRole("textbox", { name: "Amount" }), "250000");
    await user.type(screen.getByRole("textbox", { name: "Currency" }), "INR");
    await user.selectOptions(screen.getByLabelText("Period"), "month");
    expect(screen.getByText("You will never see jobs that pay less than INR 30,00,000 a year.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posted[0]).toMatchObject({ salary_floor: { amount: 3000000, currency: "INR" } }));
  });

  it("saves a remote policy from the boxes ticked, and refuses to save none", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    renderWithApi(<ConstraintCards mode="intake" />, routes);
    await user.click(await screen.findByRole("button", { name: "Skip" })); // location
    await screen.findByRole("group", { name: "Ways of working you accept" });

    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Choose at least one way of working you accept.")).toBeInTheDocument();
    expect(posted).toHaveLength(0);

    await user.click(screen.getByRole("checkbox", { name: "Remote" }));
    await user.click(screen.getByRole("checkbox", { name: "Hybrid" }));
    expect(screen.getByText("You will only see hybrid or remote jobs.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posted[0]).toEqual({ kind: "remote_policy", statement: "You will only see hybrid or remote jobs.", arrangements: ["remote", "hybrid"] }));
  });

  it("takes several countries and cities for a location, losing none", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    renderWithApi(<ConstraintCards mode="intake" />, routes);
    const countries = await screen.findByLabelText("Countries you want jobs in");
    await user.selectOptions(countries, "IN");
    await user.selectOptions(screen.getByLabelText("Countries you want jobs in"), "AE");
    expect(screen.getByRole("list", { name: "Countries you want jobs in, chosen" })).toHaveTextContent("IndiaRemoveUnited Arab EmiratesRemove");
    await user.type(screen.getByRole("textbox", { name: "Cities (optional)" }), "Bengaluru{Enter}Pune");
    await user.click(screen.getByRole("button", { name: "Remove United Arab Emirates" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posted[0]).toEqual({ kind: "location", statement: "You will only see jobs in Bengaluru or Pune, India.", country_codes: ["IN"], cities: ["Bengaluru", "Pune"] }));
  });

  it("offers a preference instead when the API says a constraint cannot be enforced, and saves it as one", async () => {
    const user = userEvent.setup();
    const sent: unknown[] = [];
    renderWithApi(<ConstraintCards mode="intake" />, {
      "GET /v1/me/record": ok(recordView()),
      "POST /v1/me/constraints": fail(422, "constraint_not_enforceable", {
        supported_kinds: [],
        suggested_preference: { kind: "untyped_dealbreaker", statement: "You will only see on-site jobs." },
      }),
      "POST /v1/me/preferences": async (request) => {
        sent.push(await bodyOf(request));
        return ok({ preference: { id: "p1", kind: "untyped_dealbreaker", value: "x", weight: -3, statement: "x", source: "candidate_edit", created_at: "2026-11-23T05:00:00Z", notice: "not_guaranteed" }, record_version: 5 }, 201);
      },
    });
    await user.click(await screen.findByRole("button", { name: "Skip" })); // location
    await user.click(await screen.findByRole("checkbox", { name: "On-site" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This cannot be checked against job postings as a hard rule, so it cannot be guaranteed.");
    await user.click(within(alert).getByRole("button", { name: "Save as a preference instead" }));

    await waitFor(() => expect(sent).toEqual([{ kind: "untyped_dealbreaker", statement: "You will only see on-site jobs." }]));
    const card = screen.getByRole("heading", { level: 3, name: "Remote policy" }).closest("li")!;
    expect(within(card).getByText(/Saved as a preference instead\. We cannot check this in job data, so it is a preference, not a guarantee\./)).toBeInTheDocument();
  });

  it("shows the not-kept notice, and keeps nothing, when a field states a protected characteristic", async () => {
    const user = userEvent.setup();
    renderWithApi(<ConstraintCards mode="record" />, {
      "GET /v1/me/record": ok(recordView()),
      "POST /v1/me/constraints": fail(422, "validation_failed", { fields: [{ field: "company_names", issue: "protected_characteristic" }] }),
    });
    await user.click(await screen.findByRole("button", { name: "Set excluded companies" }));
    await user.type(screen.getByRole("textbox", { name: "Companies you never want to see" }), "Initech Sample Co");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("We do not keep this in your record.");
  });
});

describe("the hard-constraint cards on the record page", () => {
  const SALARY = constraint("salary_floor", "You will never see jobs that pay less than INR 28,00,000 a year.", { salary_floor: { amount: 2800000, currency: "INR" } });

  it("closes every card, shows what is saved and what is not set, and opens nothing by itself", async () => {
    const { routes } = statefulRecord([SALARY]);
    renderWithApi(<ConstraintCards mode="record" />, routes);
    const floor = (await screen.findByRole("heading", { level: 3, name: "Salary floor" })).closest("li")!;
    expect(within(floor).getByText("You will never see jobs that pay less than INR 28,00,000 a year.")).toBeInTheDocument();
    expect(within(floor).getByRole("button", { name: "Edit salary floor" })).toBeInTheDocument();
    expect(within(floor).getByRole("button", { name: "Remove salary floor" })).toBeInTheDocument();
    expect(screen.getAllByText("Not set (not enforced)")).toHaveLength(6);
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("edits a saved constraint with its value filled in, says what the edit does from tomorrow, and logs it by saving a new one", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord([SALARY]);
    renderWithApi(<ConstraintCards mode="record" />, routes);
    await user.click(await screen.findByRole("button", { name: "Edit salary floor" }));

    const amount = screen.getByRole("textbox", { name: "Amount" });
    expect(amount).toHaveValue("2800000");
    expect(screen.getByRole("textbox", { name: "Currency" })).toHaveValue("INR");
    expect(screen.getByText(/From tomorrow.s run, jobs that do not meet this are filtered out\. Past matches are not rewritten\./)).toBeInTheDocument();

    await user.clear(amount);
    await user.type(amount, "3000000");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posted[0]).toMatchObject({ kind: "salary_floor", salary_floor: { amount: 3000000, currency: "INR" } }));
    // the card closes, and shows the new value once the record has been read again
    expect(await screen.findByText("You will never see jobs that pay less than INR 30,00,000 a year.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Amount" })).not.toBeInTheDocument();
  });

  it("cancels an edit without sending anything", async () => {
    const user = userEvent.setup();
    const { routes } = statefulRecord([SALARY]);
    const { calls } = renderWithApi(<ConstraintCards mode="record" />, routes);
    await user.click(await screen.findByRole("button", { name: "Edit salary floor" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Amount" })).not.toBeInTheDocument();
    expect(calls.filter((call) => call.method !== "GET")).toHaveLength(0);
  });

  it("removes a constraint, which then reads not set", async () => {
    const user = userEvent.setup();
    let constraints: unknown[] = [SALARY];
    const { calls } = renderWithApi(<ConstraintCards mode="record" />, {
      "GET /v1/me/record": () => ok(recordView({ constraints })),
      [`DELETE /v1/me/constraints/${SALARY.id}`]: () => {
        constraints = [];
        return ok({ record_version: 5 });
      },
    });
    await user.click(await screen.findByRole("button", { name: "Remove salary floor" }));
    await waitFor(() => expect(screen.getAllByText("Not set (not enforced)")).toHaveLength(7));
    expect(calls.find((call) => call.method === "DELETE")!.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("says what failed when the record cannot be read", async () => {
    renderWithApi(<ConstraintCards mode="record" />, { "GET /v1/me/record": fail(503, "dependency_unavailable") });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your constraints.");
  });
});
