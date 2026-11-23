import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import type { DocumentLine, RecordFact } from "./api";
import { FactReview } from "./FactReview";

// An invented resume: Asha Example, at made-up employers
const DOCUMENT = "9d3c6a52-0e1f-4b7d-8a2c-5f4e3d2c1b0a";
const TEXTS = [
  "INVENTED SAMPLE: not a real person",
  "Asha Example",
  "Experience",
  "Senior Backend Engineer, Globex Example Ltd, Mar 2021 - Present",
  "Built and ran payment services in Python.",
  "Backend Engineer, Initech Sample Co, Jul 2018 - Feb 2021",
  "Skills: Python, PostgreSQL",
];
const LINES: DocumentLine[] = (() => {
  let start = 0;
  return TEXTS.map((text, index) => {
    const line = { n: index + 1, start, end: start + text.length, text };
    start += text.length + 1;
    return line;
  });
})();
const ids = (n: number) => `0f9b6c2a-3d41-4e8a-b5c7-1a2b3c4d5e0${n}`;

/** A fact read from line `from` (to line `to`) of the invented resume above. */
function fact(n: number, from: number, to: number, over: Partial<RecordFact> = {}): RecordFact {
  const start = LINES[from - 1].start;
  const end = LINES[to - 1].end;
  const quote = TEXTS.slice(from - 1, to).join("\n");
  return {
    id: ids(n),
    kind: "role",
    status: "unconfirmed",
    statement: quote,
    attributes: {},
    source: "resume",
    parent_fact_id: null,
    provenance: { document_id: DOCUMENT, span: { start, end }, quote },
    created_at: "2026-11-23T05:00:00Z",
    ...over,
  };
}

const ROLE = fact(1, 4, 4);
const ACHIEVEMENT = fact(2, 5, 5, { kind: "achievement", parent_fact_id: ids(1) });
const SKILL = fact(3, 7, 7, { kind: "skill", statement: "Python" });
const resume = { id: DOCUMENT, lines: LINES };
const reviewed = (decisions: unknown[]) => ({ record_version: 7, results: decisions, unconfirmed_remaining: 2 });
const bodyOf = (request: Request) => request.clone().json();

