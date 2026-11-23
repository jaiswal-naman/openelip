import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { bodyOf, recordView } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { PreferenceInput } from "./PreferenceInput";

function preference(over: Record<string, unknown> = {}) {
  return {
    id: "3b1e5c77-9d02-4f6a-8a14-6c0d2e8f7a11",
    kind: "other",
    value: "small teams",
    weight: 2,
    statement: "Small teams",
    source: "candidate_edit",
    created_at: "2026-11-23T05:00:00Z",
    notice: null,
    ...over,
  };
}

/** A record that keeps the preferences saved to it. */
function statefulRecord(answer: (body: Record<string, unknown>) => Record<string, unknown> = () => preference()) {
  let preferences: Record<string, unknown>[] = [];
  const posted: Record<string, unknown>[] = [];
  return {
    posted,
    routes: {
      "GET /v1/me/record": () => ok(recordView({ preferences })),
      "POST /v1/me/preferences": async (request: Request) => {
        const body = (await bodyOf(request)) as Record<string, unknown>;
        posted.push(body);
        const saved = answer(body);
        preferences = [...preferences, { ...saved, suggested_constraint: undefined }];
        return ok({ preference: saved, record_version: 5 }, 201);
      },
    },
  };
}

const field = () => screen.getByRole("textbox", { name: "A preference, or something you will not accept" });

