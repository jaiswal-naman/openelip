import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { JobsView } from "./JobsView";
import type { SearchParams } from "./jobs";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const NO_SEARCH: SearchParams = { q: "", country: null, remote: null };

// Invented rows in the shape /v1/jobs returns (Example Co and example.test are made up)
const row = (id: number, overrides: Record<string, unknown> = {}) => ({
  id,
  title: `Role ${id}`,
  company_name: "Example Co",
  location_raw: "Pune, India",
  country: "IN",
  city: "Pune",
  is_remote: false,
  apply_url: `https://jobs.example.test/${id}`,
  posted_at: null,
  last_verified_at: "2026-11-22T12:21:54Z",
  content_version: 1,
  ...overrides,
});

const rows = (count: number) => Array.from({ length: count }, (_, index) => row(index + 1));
const stats = ok({ open_postings: 11353, open_unique_jobs: 9996 });

/** /v1/jobs answers with as many rows as it was asked for, up to what the fake index holds. */
const jobsFrom = (all: unknown[]) => (request: Request) => {
  const limit = Number(new URL(request.url).searchParams.get("limit"));
  return ok(all.slice(0, limit), 200, { count: Math.min(limit, all.length), limit });
};

const lastQuery = (calls: Request[]) => new URL(calls.filter((call) => call.url.includes("/v1/jobs")).at(-1)!.url).searchParams;

beforeEach(() => push.mockReset());

describe("results", () => {
  it("show title, company, location, remote state, last verified time and a link out", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom([row(1)]), "GET /v1/stats": stats });

    const first = (await screen.findByRole("heading", { level: 2, name: "Role 1" })).closest("li")!;
    expect(within(first).getByText("Example Co")).toBeInTheDocument();
    expect(within(first).getByText("Pune, India")).toBeInTheDocument();
    expect(within(first).getByText("Not remote")).toBeInTheDocument(); // the row, not the filter of the same name
    expect(within(first).getByText(/Verified open/)).toHaveTextContent("Verified open 22 Nov 12:21 UTC");
    expect(within(first).getByText("22 Nov 12:21 UTC").closest("time")).toHaveAttribute("datetime", "2026-11-22T12:21:54Z");
  });

  it("link out to the posting safely, with a name that says where it goes", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom([row(1)]), "GET /v1/stats": stats });
    const link = await screen.findByRole("link", { name: "View posting for Role 1 (opens in a new tab)" });
    expect(link).toHaveAttribute("href", "https://jobs.example.test/1");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("do not make a link out of a scheme that could run script", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": jobsFrom([row(1, { apply_url: "javascript:alert(1)" })]),
      "GET /v1/stats": stats,
    });
    expect(await screen.findByText("No link in the posting")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View posting/ })).not.toBeInTheDocument();
  });

  it("say Location not stated only when nothing at all is known, and never guess a country", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": jobsFrom([
        row(1, { location_raw: null, country: null, city: null }),
        row(2, { title: "Role Two", location_raw: "Stockholm", country: null, city: null, is_remote: null }),
      ]),
      "GET /v1/stats": stats,
    });
    expect(await screen.findByText("Location not stated")).toBeInTheDocument();
    const second = screen.getByRole("heading", { name: "Role Two" }).closest("li")!;
    expect(within(second).getByText("Stockholm")).toBeInTheDocument();
    expect(within(second).queryByText(/Sweden/)).not.toBeInTheDocument();
    expect(within(second).getByText("Remote: not stated")).toBeInTheDocument();
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

  it("send no filter that was not chosen", async () => {
    const { calls } = renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(1)), "GET /v1/stats": stats });
    await screen.findByRole("heading", { name: "Role 1" });
    expect([...lastQuery(calls).keys()]).toEqual(["limit"]);
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

describe("Show more", () => {
  it("raises the limit in steps up to the 100 the API allows, keeping the rows on screen meanwhile", async () => {
    const user = userEvent.setup();
    const index = rows(150);
    const { calls } = renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(index), "GET /v1/stats": stats });

    expect(await screen.findByText("Showing 20 jobs")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Showing 50 jobs")).toBeInTheDocument();
    expect(lastQuery(calls).get("limit")).toBe("50");

    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Showing 100 jobs")).toBeInTheDocument();
    expect(lastQuery(calls).get("limit")).toBe("100");

    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    expect(screen.getByText("Showing the first 100. Narrow the search to see others.")).toBeInTheDocument();
  });

  it("is not offered when the list is shorter than a page", async () => {
    renderWithApi(<JobsView params={NO_SEARCH} />, { "GET /v1/jobs": jobsFrom(rows(5)), "GET /v1/stats": stats });
    await screen.findByText("Showing 5 jobs");
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("keeps the rows already shown while a longer list loads", async () => {
    const user = userEvent.setup();
    let release: (response: Response) => void = () => {};
    const { queryClient } = renderWithApi(<JobsView params={NO_SEARCH} />, {
      "GET /v1/jobs": (request) =>
        new URL(request.url).searchParams.get("limit") === "20"
          ? jobsFrom(rows(30))(request)
          : new Promise<Response>((resolve) => (release = resolve)),
      "GET /v1/stats": stats,
    });
    await screen.findByText("Showing 20 jobs");
    await user.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(() => expect(screen.getByRole("region", { name: "Results" })).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByRole("heading", { name: "Role 1" })).toBeInTheDocument();

    release(jobsFrom(rows(30))(new Request("http://localhost/v1/jobs?limit=50")));
    expect(await screen.findByText("Showing 30 jobs")).toBeInTheDocument();
    expect(queryClient.isFetching()).toBe(0);
  });
});
