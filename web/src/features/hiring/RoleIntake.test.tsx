import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { DESCRIPTION, me, preview, QUOTE, QUOTE_SPAN, role, ROLE } from "./testing";
import { NewRole, RoleIntake } from "./RoleIntake";

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/hire/roles",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: nav.push, refresh: vi.fn(), replace: vi.fn() }),
}));

const GET_ROLE = `GET /v1/roles/${ROLE}`;
beforeEach(() => {
  nav.push.mockReset();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
  Element.prototype.scrollIntoView = () => {};
});

describe("New role", () => {
  it("saves a draft and opens it", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<NewRole />, { "GET /v1/me": ok(me()), "POST /v1/roles": ok(role(), 201) });
    await user.type(await screen.findByRole("textbox", { name: /Role title/ }), "Senior Backend Engineer");
    await user.type(screen.getByRole("textbox", { name: "Describe the role" }), "Run Python in production.");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/hire/roles/${ROLE}`));
    const body = (await calls.find((c) => c.method === "POST")!.json()) as Record<string, unknown>;
    expect(body.title).toBe("Senior Backend Engineer");
    expect(body.org_id).toBeTruthy();
  });
});

describe("a draft role", () => {
  it("shows each requirement with its importance and confirmation, and highlights the sentence it came from (code points)", async () => {
    const user = userEvent.setup();
    // an emoji before the span: UTF-16 slicing would cut the wrong words
    const withEmoji = "🚀 " + DESCRIPTION;
    const shifted = role({ description: withEmoji, requirements: [{ ...role().requirements[0]!, span: { start: QUOTE_SPAN.start + 2, end: QUOTE_SPAN.end + 2 } }] });
    renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(shifted) });
    expect(await screen.findByText("Python in production")).toBeInTheDocument();
    expect(screen.getByText(/Required, not confirmed yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show source of: Python in production" }));
    const source = screen.getByLabelText("Source sentence");
    expect(source.querySelector("mark")?.textContent).toBe(QUOTE);
  });

  it("Extract saves the description and shows the returned requirements", async () => {
    const user = userEvent.setup();
    const extracted = role({ requirements: [{ ...role().requirements[0]!, id: "n1", text: "PostgreSQL at scale", span: null }], extraction: { state: "extracted", dropped: 0 } });
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(role({ requirements: [] })), [`PATCH /v1/roles/${ROLE}`]: ok(extracted) });
    const box = await screen.findByRole("textbox", { name: "Describe the role" });
    await user.clear(box);
    await user.type(box, "Needs PostgreSQL at scale.");
    await user.click(screen.getByRole("button", { name: "Extract" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(await calls.find((c) => c.method === "PATCH")!.json()).toEqual({ description: "Needs PostgreSQL at scale." });
  });

  it("with AI off says extraction is unavailable and offers Add requirement; adding sends the whole list with PUT", async () => {
    const user = userEvent.setup();
    let saved = role({ requirements: [], extraction: { state: "ai_disabled", dropped: 0 } });
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, {
      [GET_ROLE]: () => ok(saved),
      [`PATCH /v1/roles/${ROLE}`]: () => ok(saved),
      [`PUT /v1/roles/${ROLE}/requirements`]: async (request) => {
        const body = (await request.clone().json()) as { requirements: { text: string }[] };
        saved = role({ requirements: [{ ...role().requirements[0]!, text: body.requirements[0]!.text, is_confirmed: true, span: null }] });
        return ok(saved);
      },
    });
    await user.click(await screen.findByRole("button", { name: "Extract" }));
    expect(await screen.findByText(/Extraction is unavailable/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add requirement" }));
    await user.type(screen.getByRole("textbox", { name: "Requirement" }), "Go services");
    await user.click(screen.getByRole("button", { name: "Save requirement" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = (await calls.find((c) => c.method === "PUT")!.json()) as { requirements: Record<string, unknown>[] };
    expect(put.requirements).toEqual([{ kind: "other", text: "Go services", importance: "required", is_confirmed: true, span: null }]);
    expect(await screen.findByText("Go services")).toBeInTheDocument();
  });

  it("offers Extract again after a failed extraction", async () => {
    const user = userEvent.setup();
    renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(role()), [`PATCH /v1/roles/${ROLE}`]: ok(role({ extraction: { state: "failed", dropped: 0 } })) });
    await user.click(await screen.findByRole("button", { name: "Extract" }));
    expect(await screen.findByText(/Extraction failed. Extract again/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Extract" })).toBeInTheDocument();
  });

  it("Confirm, Edit and Remove each send the whole list; an edit drops the span", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(role()), [`PUT /v1/roles/${ROLE}/requirements`]: ok(role()) });
    await user.click(await screen.findByRole("button", { name: "Confirm: Python in production" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(1));
    let sent = (await calls.filter((c) => c.method === "PUT")[0]!.json()) as { requirements: { text: string; is_confirmed: boolean; span: unknown }[] };
    expect(sent.requirements.map((r) => [r.text, r.is_confirmed])).toEqual([["Python in production", true], ["Kubernetes", true]]);
    expect(sent.requirements[0]!.span).toEqual(QUOTE_SPAN);

    await user.click(screen.getByRole("button", { name: "Edit: Python in production" }));
    const box = screen.getByRole("textbox", { name: "Requirement" });
    await user.clear(box);
    await user.type(box, "Python at scale");
    await user.click(screen.getByRole("button", { name: "Save requirement" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    sent = (await calls.filter((c) => c.method === "PUT")[1]!.json()) as typeof sent;
    expect(sent.requirements[0]).toMatchObject({ text: "Python at scale", span: null, is_confirmed: true });

    await user.click(screen.getByRole("button", { name: "Remove: Kubernetes" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(3));
    sent = (await calls.filter((c) => c.method === "PUT")[2]!.json()) as typeof sent;
    expect(sent.requirements.map((r) => r.text)).toEqual(["Python in production"]);
  });

  it("saves the pay range and location with PATCH, and refuses a minimum above the maximum", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(role()), [`PATCH /v1/roles/${ROLE}`]: ok(role()) });
    await screen.findByText("Pay range (required to publish)");
    const min = screen.getByRole("textbox", { name: "Minimum a year" });
    const max = screen.getByRole("textbox", { name: "Maximum a year" });
    await user.type(min, "90000");
    await user.type(max, "80000");
    await user.click(screen.getByRole("button", { name: "Save pay and location" }));
    expect(await screen.findByText("The minimum cannot be above the maximum.")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);

    await user.clear(max);
    await user.type(max, "120000");
    await user.type(screen.getByRole("textbox", { name: "City" }), "Berlin");
    await user.click(screen.getByRole("button", { name: "Save pay and location" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(await calls.find((c) => c.method === "PATCH")!.json()).toMatchObject({ pay_range: { min: 90000, max: 120000, currency: "USD" }, city: "Berlin" });
  });

  it("disables Publish and prints what is missing beside it", async () => {
    renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(role()) });
    const publish = await screen.findByRole("button", { name: "Publish" });
    expect(publish).toBeDisabled();
    const needs = screen.getByText("Publishing needs:").parentElement!;
    expect(needs).toHaveTextContent("confirmed requirements (missing)");
    expect(needs).toHaveTextContent("pay range (missing)");
    expect(publish).toHaveAttribute("aria-describedby", "publish-needs");
  });

  it("marks done items, enables Publish, and publishes with an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const ready = role({ can_publish: true, blockers: [], pay_range: { min: 1, max: 2, currency: "USD" } });
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(ready), [`POST /v1/roles/${ROLE}/publish`]: ok(role({ status: "open" })) });
    const publish = await screen.findByRole("button", { name: "Publish" });
    expect(screen.getByText("Publishing needs:").parentElement).toHaveTextContent("pay range (done)");
    await user.click(publish);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/publish"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/publish"))!.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("shows the server's words for a refused publish", async () => {
    const user = userEvent.setup();
    const ready = role({ can_publish: true, blockers: [] });
    renderWithApi(<RoleIntake id={ROLE} />, {
      [GET_ROLE]: ok(ready),
      [`POST /v1/roles/${ROLE}/publish`]: fail(409, "conflict", { state: "answers_overdue" }),
    });
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    expect(await screen.findByText("New introductions are paused until overdue answers are given.")).toBeInTheDocument();
  });
});

describe("an open role", () => {
  const open = role({ status: "open", can_publish: false, blockers: [], pay_range: { min: 100, max: 200, currency: "EUR" } });

  it("is read-only, says Open and shows the preview", async () => {
    renderWithApi(<RoleIntake id={ROLE} />, { [GET_ROLE]: ok(open), [`GET /v1/roles/${ROLE}/preview`]: ok(preview) });
    expect(await screen.findByText("Open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Extract" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add requirement" })).not.toBeInTheDocument();
    expect(await screen.findByText("Said yes")).toBeInTheDocument();
    expect(screen.getByText("EUR 100 to 200 a year")).toBeInTheDocument();
  });

  it("closes only after a confirmation that says open asks are withdrawn", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<RoleIntake id={ROLE} />, {
      [GET_ROLE]: ok(open),
      [`GET /v1/roles/${ROLE}/preview`]: ok(preview),
      [`POST /v1/roles/${ROLE}/close`]: ok({ ...open, status: "closed" }),
    });
    await user.click(await screen.findByRole("button", { name: "Close role" }));
    const dialog = await screen.findByRole("dialog", { name: "Close this role?" });
    expect(within(dialog).getByText(/withdraws every ask that is still waiting/)).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/close"))).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Close role" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/close"))).toBe(true));
  });
});
