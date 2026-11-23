import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { JobsView } from "./JobsView";
import type { SearchParams } from "./jobs";
import { job } from "./testing";
import type { Job } from "./types";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const NO_SEARCH: SearchParams = { q: "", country: null, remote: null };

// Invented jobs in the shape /v1/jobs returns (Example Co and example.test are made up)
const row = (id: number, overrides: Partial<Job> = {}) =>
  job({ id, title: `Role ${id}`, apply_url: `https://jobs.example.test/${id}`, ...overrides });
const rows = (count: number) => Array.from({ length: count }, (_, index) => row(index + 1));
const stats = ok({ open_postings: 11353, open_unique_jobs: 9996 });

/**
 * /v1/jobs over a fake index: a page of `limit` rows, and `next_cursor` while rows remain. The fake cursor is the offset
 * into the list ("c20"); the page treats a cursor as opaque, as it must treat the real one. `mode: null` sends none.
 */
const jobsFrom =
  (all: Job[], mode: string | null = "hybrid") =>
  (request: Request) => {
    const query = new URL(request.url).searchParams;
    const limit = Number(query.get("limit"));
    const offset = Number(query.get("cursor")?.slice(1) ?? 0);
    const page = all.slice(offset, offset + limit);
    const next = offset + limit < all.length ? `c${offset + limit}` : null;
    return ok(page, 200, { count: page.length, limit, next_cursor: next, ...(mode === null ? {} : { mode }) });
  };

const jobCalls = (calls: Request[]) => calls.filter((call) => new URL(call.url).pathname === "/v1/jobs");
const lastQuery = (calls: Request[]) => new URL(jobCalls(calls).at(-1)!.url).searchParams;
const titles = () => screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);

beforeEach(() => push.mockReset());

describe("results", () => {
  it("show each job as a card: title linked to its page, company, place, source, last verified time and a link out", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom([row(1)]), "GET /v1/stats": stats });

    const first = (await screen.findByRole("heading", { level: 2, name: "Role 1" })).closest("li")!;
    expect(within(first).getByRole("link", { name: "Role 1" })).toHaveAttribute("href", "/jobs/1");
    expect(within(first).getByText("Example Co")).toBeInTheDocument();
    expect(within(first).getByText("Pune, India")).toBeInTheDocument();
    expect(within(first).getByText("Hybrid")).toBeInTheDocument();
    expect(within(first).getByText("Source: Greenhouse")).toBeInTheDocument();
    expect(within(first).getByText(/Verified open/)).toHaveTextContent("Verified open 22 Nov 12:21 UTC");
    expect(within(first).getByText("22 Nov 12:21 UTC").closest("time")).toHaveAttribute("datetime", "2026-11-22T12:21:54Z");
    expect(within(first).getByRole("link", { name: "View posting for Role 1 (opens in a new tab)" })).toHaveAttribute(
      "href",
      "https://jobs.example.test/1",
    );
  });

  it("say Location: unknown and Remote: not stated when the API could not tell, and never guess", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": jobsFrom([row(1, { location_raw: null, country: null, city: null, work_arrangements: [], is_remote: null })]),
      "GET /v1/stats": stats,
    });
    expect(await screen.findByText("Location: unknown")).toBeInTheDocument();
    expect(screen.getByText("Remote: not stated")).toBeInTheDocument();
  });

  it("read the search from the URL and send exactly that to the API", async () => {
    const { calls } = renderWithApi(<JobsView params={{ q: "backend python", country: "IN", remote: true }} />, {
      "GET /v1/jobs": jobsFrom(rows(2)),
      "GET /v1/stats": stats,
    });
    await screen.findByRole("heading", { name: "Role 1" });
    const query = lastQuery(calls);
    expect(query.get("q")).toBe("backend python");
    expect(query.get("country")).toBe("IN");
    expect(query.get("remote")).toBe("true");
    expect(query.get("limit")).toBe("20");
  });

  it("send no filter that was not chosen, and no cursor for the first page", async () => {
    const { calls } = renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(1)), "GET /v1/stats": stats });
    await screen.findByRole("heading", { name: "Role 1" });
    expect([...lastQuery(calls).keys()]).toEqual(["limit"]);
  });
});