describe("the fact review", () => {
  it("shows each fact with the words of the resume it came from, and the line", async () => {
    renderWithApi(<FactReview facts={[ROLE, fact(4, 4, 5, { kind: "achievement" })]} document={resume} />, {});
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Senior Backend Engineer, Globex Example Ltd, Mar 2021 - Present");
    expect(within(rows[0]).getByText("From your resume · L4")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Senior Backend Engineer, Globex Example Ltd, Mar 2021 - Present", { selector: "blockquote" })).toBeInTheDocument();
    expect(within(rows[1]).getByText("From your resume · L4-L5")).toBeInTheDocument(); // the cited text runs over two lines
  });

  it("says where a fact came from without a line when the resume's lines are not known", () => {
    renderWithApi(<FactReview facts={[ROLE]} />, {});
    expect(screen.getByText("From your resume")).toBeInTheDocument();
  });

  it("labels a fact the candidate wrote, with no resume words to quote", () => {
    const own = fact(5, 4, 4, { source: "candidate_edit", provenance: null, status: "confirmed", statement: "Led four engineers" });
    const { container } = renderWithApi(<FactReview facts={[own]} document={resume} />, {});
    expect(screen.getByText("Edited by you")).toBeInTheDocument();
    expect(container.querySelector("blockquote")).toBeNull();
  });

  it("shows resume text as text, never as markup", () => {
    const hostile = fact(6, 4, 4, { provenance: { document_id: DOCUMENT, span: { start: 0, end: 5 }, quote: '<img src=x onerror="alert(1)"> <b>bold</b>' } });
    const { container } = renderWithApi(<FactReview facts={[hostile]} document={resume} />, {});
    expect(screen.getByText('<img src=x onerror="alert(1)"> <b>bold</b>', { selector: "blockquote" })).toBeInTheDocument();
    expect(container.querySelector("img, b")).toBeNull();
  });

  it("confirms one fact with one write, carrying an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<FactReview facts={[ROLE, ACHIEVEMENT]} document={resume} />, {
      "POST /v1/me/facts/review": ok(reviewed([{ fact_id: ids(1), status: "confirmed" }])),
    });
    await user.click(screen.getByRole("button", { name: `Confirm: ${ROLE.statement}` }));

    await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(1));
    const post = calls.find((call) => call.method === "POST")!;
    expect(await bodyOf(post)).toEqual({ decisions: [{ fact_id: ids(1), action: "confirm" }] });
    expect(post.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByText("Saved. 2 to review.", { selector: "[role=status]" })).toBeInTheDocument();
  });

  it("rejects one fact with one write", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<FactReview facts={[ROLE]} document={resume} />, {
      "POST /v1/me/facts/review": ok(reviewed([{ fact_id: ids(1), status: "rejected" }])),
    });
    await user.click(screen.getByRole("button", { name: `Reject: ${ROLE.statement}` }));
    await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(1));
    expect(await bodyOf(calls.find((call) => call.method === "POST")!)).toEqual({ decisions: [{ fact_id: ids(1), action: "reject" }] });
  });

  it("fixes a fact in an editor with the resume's words still beside it, and saves the new words as a correction", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<FactReview facts={[ROLE]} document={resume} />, {
      "POST /v1/me/facts/review": ok(reviewed([{ fact_id: ids(1), status: "superseded" }])),
    });
    await user.click(screen.getByRole("button", { name: `Fix: ${ROLE.statement}` }));

    const field = screen.getByRole("textbox", { name: "Your wording" });
    expect(field).toHaveValue(ROLE.statement);
    expect(screen.getByText(ROLE.statement, { selector: "blockquote" })).toBeInTheDocument(); // the source stays beside the editor
    expect(screen.queryByRole("button", { name: /^Confirm:/ })).not.toBeInTheDocument();

    await user.clear(field);
    await user.type(field, "Senior Backend Engineer, Globex Example Ltd, March 2021 to now");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(1));
    expect(await bodyOf(calls.find((call) => call.method === "POST")!)).toEqual({
      decisions: [{ fact_id: ids(1), action: "correct", statement: "Senior Backend Engineer, Globex Example Ltd, March 2021 to now" }],
    });
  });

  it("treats saving the same words as a confirmation, and Cancel as nothing at all", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<FactReview facts={[ROLE]} document={resume} />, {
      "POST /v1/me/facts/review": ok(reviewed([{ fact_id: ids(1), status: "confirmed" }])),
    });
    await user.click(screen.getByRole("button", { name: `Fix: ${ROLE.statement}` }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Your wording" })).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: `Fix: ${ROLE.statement}` }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(await bodyOf(calls[0])).toEqual({ decisions: [{ fact_id: ids(1), action: "confirm" }] });
  });

  it("refuses an empty fix without sending anything", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<FactReview facts={[ROLE]} document={resume} />, {});
    await user.click(screen.getByRole("button", { name: `Fix: ${ROLE.statement}` }));
    await user.clear(screen.getByRole("textbox", { name: "Your wording" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Write something, or cancel.")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("confirms everything remaining in one write, leaving what could not be confirmed to be decided one by one", async () => {
    const user = userEvent.setup();
    const flagged = fact(7, 6, 6, { kind: "other", attributes: { unverified: true, reason: "employer_not_in_text" } });
    const { calls } = renderWithApi(<FactReview facts={[ROLE, ACHIEVEMENT, SKILL, flagged]} document={resume} />, {
      "POST /v1/me/facts/review": ok(reviewed([])),
    });
    expect(screen.getByRole("heading", { name: "We could not confirm these" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm all remaining" }));

    await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(1));
    expect(await bodyOf(calls.find((call) => call.method === "POST")!)).toEqual({
      decisions: [
        { fact_id: ids(1), action: "confirm" },
        { fact_id: ids(2), action: "confirm" },
        { fact_id: ids(3), action: "confirm" },
      ],
    });
  });

  it("tells how many are still to review and that they are ignored until confirmed", () => {
    renderWithApi(<FactReview facts={[ROLE, ACHIEVEMENT, fact(5, 5, 5, { status: "confirmed" })]} document={resume} />, {});
    expect(screen.getByText("2 to review. They are ignored until you confirm them.")).toBeInTheDocument();
  });

  it("says nothing is left once every fact is decided", () => {
    renderWithApi(<FactReview facts={[fact(5, 5, 5, { status: "confirmed" })]} document={resume} />, {});
    expect(screen.getByText("Nothing left to review.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm all remaining" })).not.toBeInTheDocument();
  });

  it("does not show rejected or replaced facts as active, and does not count them", () => {
    const rejected = fact(8, 4, 4, { status: "rejected", statement: "Rejected statement" });
    const replaced = fact(9, 4, 4, { status: "superseded", statement: "Replaced statement" });
    renderWithApi(<FactReview facts={[ROLE, rejected, replaced]} document={resume} />, {});
    expect(screen.queryByText("Rejected statement")).not.toBeInTheDocument();
    expect(screen.queryByText("Replaced statement")).not.toBeInTheDocument();
    expect(screen.getByText("1 to review. They are ignored until you confirm them.")).toBeInTheDocument();
  });

  it("lists a confirmed fact as confirmed, with no buttons to decide it again", () => {
    renderWithApi(<FactReview facts={[fact(5, 5, 5, { status: "confirmed" })]} document={resume} />, {});
    const row = screen.getByRole("listitem");
    expect(within(row).getByText("Confirmed", { selector: "span" })).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });

  it("says when nothing was read from the resume", () => {
    renderWithApi(<FactReview facts={[]} />, {});
    expect(screen.getByText(/No facts were read from your resume/)).toBeInTheDocument();
  });

  it("says why a decision failed and shows the latest after a conflict", async () => {
    const user = userEvent.setup();
    renderWithApi(<FactReview facts={[ROLE]} document={resume} />, {
      "POST /v1/me/facts/review": fail(409, "conflict", { state: "confirmed" }),
    });
    await user.click(screen.getByRole("button", { name: `Confirm: ${ROLE.statement}` }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This changed in another tab or by an agent. Showing the latest.");
  });
});
