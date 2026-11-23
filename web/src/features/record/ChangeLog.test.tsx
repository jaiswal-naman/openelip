import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { ChangeLog } from "./ChangeLog";

let counter = 0;
/** One row of the change log as GET /v1/me/events answers it. */
function event(over: Record<string, unknown> = {}) {
  counter += 1;
  return {
    id: `0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e${String(counter).padStart(2, "0")}`,
    status: "applied",
    entity: "constraint",
    operation: "update",
    entity_id: "7c2d9e41-5a6b-4c3d-8e9f-0a1b2c3d4e12",
    before: null,
    after: null,
    source: "candidate_edit",
    origin: null,
    proposal_event_id: null,
    reason: null,
    record_version_after: counter,
    created_at: "2026-11-23T05:30:00Z",
    ...over,
  };
}

const SALARY_BEFORE = { id: "c1", kind: "salary_floor", statement: "You will never see jobs that pay less than INR 28,00,000 a year.", source: "candidate_edit" };
const SALARY_AFTER = { id: "c2", kind: "salary_floor", statement: "You will never see jobs that pay less than INR 30,00,000 a year.", source: "candidate_edit" };
const FACT = { id: "f1", kind: "role", status: "unconfirmed", statement: "Senior Backend Engineer, Globex Example Ltd", source: "resume" };

const rowsOf = () => within(screen.getByRole("table", { name: "Change log, newest first" })).getAllByRole("row").slice(1);

describe("the change log", () => {
  it("lists before, after, where it came from and when, newest first", async () => {
    renderWithApi(<ChangeLog />, {
      "GET /v1/me/events": ok([
        event({ before: SALARY_BEFORE, after: SALARY_AFTER, created_at: "2026-11-23T05:30:00Z" }),
        event({ entity: "fact", operation: "confirm", before: FACT, after: { ...FACT, status: "confirmed" }, created_at: "2026-11-22T18:02:00Z" }),
      ]),
    });
    await screen.findByRole("table");
    const [first, second] = rowsOf();
    expect(within(first).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "Changed salary floor",
      SALARY_BEFORE.statement,
      SALARY_AFTER.statement,
      "Edited by you",
    ]);
    expect(first.querySelector("time")).toHaveAttribute("datetime", "2026-11-23T05:30:00Z");
    expect(first.querySelector("time")).toHaveTextContent(/23 Nov( 2026)? 05:30 UTC/);
    // a fact that was only confirmed has the same words before and after, so its status is what changed
    expect(within(second).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Confirmed fact", "To review", "Confirmed", "From your resume"]);
  });

  it("says what was there before an addition and what is there after a removal", async () => {
    renderWithApi(<ChangeLog />, {
      "GET /v1/me/events": ok([
        event({ entity: "preference", operation: "remove", before: { id: "p1", kind: "other", statement: "Small teams", source: "candidate_edit" }, after: null }),
        event({ entity: "fact", operation: "create", before: null, after: { ...FACT, status: "confirmed", source: "candidate_edit", statement: "Led four engineers" } }),
      ]),
    });
    await screen.findByRole("table");
    const [removal, addition] = rowsOf();
    expect(within(removal).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Removed preference", "Small teams", "Removed", "Edited by you"]);
    expect(within(addition).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Added fact", "Did not exist", "Role: Led four engineers", "Edited by you"]);
  });

  it("shows a declined proposal with where it came from, and a change confirmed from chat as such", async () => {
    renderWithApi(<ChangeLog />, {
      "GET /v1/me/events": ok([
        event({ status: "declined", operation: "create", entity: "constraint", after: SALARY_AFTER, source: null, origin: "chat" }),
        event({ source: "confirmed_proposal", before: SALARY_BEFORE, after: SALARY_AFTER }),
      ]),
    });
    await screen.findByRole("table");
    const [declined, confirmed] = rowsOf();
    expect(declined).toHaveTextContent("Declined a proposed change");
    expect(declined).toHaveTextContent("Proposed in chat");
    expect(confirmed).toHaveTextContent("Confirmed from chat");
  });

  it("pages with the cursor from the answer, and appends the next page", async () => {
    const user = userEvent.setup();
    const cursors: (string | null)[] = [];
    renderWithApi(<ChangeLog />, {
      "GET /v1/me/events": (request) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        cursors.push(cursor);
        return cursor === null
          ? ok([event({ before: SALARY_BEFORE, after: SALARY_AFTER })], 200, { next_cursor: "page-two" })
          : ok([event({ entity: "fact", operation: "create", before: null, after: { ...FACT, status: "confirmed", source: "candidate_edit" } })]);
      },
    });
    await screen.findByRole("table");
    expect(rowsOf()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(rowsOf()).toHaveLength(2));
    expect(cursors).toEqual([null, "page-two"]);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument(); // no cursor in the last answer
  });

  it("shows a new row without a reload after the record is written", async () => {
    let log = [event({ before: SALARY_BEFORE, after: SALARY_AFTER })];
    const { queryClient } = renderWithApi(<ChangeLog />, { "GET /v1/me/events": () => ok(log) });
    await screen.findByRole("table");
    expect(rowsOf()).toHaveLength(1);

    log = [event({ entity: "fact", operation: "create", before: null, after: { ...FACT, status: "confirmed", source: "candidate_edit" } }), ...log];
    await act(() => queryClient.invalidateQueries({ queryKey: ["record"] })); // what every write on the record page does
    await waitFor(() => expect(rowsOf()).toHaveLength(2));
    expect(rowsOf()[0]).toHaveTextContent("Added fact");
  });

  it("says why it is empty", async () => {
    renderWithApi(<ChangeLog />, { "GET /v1/me/events": ok([]) });
    expect(await screen.findByText(/Nothing has changed yet/)).toBeInTheDocument();
  });

  it("shows what failed, with a Retry that works", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    renderWithApi(<ChangeLog />, { "GET /v1/me/events": () => (++attempt === 1 ? fail(503, "dependency_unavailable") : ok([event({ before: SALARY_BEFORE, after: SALARY_AFTER })])) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the change log.");
    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});