describe("the keyword-only note", () => {
  it("is shown when the API says the results are keyword-only", async () => {
    renderWithApi(<JobsView params={{ q: "dentist", country: null, remote: null }} />, {
      "GET /v1/jobs": jobsFrom(rows(2), "keyword"),
      "GET /v1/stats": stats,
    });
    expect(await screen.findByText("Keyword results only right now.")).toBeInTheDocument();
  });

  it("is not shown when meaning was searched too", async () => {
    renderWithApi(<JobsView params={{ q: "dentist", country: null, remote: null }} />, {
      "GET /v1/jobs": jobsFrom(rows(2), "hybrid"),
      "GET /v1/stats": stats,
    });
    await screen.findByRole("heading", { name: "Role 1" });
    expect(screen.queryByText("Keyword results only right now.")).not.toBeInTheDocument();
  });

  it("is not shown when the answer does not say, because the page claims only what it was told", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(2), null), "GET /v1/stats": stats });
    await screen.findByRole("heading", { name: "Role 1" });
    expect(screen.queryByText("Keyword results only right now.")).not.toBeInTheDocument();
  });
});

describe("the count line", () => {
  it("shows how many jobs were shown and how many the index holds", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(3)), "GET /v1/stats": stats });
    expect(await screen.findByText("Showing 3 jobs")).toBeInTheDocument();
    expect(await screen.findByText(/open jobs in the index/)).toHaveTextContent("9,996 open jobs in the index");
  });

  it("says the index size is unavailable when stats fail, and still shows the jobs", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(1)), "GET /v1/stats": fail(503, "dependency_unavailable") });
    expect(await screen.findByText("Index size not available")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Role 1" })).toBeInTheDocument();
  });
});

describe("the search form", () => {
  it("has a labelled keyword box, a country select and a three-state remote filter", async () => {
    renderWithApi(<JobsView params={{ q: "go", country: "IN", remote: false }} />, { "GET /v1/jobs": jobsFrom([]), "GET /v1/stats": stats });
    expect(screen.getByRole("searchbox", { name: "Search open jobs" })).toHaveValue("go");
    expect(screen.getByRole("combobox", { name: "Country" })).toHaveValue("IN");
    const remote = screen.getByRole("radiogroup", { name: "Remote" });
    expect(within(remote).getAllByRole("radio").map((radio) => radio.closest("label")?.textContent)).toEqual(["Any", "Remote only", "Not remote"]);
    expect(within(remote).getByRole("radio", { name: "Not remote" })).toBeChecked();
    await screen.findByText(/No open jobs match/);
  });

  it("puts the search in the URL when it is submitted, and leaves out what is not set", async () => {
    const user = userEvent.setup();
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(1)), "GET /v1/stats": stats });
    await screen.findByRole("heading", { name: "Role 1" });

    await user.type(screen.getByRole("searchbox", { name: "Search open jobs" }), "backend python");
    await user.selectOptions(screen.getByRole("combobox", { name: "Country" }), "IN");
    await user.click(screen.getByRole("radio", { name: "Remote only" }));
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(push).toHaveBeenLastCalledWith("/jobs?q=backend+python&country=IN&remote=true");

    await user.clear(screen.getByRole("searchbox", { name: "Search open jobs" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Country" }), "");
    await user.click(screen.getByRole("radio", { name: "Any" }));
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(push).toHaveBeenLastCalledWith("/jobs");
  });

  it("submits with the Enter key from the keyword box", async () => {
    const user = userEvent.setup();
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom([]), "GET /v1/stats": stats });
    await user.type(screen.getByRole("searchbox", { name: "Search open jobs" }), "rust{Enter}");
    expect(push).toHaveBeenCalledWith("/jobs?q=rust");
  });
});