describe("soft preferences", () => {
  it("saves free text with how much it matters, empties the field and lists it as a chip", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    const { calls } = renderWithApi(<PreferenceInput />, routes);
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "Small teams");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(posted).toEqual([{ kind: "other", statement: "Small teams", weight: 2 }]));
    expect(calls.find((call) => call.method === "POST")!.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    const list = await screen.findByRole("list", { name: "Your preferences" });
    expect(within(list).getByText("Small teams")).toBeInTheDocument();
    expect(within(list).getByText("Would like")).toBeInTheDocument();
    expect(field()).toHaveValue("");
  });

  it("saves something to avoid with a negative weight", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord();
    renderWithApi(<PreferenceInput />, routes);
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "Long commutes");
    await user.click(screen.getByRole("radio", { name: "I would rather avoid this" }));
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(posted).toEqual([{ kind: "other", statement: "Long commutes", weight: -2 }]));
  });

  it("saves a dealbreaker as one, and says it is a preference and not a guarantee", async () => {
    const user = userEvent.setup();
    const { routes, posted } = statefulRecord((body) =>
      preference({ kind: "untyped_dealbreaker", value: "no heavy travel", weight: -3, statement: body.statement, notice: "not_guaranteed", suggested_constraint: null }),
    );
    renderWithApi(<PreferenceInput />, routes);
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "No heavy travel");
    await user.click(screen.getByRole("radio", { name: "This is a dealbreaker" }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(posted).toEqual([{ kind: "untyped_dealbreaker", statement: "No heavy travel" }]));
    const list = await screen.findByRole("list", { name: "Your preferences" });
    expect(within(list).getByText("Dealbreaker")).toBeInTheDocument();
    expect(within(list).getByText("We cannot check this in job data, so it is a preference, not a guarantee.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make this a hard constraint" })).not.toBeInTheDocument();
  });

  it("offers to make a dealbreaker a hard constraint when it fits one, through the same editor and Confirm", async () => {
    const user = userEvent.setup();
    const suggested = {
      kind: "remote_policy",
      statement: "No remote work",
      arrangements: ["onsite", "hybrid"],
      country_codes: null,
      cities: null,
      employment_types: null,
      seniorities: null,
      salary_floor: null,
      company_names: null,
    };
    const { routes } = statefulRecord((body) =>
      preference({ kind: "untyped_dealbreaker", weight: -3, statement: body.statement, notice: "not_guaranteed", suggested_constraint: suggested }),
    );
    const constraints: unknown[] = [];
    renderWithApi(<PreferenceInput />, {
      ...routes,
      "POST /v1/me/constraints": async (request) => {
        constraints.push(await bodyOf(request));
        return ok({ constraint: { id: "c1", ...suggested }, record_version: 6 }, 201);
      },
    });
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "No remote work");
    await user.click(screen.getByRole("radio", { name: "This is a dealbreaker" }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    await user.click(await screen.findByRole("button", { name: "Make this a hard constraint" }));
    expect(screen.getByText(/You wrote: .No remote work./)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "On-site" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Hybrid" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Remote" })).not.toBeChecked();
    expect(screen.getByText("You will only see on-site or hybrid jobs.")).toBeInTheDocument();
    expect(constraints).toHaveLength(0); // nothing becomes a rule until Confirm

    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(constraints).toEqual([{ kind: "remote_policy", statement: "You will only see on-site or hybrid jobs.", arrangements: ["onsite", "hybrid"] }]));
    expect(await screen.findByText(/Saved as a hard constraint\. It is now enforced/)).toBeInTheDocument();
  });

  it("does not keep a protected characteristic: shows the notice, empties the field, saves nothing and sends nothing more", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<PreferenceInput />, {
      "GET /v1/me/record": ok(recordView()),
      "POST /v1/me/preferences": fail(422, "validation_failed", { fields: [{ field: "statement", issue: "protected_characteristic" }] }),
    });
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "Religion: Jainism");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("We do not keep this in your record.");
    expect(field()).toHaveValue("");
    expect(screen.queryByRole("list", { name: "Your preferences" })).not.toBeInTheDocument();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(1); // no second request, as a preference or as anything else
    expect(calls.filter((call) => call.method !== "GET" && call.method !== "POST")).toHaveLength(0);
  });

  it("asks for words before sending anything", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<PreferenceInput />, { "GET /v1/me/record": ok(recordView()) });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    expect(screen.getByText("Write what you prefer, or what you will not accept.")).toBeInTheDocument();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);
  });

  it("edits the words of a saved preference in place, keeping its meaning words in step", async () => {
    const user = userEvent.setup();
    let saved = preference();
    const patches: unknown[] = [];
    renderWithApi(<PreferenceInput />, {
      "GET /v1/me/record": () => ok(recordView({ preferences: [saved] })),
      [`PATCH /v1/me/preferences/${saved.id}`]: async (request) => {
        const body = (await bodyOf(request)) as { statement: string };
        patches.push(body);
        saved = preference({ statement: body.statement });
        return ok({ preference: saved, record_version: 6 });
      },
    });
    await user.click(await screen.findByRole("button", { name: "Edit: Small teams" }));
    const box = screen.getByRole("textbox", { name: "Your wording" });
    expect(box).toHaveValue("Small teams");
    await user.clear(box);
    await user.type(box, "Small  Product teams");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches).toEqual([{ statement: "Small  Product teams", value: "small product teams" }]));
    expect(await screen.findByText("Small Product teams")).toBeInTheDocument(); // the page collapses the doubled space when it matches text
  });

  it("removes a preference", async () => {
    const user = userEvent.setup();
    let saved: unknown[] = [preference()];
    const { calls } = renderWithApi(<PreferenceInput />, {
      "GET /v1/me/record": () => ok(recordView({ preferences: saved })),
      [`DELETE /v1/me/preferences/${preference().id}`]: () => {
        saved = [];
        return ok({ record_version: 7 });
      },
    });
    await user.click(await screen.findByRole("button", { name: "Remove: Small teams" }));
    await waitFor(() => expect(screen.queryByRole("list", { name: "Your preferences" })).not.toBeInTheDocument());
    expect(calls.some((call) => call.method === "DELETE")).toBe(true);
  });

  it("says what failed when a preference cannot be saved", async () => {
    const user = userEvent.setup();
    renderWithApi(<PreferenceInput />, { "GET /v1/me/record": ok(recordView()), "POST /v1/me/preferences": fail(500, "internal_error") });
    await user.type(await screen.findByRole("textbox", { name: "A preference, or something you will not accept" }), "Small teams");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on the server. Nothing was sent.");
    expect(field()).toHaveValue("Small teams"); // what the person wrote is kept
  });
});