describe("empty, loading and error states", () => {
  it("explains an empty search and offers the one action that changes it", async () => {
    renderWithApi(<JobsView params={{ q: "zzzz", country: null, remote: null }} />, { "GET /v1/jobs": jobsFrom([]), "GET /v1/stats": stats });
    expect(await screen.findByText(/No open jobs match this search/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Clear the search" })).toHaveAttribute("href", "/jobs");
  });

  it("says an empty index is empty, without a clear-the-search action", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom([]), "GET /v1/stats": ok({ open_unique_jobs: 0 }) });
    expect(await screen.findByText(/The job index has no open jobs yet/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Clear the search" })).not.toBeInTheDocument();
  });

  it("shows a loading state shaped like the results while the first page loads", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": () => new Promise<Response>(() => {}), "GET /v1/stats": stats });
    expect(screen.getByRole("status")).toHaveTextContent("Loading jobs");
    expect(document.querySelectorAll("li")).toHaveLength(3);
  });

  it("shows what failed and a Retry that works, and the rest of the page keeps working", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": (request) => (++attempt === 1 ? fail(503, "dependency_unavailable") : jobsFrom(rows(1))(request)),
      "GET /v1/stats": stats,
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load jobs.");
    expect(alert).toHaveTextContent("A service this needs is unavailable. Try again shortly.");
    expect(screen.getByRole("searchbox", { name: "Search open jobs" })).toBeEnabled();

    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Role 1" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("names a network failure plainly", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": () => Promise.reject(new TypeError("Failed to fetch")), "GET /v1/stats": stats });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
  });
});

describe("Load more", () => {
  it("asks for the next page with the API's cursor and the same search, and adds its rows under the first", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<JobsView params={{ q: "backend python", country: "IN", remote: true }} />, {
      "GET /v1/jobs": jobsFrom(rows(45)),
      "GET /v1/stats": stats,
    });
    expect(await screen.findByText("Showing 20 jobs")).toBeInTheDocument();
    expect(lastQuery(calls).has("cursor")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Showing 40 jobs")).toBeInTheDocument();
    const second = lastQuery(calls);
    expect(second.get("cursor")).toBe("c20");
    expect(second.get("q")).toBe("backend python");
    expect(second.get("country")).toBe("IN");
    expect(second.get("remote")).toBe("true");
    expect(second.get("limit")).toBe("20");
    expect(titles()).toEqual(rows(40).map((r) => r.title)); // the first page stays, in order, with the new rows after it

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Showing 45 jobs")).toBeInTheDocument();
    expect(lastQuery(calls).get("cursor")).toBe("c40");
  });

  it("hides itself when the API sends no further cursor", async () => {
    const user = userEvent.setup();
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(25)), "GET /v1/stats": stats });
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Showing 25 jobs");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("is not offered when the first page is all there is", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(5)), "GET /v1/stats": stats });
    await screen.findByText("Showing 5 jobs");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("never writes the cursor into the address: the URL keeps the query and filters only", async () => {
    const user = userEvent.setup();
    renderWithApi(<JobsView params={{ q: "backend", country: null, remote: null }} />, { "GET /v1/jobs": jobsFrom(rows(30)), "GET /v1/stats": stats });
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Showing 30 jobs");
    expect(push).not.toHaveBeenCalled();
    expect(window.location.href).not.toMatch(/cursor|c20/);
  });

  it("keeps the rows already shown while the next page loads", async () => {
    const user = userEvent.setup();
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": (request) =>
        new URL(request.url).searchParams.has("cursor") ? new Promise<Response>(() => {}) : jobsFrom(rows(30))(request),
      "GET /v1/stats": stats,
    });
    await user.click(await screen.findByRole("button", { name: "Load more" }));

    await waitFor(() => expect(screen.getByRole("region", { name: "Results" })).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByRole("button", { name: "Load more" })).toHaveAttribute("aria-disabled", "true");
    expect(titles()).toHaveLength(20);
  });

  it("keeps the rows and says why when a page fails, and Retry brings it in", async () => {
    const user = userEvent.setup();
    let cursorCalls = 0;
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": (request) =>
        new URL(request.url).searchParams.has("cursor") && ++cursorCalls === 1
          ? fail(503, "dependency_unavailable")
          : jobsFrom(rows(30))(request),
      "GET /v1/stats": stats,
    });
    await user.click(await screen.findByRole("button", { name: "Load more" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load more jobs.");
    expect(alert).toHaveTextContent("A service this needs is unavailable. Try again shortly.");
    expect(titles()).toHaveLength(20); // nothing already loaded is lost

    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Showing 30 jobs")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
